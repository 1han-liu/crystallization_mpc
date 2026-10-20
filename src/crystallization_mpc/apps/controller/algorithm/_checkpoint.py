"""Content checks for the single supported algorithm checkpoint format.

An old numeric format label is deliberately irrelevant. No missing numerical
state is reconstructed here, and validation never changes the supplied archive.
"""
from __future__ import annotations

import math
from typing import Any, Mapping


def _number(value: Any, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Checkpoint {name} must be a number.")
    number = float(value)
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise ValueError(f"Checkpoint {name} is outside its valid range.")
    return number


def _integer(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"Checkpoint {name} must be a nonnegative integer.")
    return value


def _boolean(value: Any, name: str) -> None:
    if type(value) is not bool:
        raise ValueError(f"Checkpoint {name} must be a boolean.")


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Checkpoint {name} must be an object.")
    return value


def _vector(value: Any, name: str, *, minimum: float | None = None) -> list:
    if not isinstance(value, list):
        raise ValueError(f"Checkpoint {name} must be an array.")
    for item in value:
        _number(item, name, minimum=minimum)
    return value


def _parameter_shape(value: Any, expected: Any, name: str) -> None:
    """Check serialized parameter types without filling defaults or changing values."""
    if isinstance(expected, bool):
        _boolean(value, name)
    elif isinstance(expected, (int, float)):
        _number(value, name)
    elif isinstance(expected, str):
        if not isinstance(value, str):
            raise ValueError(f"Checkpoint {name} must be text.")
    elif isinstance(expected, (list, tuple)):
        if not isinstance(value, list) or len(value) != len(expected):
            raise ValueError(f"Checkpoint {name} has an incompatible shape.")
        for actual, template in zip(value, expected):
            _parameter_shape(actual, template, name)
    elif isinstance(expected, Mapping):
        actual = _mapping(value, name)
        for key, template in expected.items():
            _parameter_shape(actual[key], template, f"{name}.{key}")


def validate_checkpoint(
    state: Mapping[str, Any], configured: Mapping[str, Any], history_keys: tuple[str, ...]
) -> None:
    """Raise on incomplete/invalid content, including superficially compatible archives."""
    frame = _integer(state["frame_index"], "frame_index")
    elapsed = _number(state["elapsed_s"], "elapsed_s", minimum=0)
    if frame == 0 and elapsed != 0:
        raise ValueError("Checkpoint initial elapsed time must be zero.")
    _boolean(state["running"], "running")
    if _number(state["control_T_j_reference_K"], "activation reference") <= 0:
        raise ValueError("Checkpoint activation temperature must be positive.")

    params = _mapping(state["params"], "params")
    for key, template in configured.items():
        _parameter_shape(params[key], template, f"params.{key}")
    # Reconstruct operator changes from the immutable startup snapshot.  New
    # online fields are not blanket-whitelisted: each must be justified by a
    # validated event in the numerical checkpoint's ledger.
    from crystallization_mpc.messaging.parameter_policy import parameter_defaults, parameter_policies, validate_parameter_changes
    from crystallization_mpc.messaging.controller_runtime import validate_runtime_changes
    from .parameters import merge_runtime_parameters
    expected = dict(configured)
    ledger = state.get("runtime_parameter_updates", [])
    if not isinstance(ledger, list):
        raise ValueError("Checkpoint parameter ledger must be an array.")
    event_ids = set()
    previous_tick = 0
    previous_event_time = -1.0
    defaults = parameter_defaults()
    policies = parameter_policies()
    for event in ledger:
        event = _mapping(event, "parameter update")
        event_id = event["event_id"]
        if not isinstance(event_id, str) or not event_id.strip() or event_id in event_ids:
            raise ValueError("Checkpoint parameter event identity is invalid.")
        event_ids.add(event_id)
        tick = _integer(event["effective_tick"], "parameter effective tick")
        if not previous_tick < tick <= frame:
            raise ValueError("Checkpoint parameter event order is invalid.")
        previous_tick = tick
        event_time = _number(event["elapsed_s"], "parameter elapsed time", minimum=0)
        if not previous_event_time < event_time <= elapsed:
            raise ValueError("Checkpoint parameter event time is invalid.")
        previous_event_time = event_time
        _boolean(event["refreshed"], "parameter refreshed")
        current = {section: {key: expected.get(key.removeprefix("params."), default)
                             for key, default in defaults[section].items()}
                   for section in ("shared", "controller")}
        patch = validate_parameter_changes(_mapping(event["parameter_changes"], "parameter changes"), current)
        actual_patch = validate_parameter_changes(event.get("actual_parameter_changes", patch))
        if any(key not in patch.get(section, {}) or value != patch[section][key]
               for section, values in actual_patch.items() for key, value in values.items()):
            raise ValueError("Checkpoint actual parameter changes contradict the submitted patch.")
        legacy = _mapping(event.get("changes", {}), "parameter legacy changes")
        if legacy:
            validate_runtime_changes(legacy)
        measurement_changed = event.get("measurement_changed", False)
        _boolean(measurement_changed, "measurement_changed")
        effects = {policies[section][key]["effect"] for section, values in actual_patch.items() for key in values}
        should_refresh = bool(legacy or measurement_changed or
            effects & {"controller_refresh", "measurement_segment", "reserved_filter"} or
            ("simulation_only" in effects and configured["run_type"] == "simulation"))
        if event["refreshed"] != should_refresh or not (patch or legacy or measurement_changed):
            raise ValueError("Checkpoint parameter refresh receipt contradicts its changes.")
        canonical = {key.removeprefix("params."): value for section, values in patch.items()
                     if section != "gsensor" for key, value in values.items()}
        expected = merge_runtime_parameters(expected, canonical)
    # Legacy target changes and fitted kinetics retain their established checks.
    mutable = {"target", "target_set", "sigma_set", "G_set", "n", "k_0", "E_A",
               "K_P_T", "K_I_T", "dT_dt_min", "dT_dt_max", "t_lag_threshold_perc"}
    if set(params) != set(expected):
        raise ValueError("Checkpoint parameter keys do not match startup plus confirmed changes.")
    for key, initial in expected.items():
        _parameter_shape(params[key], initial, f"params.{key}")
        if key not in mutable and params[key] != expected[key]:
            raise ValueError(f"Checkpoint immutable parameter {key} does not match.")
    if params["target"] not in {"sigma", "G"}:
        raise ValueError("Checkpoint control target is invalid.")
    for key in ("target_set", "sigma_set", "G_set"):
        if _number(params[key], key) <= 0:
            raise ValueError("Checkpoint target values must be positive.")
    if params["dT_dt_min"] > params["dT_dt_max"]:
        raise ValueError("Checkpoint rate bounds are inverted.")

    history = _mapping(state["history"], "history")
    if set(history) != set(history_keys):
        raise ValueError("Checkpoint history fields are incompatible.")
    # None is intentional for inactive targets, unavailable growth, and warm-up.
    nullable = {"dT_dt_set", "T_j_set", "objective", "target_sigma", "target_sigma_set",
                "abs_e_target_sigma", "objective_sigma", "target_G", "target_G_set",
                "abs_e_target_G", "objective_G", "G_u", "G_u_KF", "G_v", "G_v_KF",
                "G_measure", "G_measure_KF"}
    for key, values in history.items():
        if not isinstance(values, list):
            raise ValueError(f"Checkpoint history {key} must be an array.")
        for value in values:
            if key == "to_adapt":
                _boolean(value, key)
            elif value is not None or key not in nullable:
                _number(value, key)
    length = len(history["t"])
    if length > frame or any(len(history[key]) != length for key in history_keys
                             if key not in {"n", "k_0", "E_A"}):
        raise ValueError("Checkpoint history lengths are inconsistent.")
    if len({len(history[key]) for key in ("n", "k_0", "E_A")}) != 1 or len(history["n"]) > length:
        raise ValueError("Checkpoint parameter history lengths are inconsistent.")
    times = history["t"]
    if any(t < 0 or t > elapsed for t in times) or any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("Checkpoint history times are inconsistent.")

    integrals = _mapping(state["integrals"], "integrals")
    for key in ("int_e_target_dt", "int_e_dT_dt_dt", "int_e_T_dt"):
        _number(integrals[key], key)
    simulation = _mapping(state["simulation"], "simulation")
    process = _mapping(simulation["state"], "simulation.state")
    for key in ("T", "T_j", "T_j_set", "c", "count_middle"):
        number = _number(process[key], key, minimum=0)
        if key in {"T", "T_j", "T_j_set"} and number == 0:
            raise ValueError("Checkpoint process temperature must be positive.")
    seeds = _vector(simulation["size_list_seed"], "size_list_seed", minimum=0)
    sizes = _vector(simulation["size_list"], "size_list", minimum=0)
    if not seeds or not sizes or any(value == 0 for value in seeds):
        raise ValueError("Checkpoint crystal size arrays are incompatible.")
    for key in ("pending_seed", "mark_seed"):
        _boolean(simulation[key], key)
    population = simulation.get("seed_population")
    if population is None:
        # Historical archives have exactly one seed population.  Do not turn
        # backwards compatibility into blanket permission for arbitrary sizes.
        if len(seeds) != len(sizes):
            raise ValueError("Legacy checkpoint crystal size arrays differ.")
    else:
        population = _mapping(population, "seed_population")
        initial_count = _integer(population["initial_count"], "initial seed count")
        _boolean(population["base_seeded"], "base_seeded")
        additions = population["additions"]
        if initial_count == 0 or not isinstance(additions, list):
            raise ValueError("Checkpoint seed population metadata is invalid.")
        seed_keys = {"m_seed", "d_mean_seed", "d_std_seed", "rho_solute"}
        seed_params = dict(configured)

        def count_from_parameters() -> int:
            mean, std = float(seed_params["d_mean_seed"]), float(seed_params["d_std_seed"])
            volume = math.pi / 6 * (mean ** 3 + 3 * mean * std ** 2)
            return max(1, int(round(float(seed_params["m_seed"]) / (float(seed_params["rho_solute"]) * volume))))

        seed_updates = []
        for event in ledger:
            changed = {key.removeprefix("params."): value
                       for key, value in event.get("actual_parameter_changes", event["parameter_changes"]).get("controller", {}).items()
                       if key.removeprefix("params.") in seed_keys}
            if changed:
                seed_updates.append((event["effective_tick"], changed))
        update_position, previous_seed_tick = 0, 0
        current_seed_count = initial_count
        active_count = initial_count if population["base_seeded"] else 0
        for addition in additions:
            addition = _mapping(addition, "seed addition")
            seed_tick = _integer(addition["tick_seq"], "seed tick")
            if not previous_seed_tick < seed_tick <= frame:
                raise ValueError("Checkpoint seed addition order is invalid.")
            previous_seed_tick = seed_tick
            while update_position < len(seed_updates) and seed_updates[update_position][0] < seed_tick:
                seed_params.update(seed_updates[update_position][1])
                current_seed_count = count_from_parameters()
                update_position += 1
            if _integer(addition["added_count"], "added seed count") != current_seed_count:
                raise ValueError("Checkpoint seed addition count lacks matching configuration.")
            active_count += current_seed_count
            if _integer(addition["total_count"], "active seed count") != active_count:
                raise ValueError("Checkpoint existing crystal population was replaced.")
        while update_position < len(seed_updates):
            seed_params.update(seed_updates[update_position][1])
            current_seed_count = count_from_parameters()
            update_position += 1
        if len(seeds) != current_seed_count:
            raise ValueError("Checkpoint future seed population has the wrong count.")
        if len(sizes) != (active_count if additions or population["base_seeded"] else initial_count):
            raise ValueError("Checkpoint active crystal population has the wrong count.")
        if configured["run_type"] == "simulation" and simulation["mark_seed"] != bool(additions or population["base_seeded"]):
            raise ValueError("Checkpoint seed flag contradicts actual additions.")

    adaptation = _mapping(state["adaptation"], "adaptation")
    _boolean(adaptation["enabled"], "adaptation.enabled")
    modes = {"E_A", "k_0", "n", "E_A_and_k_0", "E_A_and_n", "k_0_and_n", "all"}
    if not isinstance(adaptation["mode"], str) or adaptation["mode"] not in modes:
        raise ValueError("Checkpoint adaptation mode is invalid.")
    count = _integer(adaptation["num_adapt"], "num_adapt")
    # This is the last returned fitting-window count. A failed fit preserves
    # that value; do not recompute it from the current eligibility flags.
    refresh = _mapping(state["refresh"], "refresh")
    refresh_count = _integer(refresh["count"], "refresh.count")
    segment_id = _integer(refresh["segment_id"], "refresh.segment_id")
    start_tick = _integer(refresh["segment_start_tick"], "refresh.segment_start_tick")
    history_start = _integer(refresh["segment_history_start"], "refresh.segment_history_start")
    if refresh_count != segment_id or refresh_count > frame or start_tick > frame or history_start > length:
        raise ValueError("Checkpoint refresh boundary is inconsistent.")
    event = refresh["last_event"]
    if refresh_count == 0:
        if event is not None or start_tick != 0 or history_start != 0:
            raise ValueError("Checkpoint initial refresh state is inconsistent.")
    else:
        event = _mapping(event, "refresh.last_event")
        if not isinstance(event["event_id"], str) or not event["event_id"].strip():
            raise ValueError("Checkpoint refresh event ID is invalid.")
        if _integer(event["effective_tick"], "refresh.effective_tick") != start_tick:
            raise ValueError("Checkpoint refresh event tick does not match its segment.")
        event_time = _number(event["elapsed_s"], "refresh.elapsed_s", minimum=0)
        if event_time > elapsed or not isinstance(event["reason"], str) or not event["reason"]:
            raise ValueError("Checkpoint refresh event metadata is invalid.")
        from crystallization_mpc.messaging.controller_runtime import validate_runtime_changes
        legacy = _mapping(event["changes"], "refresh.changes")
        if legacy:
            validate_runtime_changes(legacy)
        raw_changes = validate_parameter_changes(event.get("parameter_changes", {}))
        if not legacy and not raw_changes and event["reason"] != "measurement_changed":
            raise ValueError("Checkpoint refresh changes are empty.")
        if history_start >= length or history["t"][history_start] != event_time:
            raise ValueError("Checkpoint refresh boundary is not recorded in history.")
        completed = [event for event in ledger if event["refreshed"]]
        if completed:
            last = completed[-1]
            if any(last[key] != event[key] for key in ("event_id", "effective_tick", "elapsed_s", "changes")) or last.get("actual_parameter_changes", last["parameter_changes"]) != event.get("parameter_changes", {}):
                raise ValueError("Checkpoint latest parameter receipt disagrees with filter refresh.")

    segment = _mapping(adaptation["fitting_segment"], "adaptation.fitting_segment")
    if set(segment) != {"G", "sigma", "T", "ticks"}:
        raise ValueError("Checkpoint fitting segment fields are incompatible.")
    for key in ("G", "sigma", "T"):
        _vector(segment[key], f"fitting_segment.{key}")
    ticks = segment["ticks"]
    if not isinstance(ticks, list) or any(_integer(tick, "fitting tick") <= start_tick or tick > frame for tick in ticks):
        raise ValueError("Checkpoint fitting samples cross a refresh boundary.")
    if any(b <= a for a, b in zip(ticks, ticks[1:])):
        raise ValueError("Checkpoint fitting ticks are not increasing.")
    if len({len(segment[key]) for key in segment}) != 1 or len(ticks) > int(params["max_num_adapt"]):
        raise ValueError("Checkpoint fitting segment lengths are inconsistent.")
    if count > len(ticks):
        raise ValueError("Checkpoint adaptation count exceeds its segment/window.")
    recorded_ticks = state.get("history_tick_seq")
    if recorded_ticks is None:
        # Archives without an explicit tick map predate online dt changes.
        recorded_ticks = [int(round(value / float(configured["dt"]))) for value in history["t"]]
    if (not isinstance(recorded_ticks, list) or len(recorded_ticks) != length
            or any(_integer(tick, "history tick") < 1 or tick > frame for tick in recorded_ticks)
            or any(b <= a for a, b in zip(recorded_ticks, recorded_ticks[1:]))):
        raise ValueError("Checkpoint history tick identities are inconsistent.")
    history_by_tick = {tick: index for index, tick in enumerate(recorded_ticks)}
    for event in ledger:
        index = history_by_tick.get(event["effective_tick"])
        if index is None or history["t"][index] != event["elapsed_s"]:
            raise ValueError("Checkpoint parameter receipt is not a completed recorded tick.")
    # The interval ending on an update tick still belongs to the old period.
    # A new period is used only for subsequent intervals; this validates the
    # historical axis without rewriting it as tick * the latest dt.
    dt_updates = {event["effective_tick"]: event["parameter_changes"].get("shared", {}).get("dt")
                  for event in ledger if "dt" in event["parameter_changes"].get("shared", {})}
    period, accumulated = float(configured["dt"]), 0.0
    for tick in range(1, frame + 1):
        accumulated += period
        if tick in history_by_tick and not math.isclose(history["t"][history_by_tick[tick]], accumulated,
                                                       rel_tol=1e-10, abs_tol=1e-8):
            raise ValueError("Checkpoint time axis disagrees with its period-change ledger.")
        if tick in dt_updates:
            period = float(dt_updates[tick])
    for position, tick in enumerate(ticks):
        index = history_by_tick.get(tick)
        if index is None or not history["to_adapt"][index]:
            raise ValueError("Checkpoint fitting sample has no eligible recorded tick.")
        for key, historical in (("G", "G_measure_KF"), ("sigma", "sigma"), ("T", "T")):
            if segment[key][position] != history[historical][index]:
                raise ValueError("Checkpoint fitting segment disagrees with history.")
    fitting = _vector(adaptation["fitting_G_measure_history"], "fitting history")
    last_tick = adaptation["fitting_last_tick"]
    if last_tick is None:
        if fitting or ticks:
            raise ValueError("Checkpoint fitting derivatives lack a last tick.")
    else:
        if not ticks or _integer(last_tick, "fitting_last_tick") != ticks[-1] or not 1 <= len(fitting) <= 3:
            raise ValueError("Checkpoint fitting derivative boundary is inconsistent.")
        for offset, value in enumerate(reversed(fitting)):
            index = history_by_tick.get(last_tick - offset)
            if index is None or not history["to_adapt"][index] or value != history["G_measure"][index]:
                raise ValueError("Checkpoint fitting derivative history crosses a gap.")
    for key in ("pause_reason", "control_hold_reason"):
        if adaptation[key] is not None and not isinstance(adaptation[key], str):
            raise ValueError("Checkpoint pause reason must be text or null.")
    diagnostics = _mapping(adaptation["diagnostics"], "diagnostics")
    if diagnostics["last_status"] not in ("not_run", "waiting_for_samples", "fit_succeeded", "fit_failed"):
        raise ValueError("Checkpoint fitting status is invalid.")
    for key in ("fit_count", "failure_count"):
        if _integer(diagnostics[key], key) > frame:
            raise ValueError("Checkpoint fitting count exceeds elapsed ticks.")
    if diagnostics["last_tick"] is not None and _integer(diagnostics["last_tick"], "last_tick") > frame:
        raise ValueError("Checkpoint fitting tick exceeds elapsed ticks.")
    if diagnostics["last_mode"] is not None and diagnostics["last_mode"] not in tuple(modes):
        raise ValueError("Checkpoint fitting mode is invalid.")

    filters = _mapping(state["filters"], "filters")
    for key, dimension in (("controller", 4), ("G_measure", 3), ("n", 3), ("k_0", 3), ("E_A", 3)):
        filter_state = _mapping(filters[key], f"filters.{key}")
        vector = _vector(filter_state["state"], f"{key}.state")
        covariance = filter_state["covariance"]
        if len(vector) != dimension or not isinstance(covariance, list) or len(covariance) != dimension:
            raise ValueError("Checkpoint filter dimensions are incompatible.")
        for row in covariance:
            if len(_vector(row, f"{key}.covariance")) != dimension:
                raise ValueError("Checkpoint covariance dimensions are incompatible.")
