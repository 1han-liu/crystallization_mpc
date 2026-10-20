"""Independent, real-time test plant; no controller/filter state is an input.

Physics uses the migrated process equations and ``*_proc`` kinetics. Sensor
noise is sampled on a declared 15 s cadence and held, never injected into the
latent physical state every second. ``count_middle=17`` is a transport fixture,
not an FBRM or nucleation prediction.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Mapping

import numpy as np

from crystallization_mpc.apps.controller.algorithm.control import update_T_j
from crystallization_mpc.apps.controller.algorithm.dynamics import state_transition_function_T
from crystallization_mpc.apps.controller.algorithm.mass_balance import (
    calc_next_crystallization_mass_balance, calc_volume, create_size_list,
)
from crystallization_mpc.apps.controller.algorithm.parameters import build_parameters
from crystallization_mpc.apps.controller.algorithm.thermodynamics import calc_G, calc_relative_sigma

from .opcua_client import ProsysProcessClient


SOURCE = "Synthetic G / Model-generated input"
PLANT_PARAMETER_KEYS = frozenset({
    "T_init_sigma", "T_j_init", "T_j_set_init", "c_init", "m_solvent",
    "n_proc", "E_A_proc", "k_0_proc", "d_mean_seed", "d_std_seed",
})


def canonical_started_at(value: str | None) -> str | None:
    if value is None:
        return None
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("experiment_started_at must include a timezone.")
    return stamp.astimezone(timezone.utc).isoformat()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExternalPlant:
    """Deterministic process truth. ``advance`` is called only by the wall clock loop."""

    def __init__(self, *, seed: int = 123, parameters: Mapping[str, Any] | None = None):
        overrides = dict(parameters or {})
        if set(overrides) - PLANT_PARAMETER_KEYS:
            raise ValueError("Only frozen plant parameters are allowed; no controller fitted parameters.")
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**32:
            raise ValueError("seed must be an unsigned 32-bit integer.")
        self.params = build_parameters({
            "dt": 15.0, "dt_G": 15.0, "target": "sigma", "k_0": 2e15,
            **overrides,
        })
        for key in PLANT_PARAMETER_KEYS & self.params.keys():
            if not math.isfinite(float(self.params[key])) or float(self.params[key]) < 0:
                raise ValueError(f"Invalid plant parameter {key}.")
        self.seed = seed
        self.T = float(self.params["T_init"])
        self.T_j = float(self.params["T_j_init"])
        self.T_j_set = float(self.params["T_j_set_init"])
        self.c = float(self.params["c_init"])
        self.elapsed_s = 0.0
        self.sizes = np.empty(0, dtype=float)
        self.seed_event_ids: list[str] = []
        self.seed_mass_kg = 0.0

    def add_seed(self, event_id: str, mass_kg: float) -> bool:
        if not event_id or not isinstance(event_id, str):
            raise ValueError("A nonempty seed event_id is required.")
        if event_id in self.seed_event_ids:
            if not math.isclose(mass_kg, self.seed_mass_kg, rel_tol=0, abs_tol=1e-12):
                raise ValueError("A repeated event must retain its original mass.")
            return False
        if self.seed_event_ids:
            raise ValueError("This paper test permits exactly one seed addition.")
        if not math.isfinite(mass_kg) or not math.isclose(mass_kg, .001, rel_tol=0, abs_tol=1e-12):
            raise ValueError("The paper protocol requires exactly 1 g (0.001 kg) seed.")
        rng = np.random.default_rng(np.random.SeedSequence([self.seed, 913]))
        sizes = create_size_list(self.params, mass_kg, self.params["d_mean_seed"],
                                 self.params["d_std_seed"], rng=rng)
        # Keep the stochastic size distribution but make the supplied mass 1 g.
        actual_mass = float(np.sum(calc_volume(sizes))) * self.params["rho_solute"]
        self.sizes = sizes * (mass_kg / actual_mass) ** (1.0 / 3.0)
        self.seed_mass_kg = mass_kg
        self.seed_event_ids.append(event_id)
        return True

    def advance(self, elapsed_s: float, setpoint_K: float) -> None:
        if not math.isfinite(elapsed_s) or not 0 < elapsed_s <= 5:
            raise ValueError("A physical update must represent at most 5 s of measured wall time.")
        if not math.isfinite(setpoint_K) or not self.params["T_j_min"] <= setpoint_K <= self.params["T_j_max"]:
            raise ValueError("Actual OPC UA setpoint is outside the plant bounds.")
        remaining = float(elapsed_s)
        while remaining > 1e-10:
            dt = min(1.0, remaining)
            previous_T = self.T
            T = state_transition_function_T(self.params, self.T, self.T_j, dt)
            T_j = update_T_j(setpoint_K, self.T_j, self.params["dT_j_dt_max"], dt,
                             self.params["dT_j"], min(self.params["dt_update"], dt),
                             self.params["T_j_min"], self.params["T_j_max"])
            c, sizes = calc_next_crystallization_mass_balance(
                self.params, self.params["m_solvent"] * self.c, self.params["m_solvent"],
                self.sizes, previous_T, dt,
            )
            if not all(math.isfinite(v) for v in (T, T_j, c)) or c < 0 or np.any(sizes < 0):
                raise FloatingPointError("Plant left the valid mass-balance domain; stop, do not clamp data.")
            self.T, self.T_j, self.c, self.sizes = T, T_j, c, sizes
            self.elapsed_s += dt
            remaining -= dt
        self.T_j_set = setpoint_K

    @property
    def G_true_m_s(self) -> float:
        return float(calc_G(self.params, self.c, self.T, is_model=True))

    def noise(self, channel: int, index: int, amplitude: float) -> float:
        rng = np.random.default_rng(np.random.SeedSequence([self.seed, channel, index]))
        return float(rng.normal(0.0, amplitude))

    def process_measurements(self) -> dict[str, float]:
        index = int(self.elapsed_s // 15.0)
        return {
            "T": self.T - 273.15 + self.noise(1, index, .01),
            "T_j": self.T_j - 273.15,
            "c": self.c + self.noise(2, index, .0002),
            "count_middle": 17.0,
        }

    def serialize(self) -> dict[str, Any]:
        return {"params": self.params, "seed": self.seed, "T": self.T, "T_j": self.T_j,
                "T_j_set": self.T_j_set, "c": self.c, "elapsed_s": self.elapsed_s,
                "sizes": self.sizes.tolist(), "seed_event_ids": self.seed_event_ids,
                "seed_mass_kg": self.seed_mass_kg}

    @classmethod
    def restore(cls, value: Mapping[str, Any]) -> "ExternalPlant":
        obj = cls(seed=int(value["seed"]), parameters={key: value["params"][key]
                  for key in PLANT_PARAMETER_KEYS if key in value["params"]})
        for key in ("T", "T_j", "T_j_set", "c", "elapsed_s", "seed_mass_kg"):
            number = float(value[key])
            if not math.isfinite(number):
                raise ValueError(f"Nonfinite persisted {key}.")
            setattr(obj, key, number)
        obj.sizes = np.asarray(value["sizes"], dtype=float)
        if not np.isfinite(obj.sizes).all() or np.any(obj.sizes < 0):
            raise ValueError("Invalid persisted size population.")
        obj.seed_event_ids = list(value["seed_event_ids"])
        return obj


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, allow_nan=False, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class PlantService:
    """Threaded test-only service, with durable exactly-once seed events.

    Restoring a checkpoint does not start I/O or resume a run. Every new
    connection must re-verify Prosys. A failed read/write stops advancement;
    automatic reconnect/replay is deliberately absent.
    """

    def __init__(self, client: ProsysProcessClient, state_path: Path, *,
                 monotonic: Callable[[], float] = time.monotonic,
                 now: Callable[[], str] = utc_now):
        self.client = client
        self.state_path = Path(state_path)
        self._monotonic, self._now = monotonic, now
        self._lock = threading.RLock()
        self._shutdown = threading.Event()
        self._thread: threading.Thread | None = None
        self.plant: ExternalPlant | None = None
        self.run_id: str | None = None
        self.experiment_started_at: str | None = None
        self.state = "unprepared"
        self.error: str | None = None
        self.heartbeat_at: str | None = None
        self.observed_setpoint_K: float | None = None
        self.setpoint_observed_at: str | None = None
        self._heartbeat_mono: float | None = None
        self._last_step_mono: float | None = None
        self._last_checkpoint_mono = 0.0
        self.observed_at: str | None = None
        self.sample_seq = 0
        self.G_true_m_s: float | None = None
        self.G_measured_m_s: float | None = None
        self._sample_index = -1
        if self.state_path.exists():
            pointer = json.loads(self.state_path.read_text())
            self.run_id = pointer["run_id"]
            payload = json.loads(self._run_path(self.run_id).read_text())
            if pointer.get("schema_version") != 1 or payload.get("schema_version") != 1 or payload["run_id"] != self.run_id:
                raise ValueError("Plant checkpoint identity/schema mismatch; refusing recovery.")
            self.plant = ExternalPlant.restore(payload["plant"])
            self.experiment_started_at = payload["experiment_started_at"]
            self.sample_seq = int(payload["sample_seq"])
            self.state = "stopped"

    def _run_path(self, run_id: str) -> Path:
        digest = hashlib.sha256(run_id.encode()).hexdigest()
        return self.state_path.parent / "runs" / f"{digest}.json"

    def _persist(self) -> None:
        if self.plant is None:
            return
        _atomic_json(self._run_path(self.run_id), {
            "schema_version": 1, "run_id": self.run_id,
            "experiment_started_at": self.experiment_started_at,
            "plant": self.plant.serialize(), "sample_seq": self.sample_seq,
            "saved_at": self._now(), "source": SOURCE,
        })
        _atomic_json(self.state_path, {"schema_version": 1, "run_id": self.run_id})
        self._last_checkpoint_mono = self._monotonic()

    def reset(self, run_id: str, *, experiment_started_at: str | None = None,
              seed: int = 123, parameters: Mapping[str, Any] | None = None,
              acknowledge_reset: bool = False) -> dict[str, Any]:
        with self._lock:
            if not acknowledge_reset or not isinstance(run_id, str) or not run_id.strip():
                raise ValueError("Explicit acknowledge_reset and nonempty run_id are required.")
            if self.state in {"running", "prepared"}:
                raise ValueError("Stop the active test before reset.")
            if self._run_path(run_id).exists():
                raise ValueError("Run identity already has durable state; use a new run_id, not reset/reseed.")
            started_at = canonical_started_at(experiment_started_at)
            new_plant = ExternalPlant(seed=seed, parameters=parameters)
            # A stopped test may retain an expired UA session. Only this
            # explicitly acknowledged new-run boundary may replace it; tick()
            # never reconnects or replays an uncertain write.
            try:
                self.client.close()
            except Exception as exc:
                self.state, self.error = "error", f"Reset connection close failed: {type(exc).__name__}: {exc}"
                raise
            try:
                self.client.connect()  # re-verifies product and every writable node
                observed_setpoint = self.client.read_setpoint_K()
                observed_at = self._now()
                # Exactly one publication, before committing the new identity.
                self.client.write_process(new_plant.process_measurements())
            except Exception as exc:
                self.state, self.error = "error", f"Reset preparation failed: {type(exc).__name__}: {exc}"
                try:
                    self.client.close()
                except Exception as close_error:
                    self.error += f"; cleanup close failed: {type(close_error).__name__}: {close_error}"
                raise  # cleanup must not replace the original read/write error
            new_plant.T_j_set = observed_setpoint
            self.plant = new_plant
            self.run_id, self.experiment_started_at = run_id, started_at
            self.state, self.error = "prepared", None
            self._sample_index, self.sample_seq = -1, 0
            self.observed_at = self.G_true_m_s = self.G_measured_m_s = None
            self.heartbeat_at, self._heartbeat_mono = self._now(), self._monotonic()
            self.observed_setpoint_K, self.setpoint_observed_at = observed_setpoint, observed_at
            self._last_step_mono = None
            try:
                self._persist()
            except Exception as exc:
                self.state, self.error = "error", f"Reset checkpoint failed: {exc}"
                raise
            return self.status()

    def _check_identity(self, run_id: str, started_at: str | None) -> None:
        if run_id != self.run_id or canonical_started_at(started_at) != self.experiment_started_at:
            raise ValueError("Plant run identity mismatch.")

    def start(self, run_id: str, experiment_started_at: str) -> dict[str, Any]:
        with self._lock:
            started_at = canonical_started_at(experiment_started_at)
            if self.plant is None or not started_at or run_id != self.run_id:
                raise ValueError("Prepare this run and provide authoritative experiment_started_at first.")
            if self.experiment_started_at not in (None, started_at):
                raise ValueError("Cannot rebind a started experiment.")
            if self.state == "running":
                return self.status()
            if self.state == "error":
                raise ValueError("A failed run is terminal; prepare a new run.")
            self.client.connect()
            self.plant.T_j_set = self.client.read_setpoint_K()
            self.observed_setpoint_K = self.plant.T_j_set
            self.setpoint_observed_at = self._now()
            self.experiment_started_at = started_at
            self.state = "running"
            self._last_step_mono = self._monotonic()
            self._sample_growth()
            try:
                self._persist()
            except Exception as exc:
                self.state, self.error = "error", f"Start checkpoint failed: {exc}"
                raise
            return self.status()

    def add_seed(self, run_id: str, experiment_started_at: str, event_id: str,
                 mass_kg: float = .001) -> dict[str, Any]:
        with self._lock:
            self._check_identity(run_id, experiment_started_at)
            if self.state != "running":
                raise ValueError("Seed can be added only to a running, identity-bound plant.")
            # Validate/apply before persistence. Once persistence is attempted,
            # its outcome may be uncertain (rename succeeded, fsync failed).
            # Never roll memory back and permit a second seed in that case.
            added = self.plant.add_seed(event_id, mass_kg)
            try:
                self._persist()
            except Exception as exc:
                self.state, self.error = "error", f"Seed checkpoint failed: {exc}"
                raise
            return {**self.status(), "seed_applied": added, "event_id": event_id}

    def stop(self, run_id: str, experiment_started_at: str | None) -> dict[str, Any]:
        with self._lock:
            self._check_identity(run_id, experiment_started_at)
            self._stop_locked()
            return self.status()

    def _stop_locked(self) -> None:
        if self.state in {"running", "prepared"}:
            self.state = "stopped"
            try:
                self._persist()
            except Exception as exc:
                self.state, self.error = "error", f"Stop checkpoint failed: {exc}"
                raise

    def _sample_growth(self) -> None:
        index = int(self.plant.elapsed_s // 15.0)
        if index == self._sample_index:
            return
        self._sample_index = index
        self.sample_seq += 1
        self.G_true_m_s = self.plant.G_true_m_s
        self.G_measured_m_s = self.G_true_m_s + self.plant.noise(3, index, 1e-9)
        self.observed_at = self._now()

    def tick(self) -> None:
        with self._lock:
            if self.state not in {"prepared", "running"}:
                return
            try:
                setpoint = self.client.read_setpoint_K()
                self.observed_setpoint_K, self.setpoint_observed_at = setpoint, self._now()
                current = self._monotonic()
                if self.state == "running":
                    elapsed = current - self._last_step_mono
                    self.plant.advance(elapsed, setpoint)
                    self._last_step_mono = current
                else:
                    self.plant.T_j_set = setpoint
                self.client.write_process(self.plant.process_measurements())
                self.heartbeat_at, self._heartbeat_mono = self._now(), self._monotonic()
                if self.state == "running":
                    self._sample_growth()
                if current - self._last_checkpoint_mono >= 15:
                    self._persist()
            except Exception as exc:
                self.state, self.error = "error", f"{type(exc).__name__}: {exc}"
                try:
                    self._persist()
                except Exception as persist_error:
                    self.error += f"; checkpoint failed: {persist_error}"
                try:
                    self.client.close()
                except Exception:
                    pass

    def status(self) -> dict[str, Any]:
        with self._lock:
            plant = self.plant
            return {
                "run_id": self.run_id, "experiment_started_at": self.experiment_started_at,
                "running": self.state == "running", "status": self.state, "error": self.error,
                "source": SOURCE, "elapsed_s": plant.elapsed_s if plant else 0,
                "sample_seq": self.sample_seq, "observed_at": self.observed_at,
                "G_true_m_s": self.G_true_m_s, "G_measured_m_s": self.G_measured_m_s,
                "heartbeat_at": self.heartbeat_at,
                "observed_setpoint_K": self.observed_setpoint_K,
                "setpoint_observed_at": self.setpoint_observed_at,
                "sample_age_s": None if self._heartbeat_mono is None else max(0, self._monotonic() - self._heartbeat_mono),
                "seed_event_ids": list(plant.seed_event_ids) if plant else [],
                "seed_mass_kg": plant.seed_mass_kg if plant else 0,
                "T_K": plant.T if plant else None, "T_j_K": plant.T_j if plant else None,
                "T_j_set_K": plant.T_j_set if plant else None, "c": plant.c if plant else None,
                "sigma_true": float(calc_relative_sigma(plant.c, plant.T)) if plant else None,
                "count_middle": 17.0, "count_source": "constant transport fixture, not FBRM",
                "server_identity": self.client.server_identity,
                "noise": {"sample_period_s": 15, "T_std_K": .01, "c_std": .0002, "G_std_m_s": 1e-9,
                          "injected_into_physics": False, "seed": plant.seed if plant else None},
                "updated_at": self._now(),
            }

    def start_worker(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._shutdown.clear()
        def run() -> None:
            while not self._shutdown.wait(1.0):
                self.tick()
        self._thread = threading.Thread(target=run, name="prosys-test-plant", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._shutdown.set()
        if self._thread:
            self._thread.join(timeout=8)
        with self._lock:
            self._stop_locked()
            self.client.close()


__all__ = ["ExternalPlant", "PlantService", "SOURCE"]
