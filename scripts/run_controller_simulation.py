"""Reproducible image-free numerical audit; no service or equipment is started.

Run from the project root with its Python environment. Nonzero exit means at
least one ordinary scenario failed; artifacts are still retained. This runner
does not change the model to make a scenario pass.
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import html
import importlib.metadata
import json
import math
from pathlib import Path
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from unittest.mock import patch

import numpy as np

from crystallization_mpc.apps.controller.algorithm import adaptation
from crystallization_mpc.apps.controller.algorithm.controller import CrystallizationController
from crystallization_mpc.apps.controller.algorithm.mass_balance import calc_volume
from crystallization_mpc.apps.controller.tick import ControllerTickInput

ROOT = Path(__file__).resolve().parents[1]
KINETIC = ("E_A", "k_0", "n")
NOISE = {"T_noise": (456, .01), "c_noise": (789, .0002),
         "G_u_noise": (1122, 1e-9), "G_u_KF_noise": (3344, 1e-9),
         "G_v_noise": (5566, 1e-9), "G_v_KF_noise": (7788, 1e-9)}


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def fixed_inputs(ticks):
    # Fully materialize the existing deterministic noise rule. All six arrays
    # cover the complete run, so no unrecorded RNG fallback is used.
    return {name: [float(np.random.RandomState((i + 1) * seed).standard_normal()) * scale
                   for i in range(ticks)] for name, (seed, scale) in NOISE.items()}


@contextmanager
def no_network():
    attempts = []
    def deny(*_args, **_kwargs):
        attempts.append("blocked")
        raise AssertionError("Network access is forbidden in the numerical audit")
    with patch.object(socket.socket, "connect", deny), patch.object(socket.socket, "connect_ex", deny):
        yield attempts


def make_controller(parameters, run_id, adaptive=None):
    controller = CrystallizationController()
    controller.configure(parameters, run_id)
    controller.start()
    controller.set_adaptation(adaptive is not None, adaptive or "E_A")
    return controller


def mass(controller, sizes=None):
    p = controller.params
    solid = float(np.sum(calc_volume(controller.size_list if sizes is None else sizes))) * p["rho_solute"]
    return controller.simulation_state["c"] * p["m_solvent"] + solid


def execute(parameters, ticks, *, run_id="numerical-audit", adaptive=None, seed_tick=None,
            resume_at=None):
    assert parameters["run_type"] == "simulation"
    assert parameters["growth_rate_source"] == "simulated"
    controller = make_controller(parameters, run_id, adaptive)
    configured = copy.deepcopy(controller.params)
    population = controller.size_list_seed.tolist()
    records, violations, checkpoints = [], [], []
    seed_deliveries = 0
    with patch.object(adaptation, "_bounded_minimum", wraps=adaptation._bounded_minimum) as solver:
        for i in range(1, ticks + 1):
            previous = dict(controller.simulation_state)
            before_mass = mass(controller)
            kinetic_before = {k: controller.params[k] for k in KINETIC}
            if i == seed_tick:
                controller.add_seed({"event_id": f"{run_id}-seed"})
                seed_deliveries += 1
                # This algorithm replaces the empty population on one seed event.
                before_mass = mass(controller, controller.size_list_seed)
            fits_before = solver.call_count
            started = time.perf_counter()
            try:
                dt = float(controller.params["dt"])
                result = controller.step(ControllerTickInput(i, dt, i * dt))
                output = result.to_dict() if result is not None else {"valid": False, "error": "warmup"}
                status = "warmup" if result is None else "valid" if result.valid else "invalid"
            except Exception as exc:
                output = {"valid": False, "error": f"{type(exc).__name__}: {exc}"}
                status = "exception"
            elapsed = time.perf_counter() - started
            noise_c = parameters["simulation_noise"]["c_noise"][i - 1] if i > 1 else 0.0
            residual = mass(controller) - before_mass - controller.params["m_solvent"] * noise_c
            fits = solver.call_count - fits_before
            row = {"tick": i, "elapsed_s": i * controller.params["dt"], "status": status,
                   "runtime_s": elapsed, "fit_calls": fits, "adaptation_samples": controller.num_adapt,
                   "seed_applied": i == seed_tick and controller.mark_seed and not controller.pending_seed,
                   "mass_residual_kg": residual, "crystal_mean_m": float(np.mean(controller.size_list)),
                   "crystal_min_m": float(np.min(controller.size_list)),
                   "plant_T": controller.simulation_state["T"], "plant_T_j": controller.simulation_state["T_j"],
                   "plant_c": controller.simulation_state["c"],
                   "previous_feedback_K": previous["T_j_set"], **output}
            records.append(row)
            if not math.isfinite(residual) or abs(residual) > 1e-10:
                violations.append({"tick": i, "check": "mass_conservation_noise_corrected"})
            if row["crystal_min_m"] < 0 or not np.isfinite(controller.size_list).all():
                violations.append({"tick": i, "check": "crystal_sizes"})
            if i == seed_tick and not row["seed_applied"]:
                violations.append({"tick": i, "check": "seed_not_applied"})
            if status == "valid":
                p = controller.params
                if not p["T_j_min"] <= result.T_j_set <= p["T_j_max"]:
                    violations.append({"tick": i, "check": "jacket_bounds"})
                if not p["dT_dt_min"] - 1e-9 <= result.dT_dt_set <= p["dT_dt_max"] + 1e-9:
                    violations.append({"tick": i, "check": "rate_bounds"})
                if controller.simulation_state["T_j_set"] != result.T_j_set:
                    violations.append({"tick": i, "check": "control_feedback"})
                if fits and controller.num_adapt < p["min_num_adapt"]:
                    violations.append({"tick": i, "check": "premature_fit"})
                for key in KINETIC:
                    selected = adaptive is not None and (adaptive == "all" or key in adaptive.split("_and_"))
                    old, new = kinetic_before[key], p[key]
                    if not selected and old != new:
                        violations.append({"tick": i, "check": f"unselected_parameter_{key}"})
                    if selected and fits:
                        lo, hi = ((math.exp(math.log(old) / 2), math.exp(math.log(old) * 2))
                                  if key == "k_0" else (old / 2, old * 2))
                        if not lo * (1 - 1e-10) <= new <= hi * (1 + 1e-10):
                            violations.append({"tick": i, "check": f"adaptation_bounds_{key}"})
            if i == resume_at:
                state = json.loads(json.dumps(controller.export_state(), allow_nan=False))
                checkpoints.append(state)
                restored = CrystallizationController()
                if not restored.restore_state(parameters, run_id, state):
                    violations.append({"tick": i, "check": "restore_rejected"})
                    break
                controller = restored
            if status == "exception":
                break
    state = controller.export_state()
    controller.stop()
    try:
        controller.step(ControllerTickInput(controller.frame_index + 1, configured["dt"],
                                           (controller.frame_index + 1) * configured["dt"]))
    except RuntimeError:
        pass
    else:
        violations.append({"check": "stop_did_not_prevent_step"})
    return {"records": records, "violations": violations, "configured": configured,
            "population": population, "state": state, "checkpoints": checkpoints,
            "seed_deliveries": seed_deliveries}


def canonical_records(run):
    return [{k: v for k, v in row.items() if k != "runtime_s"} for row in run["records"]]


def summarize(run, *, adaptive=False, performance=False):
    rows = run["records"]
    valid = [r for r in rows if r["status"] == "valid"]
    invalid = [r for r in rows if r["status"] not in {"valid", "warmup"}]
    fits = sum(r["fit_calls"] for r in rows)
    ok = bool(valid) and not invalid and not run["violations"] and (not adaptive or fits > 0)
    timings = [r["runtime_s"] for r in rows[20:]]
    perf = {"measured_ticks": len(timings), "p95_s": float(np.percentile(timings, 95)) if timings else None,
            "max_s": max(timings) if timings else None}
    if performance:
        ok &= len(timings) >= 1000 and perf["p95_s"] < 4 and perf["max_s"] < 5
    errors = [r["target_error_abs"] for r in valid]
    return {"status": "PASS" if ok else "FAIL", "ticks": len(rows), "valid_ticks": len(valid),
            "warmup_ticks": sum(r["status"] == "warmup" for r in rows), "invalid_ticks": len(invalid),
            "first_failure": {"tick": invalid[0]["tick"], "error": invalid[0]["error"]} if invalid else None,
            "fit_calls": fits, "first_fit_tick": next((r["tick"] for r in rows if r["fit_calls"]), None),
            "violations": run["violations"], "seed_deliveries": run["seed_deliveries"],
            "max_mass_residual_kg": max(abs(r["mass_residual_kg"]) for r in rows),
            "tracking_descriptive_only": {"mean_absolute_error": float(np.mean(errors)) if errors else None,
                                          "initial_error": errors[0] if errors else None,
                                          "last_error": errors[-1] if errors else None},
            "performance": perf, "performance_gate_applied": performance}


def save_run(destination, run, summary):
    destination.mkdir()
    write_json(destination / "parameters.json", run["configured"])
    write_json(destination / "seed_sizes.json", run["population"])
    write_json(destination / "final_state.json", run["state"])
    write_json(destination / "summary.json", summary)
    for i, state in enumerate(run["checkpoints"]):
        write_json(destination / f"checkpoint-{i}.json", state)
    columns = list(dict.fromkeys(k for row in run["records"] for k in row))
    with (destination / "ticks.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(run["records"])


def curves(run):
    # Offline vector plots, no browser assets or third-party plotting dependency.
    parts = []
    for names in (("T", "T_j", "T_j_set"), ("c", "c_KF"), ("target_value", "target_set"),
                  ("G_model", "G_measure")):
        rows = [r for r in run["records"] if r["status"] == "valid"]
        values = [r[k] for r in rows for k in names if r.get(k) is not None]
        if not values:
            continue
        lo, hi = min(values), max(values)
        span = max(hi - lo, max(abs(lo), 1e-15) * .01)
        end = max((r["elapsed_s"] for r in rows), default=1)
        svg = ['<svg viewBox="0 0 660 210" role="img">', '<path d="M65 20V175H635" fill="none" stroke="#999"/>']
        svg.append(f'<text x="5" y="25">{hi:.4g}</text><text x="5" y="175">{lo:.4g}</text>')
        svg.append(f'<text x="240" y="205">Simulated time: 0–{end:g} s</text>')
        for k, color in zip(names, ("#2563eb", "#dc2626", "#059669")):
            points = " ".join(f'{65 + r["elapsed_s"] / end * 570:.2f},{175 - (r[k] - lo) / span * 150:.2f}'
                              for r in rows if r.get(k) is not None)
            svg.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="1.3"/>')
        svg.append('</svg>')
        parts.append(f'<h4>{html.escape(" / ".join(names))}</h4>' + ''.join(svg))
    return ''.join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    noise = fixed_inputs(1020)
    base = {"run_type": "simulation", "growth_rate_source": "simulated", "dt": 5.0,
            "simulation_noise": noise}
    scenarios = [(f"{mode}_{target}", {"mode": mode, "target": target}, None, 120, 11)
                 for mode in ("MPC", "PI") for target in ("sigma", "G")]
    scenarios.append(("MPC_sigma_performance", {}, None, 1020, None))
    scenarios.append(("MPC_sigma_seeded_long", {}, None, 1020, 11))
    for mode in adaptation.ADAPTATION_MODES:
        scenarios.append((f"adapt_default_{mode}", {}, mode, 80, 11))
        scenarios.append((f"adapt_positive_{mode}", {"c_init": .4}, mode, 80, 11))
    summaries, chart_sections = {}, []
    runs = {}
    with no_network() as network_attempts:
        for name, overrides, adaptive, ticks, seed_tick in scenarios:
            run = execute({**base, **overrides}, ticks, run_id=name, adaptive=adaptive, seed_tick=seed_tick)
            summary = summarize(run, adaptive=adaptive is not None, performance=name.endswith("performance"))
            summary["scenario_overrides"] = overrides
            summary["seed_tick"] = seed_tick
            summary["adaptation_mode"] = adaptive
            summaries[name] = summary
            save_run(output / name, run, summary)
            if name in {"MPC_sigma", "MPC_G", "MPC_sigma_seeded_long", "adapt_positive_all", "adapt_default_all"}:
                chart_sections.append(f'<h2>{name}: {summary["status"]}</h2>' + curves(run))
            if name == "adapt_positive_all":
                runs[name] = run
            print(name, summary["status"], "valid", summary["valid_ticks"], "invalid", summary["invalid_ticks"], flush=True)
        reference = runs["adapt_positive_all"]
        parameters = {**base, "c_init": .4}
        repeated = execute(parameters, 80, run_id="adapt_positive_all", adaptive="all", seed_tick=11)
        restored = execute(parameters, 80, run_id="adapt_positive_all", adaptive="all", seed_tick=11, resume_at=40)
        for name, run in (("repeat_all", repeated), ("resume_all", restored)):
            summary = summarize(run, adaptive=True)
            exact = canonical_records(run) == canonical_records(reference) and run["state"] == reference["state"]
            summary["exact_repeat_or_resume"] = exact
            if not exact:
                summary["status"] = "FAIL"
            summaries[name] = summary
            save_run(output / name, run, summary)
            print(name, summary["status"], flush=True)
    fixture = ROOT / "tests/controller/fixtures/matlab_r2021a_golden.mat"
    metadata = {"python": sys.version, "executable": sys.executable,
                "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "source_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in sorted((ROOT / "src/crystallization_mpc/apps/controller/algorithm").glob("*.py"))},
                "packages": {x: importlib.metadata.version(x) for x in ("numpy", "scipy", "pytest")},
                "mat_fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest()}
    report = {"scope": "image-free, algorithm-only numerical audit", "metadata": metadata,
              "status": "PASS" if all(s["status"] == "PASS" for s in summaries.values()) else "FAIL",
              "scenarios": summaries, "blocked_network_attempts": len(network_attempts),
              "device_access": "No ProcessState/adapter supplied; socket connects blocked in audit process",
              "not_run": ["GSensor/images", "web lifecycle", "database", "real experiment"],
              "limitations": ["PI retains documented mixed semantics D-001/D-002.",
                              "Tracking metrics are descriptive, not tuned-performance acceptance.",
                              "Seed event occurs at tick 11 in seeded stress cases, not at default seed_time.",
                              "Simulation state uses the current implementation; mass residual corrects injected concentration noise.",
                              "Default-initial-condition adaptation failures are retained, not silently excluded.",
                              "fit_calls counts scalar optimizer attempts, including failed initial evaluations; it is not successful parameter updates.",
                              "This is Python simulation acceptance, not full MATLAB closed-loop equivalence."]}
    write_json(output / "report.json", report)
    table = ''.join(f'<tr><td>{html.escape(n)}</td><td>{s["status"]}</td><td>{s["valid_ticks"]}/{s["ticks"]}</td><td>{s["fit_calls"]}</td></tr>'
                    for n, s in summaries.items())
    (output / "report.html").write_text('<!doctype html><meta charset="utf-8"><title>Numerical simulation audit</title>'
        '<style>body{font:15px sans-serif;max-width:1000px;margin:36px auto;color:#172033}td,th{padding:7px;border-bottom:1px solid #ddd}svg{width:100%;max-width:750px}text{font-size:11px}</style>'
        '<h1>Image-free controller simulation audit</h1><p>No hardware, image input, database or web experiment started. '
        'Temperatures are K; concentration kg/kg solvent; G is m/s. Tracking is descriptive, not a performance guarantee.</p>'
        '<p>FAIL scenarios are retained. Default-initial-condition adaptation may violate logarithmic-model prerequisites. '
        'c_init=0.4 scenarios are separate qualified tests, not a change to project defaults. PI has documented mixed semantics.</p>'
        '<p>Optimizer attempts include failures and do not imply successful parameter updates. '
        'MATLAB solver numerical parity is a separate verification gate.</p>'
        '<table><tr><th>Scenario</th><th>Status</th><th>Valid/ticks</th><th>Optimizer attempts</th></tr>' + table + '</table>' + ''.join(chart_sections))
    print("Report:", output / "report.html", flush=True)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
