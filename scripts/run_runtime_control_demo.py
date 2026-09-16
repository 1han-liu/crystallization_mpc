#!/usr/bin/env python3
"""Reproducible four-phase numerical demonstration; no images or device I/O."""
from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import platform
import shutil
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import scipy

from crystallization_mpc.apps.controller.algorithm.integration import CrystallizationControllerAdapter
from crystallization_mpc.apps.controller.config import ControllerSettings
from crystallization_mpc.apps.controller.service import ControllerService
from crystallization_mpc.messaging.controller_runtime import ControllerRuntimeUpdatePayload

PARAMETERS = {
    "run_type": "simulation", "growth_rate_source": "simulated",
    "c_init": 0.4, "sigma_set": 0.035, "G_set": 3e-8, "dt": 5.0,
}
CHANGES = {
    11: {"adaptation_enabled": True, "adaptation_mode": "E_A"},
    51: {"control_target": "G"},
    52: {"adaptation_mode": "E_A_and_k_0"},
    53: {"adaptation_mode": "all"},
    61: {"G_set": 4e-8},
    63: {"adaptation_enabled": False},
    69: {"adaptation_enabled": True},
    75: {"control_target": "sigma", "sigma_set": 0.04},
}


def run_demo(directory: Path, mode: str) -> dict:
    directory.mkdir(parents=True, exist_ok=False)
    settings = ControllerSettings(
        rabbit_url="amqp://unused/", rabbit_exchange="unused", rabbit_queue="unused",
        adapter_spec=None, opcua_enabled=False, opcua_endpoint=None,
        influx_enabled=False, influx_url="http://unused", influx_org="unused",
        influx_bucket="unused", experiment_root=str(directory / "continuous"),
    )
    service = ControllerService(settings, adapter=CrystallizationControllerAdapter())
    params = {**PARAMETERS, "mode": mode}
    service._apply_parameters({"version": 1, "params": params})
    service._start_experiment({
        "run_id": f"runtime-demo-{mode}", "parameter_version": 1,
        "started_at": "2026-09-10T12:00:00Z",
    })
    original = copy.deepcopy(service.parameters)
    rows, violations, events = [], [], []
    checkpoints = {}
    restored = None
    kinetic_at_disable = None
    fits_at_disable = None
    fitted_modes = set()
    recovery_comparisons = 0
    for tick in range(1, 81):
        if tick == 11:
            response = service.on_message({
                "src": "central", "dst": "controller", "msg_type": "command",
                "name": "controller.add_seed",
                "payload": {"run_id": service.current_run_id, "event_id": "demo-seed",
                            "added_at": "2026-09-10T12:00:50Z"},
            })
            if not response["accepted"]:
                violations.append({"tick": tick, "check": "seed", "result": response})
        if tick in CHANGES:
            command = ControllerRuntimeUpdatePayload(
                service.current_run_id, f"phase-change-{tick}", service.runtime_revision,
                CHANGES[tick], f"simulation-tick-{tick}",
            )
            before = service.adapter.export_state()
            envelope = {"src": "central", "dst": "controller", "msg_type": "command",
                        "name": "controller.runtime.update", "payload": command.to_dict()}
            response = service.on_message(envelope)
            events.append(response)
            after = service.adapter.export_state()
            for key in before.keys() - {"params", "adaptation"}:
                if before[key] != after[key]:
                    violations.append({"tick": tick, "check": f"update_reset_{key}"})
            if not response["accepted"] or response.get("effective_tick") != tick:
                violations.append({"tick": tick, "check": "application", "result": response})
            if restored:
                other = restored.on_message(envelope)
                if other.get("configuration") != response.get("configuration"):
                    violations.append({"tick": tick, "check": "recovered_command"})

        algorithm = service.adapter.controller
        if tick == 63:
            kinetic_at_disable = {key: algorithm.params[key] for key in ("E_A", "k_0", "n")}
            fits_at_disable = algorithm.adaptation_status()["fit_count"]
        started = time.perf_counter()
        service._control_tick_once(now=tick * 5.0)
        duration = time.perf_counter() - started
        record = service.last_control_output
        output = record["result"]
        diagnostic = algorithm.adaptation_status()
        if diagnostic["last_status"] == "fit_succeeded":
            fitted_modes.add(diagnostic["last_mode"])
        if not output["valid"]:
            violations.append({"tick": tick, "check": "valid_output", "error": output["error"]})
        for key, value in output.items():
            if isinstance(value, float) and not math.isfinite(value):
                violations.append({"tick": tick, "check": f"finite_{key}"})
        if output["valid"]:
            if not algorithm.params["T_j_min"] <= output["T_j_set"] <= algorithm.params["T_j_max"]:
                violations.append({"tick": tick, "check": "jacket_bounds"})
            if not algorithm.params["dT_dt_min"] <= output["dT_dt_set"] <= algorithm.params["dT_dt_max"]:
                violations.append({"tick": tick, "check": "gradient_bounds"})
            if output["target_set"] != algorithm.params["target_set"]:
                violations.append({"tick": tick, "check": "actual_target_telemetry"})
        if 63 <= tick <= 68:
            if kinetic_at_disable != {key: algorithm.params[key] for key in kinetic_at_disable}:
                violations.append({"tick": tick, "check": "disabled_parameters_changed"})
            if fits_at_disable != diagnostic["fit_count"]:
                violations.append({"tick": tick, "check": "disabled_fitting"})
        if tick in (10, 50, 60, 62, 68, 70, 80):
            checkpoints[tick] = copy.deepcopy(diagnostic)
        if restored:
            restored._control_tick_once(now=tick * 5.0)
            recovery_comparisons += 1
            if restored.last_control_output["result"] != output:
                violations.append({"tick": tick, "check": "recovery_trajectory"})
        rows.append({
            "tick": tick, "phase": 1 if tick <= 10 else 2 if tick <= 50 else 3 if tick <= 60 else 4,
            "mode": mode, "runtime_revision": service.runtime_revision,
            **service.runtime_configuration_current, **output,
            "fit_count": diagnostic["fit_count"], "fit_failure_count": diagnostic["failure_count"],
            "duration_s": duration,
        })
        if tick == 70:
            with service._lock:
                service._try_persist_state_locked()
            recovery_root = directory / "recovered"
            recovery_root.mkdir()
            shutil.copy2(service.state_path, recovery_root / service.state_path.name)
            restored = ControllerService(replace(settings, experiment_root=str(recovery_root)),
                                         adapter=CrystallizationControllerAdapter())
            if restored.recovery_status != "restored":
                violations.append({"tick": tick, "check": "restore", "error": restored.recovery_error})

    for check, passed in {
        "phase1_no_fit": checkpoints[10]["fit_count"] == 0,
        "phase2_E_A_fit": checkpoints[50]["fit_count"] > 0 and checkpoints[50]["last_mode"] == "E_A",
        "phase3_all_fit": checkpoints[60]["last_mode"] == "all" and checkpoints[60]["fit_count"] > checkpoints[50]["fit_count"],
        "phase4_fit_resumed": checkpoints[70]["fit_count"] > checkpoints[68]["fit_count"],
        "same_startup_snapshot": service.parameters == original,
        "single_seed": service.seed_event_count == 1,
        "recovery_ten_ticks": recovery_comparisons == 10,
        "all_selected_modes_fitted": {"E_A", "E_A_and_k_0", "all"} <= fitted_modes,
        "OPCUA_disabled": not settings.opcua_enabled and not settings.opcua_write_enabled,
    }.items():
        if not passed:
            violations.append({"check": check})
    report = {
        "status": "FAIL" if violations else "PASS", "mode": mode,
        "scope": "Real numerical Controller; in-process commands; no RabbitMQ, images or device I/O.",
        "parameters": params, "checkpoints": checkpoints, "events": events,
        "violations": violations, "ticks": len(rows), "recovery_comparisons": recovery_comparisons,
        "versions": {"python": platform.python_version(), "numpy": np.__version__, "scipy": scipy.__version__},
        "noise_policy": "Existing deterministic per-tick RandomState seeds; unchanged dt=5 seconds.",
    }
    (directory / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    with (directory / "cycles.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".runtime/central-runtime-controls") /
                        datetime.now(timezone.utc).strftime("simulation-%Y%m%dT%H%M%S%fZ"))
    parser.add_argument("--mode", choices=["MPC", "PI", "both"], default="both")
    args = parser.parse_args()
    modes = ("MPC", "PI") if args.mode == "both" else (args.mode,)
    reports = [run_demo(args.output / mode, mode) for mode in modes]
    for report in reports:
        print(f"{report['mode']}: {report['status']} ({report['ticks']} ticks, "
              f"{report['recovery_comparisons']} recovery comparisons)")
        for violation in report["violations"]:
            print(json.dumps(violation))
    print(f"Reports: {args.output.resolve()}")
    return 1 if any(report["violations"] for report in reports) else 0


if __name__ == "__main__":
    sys.exit(main())
