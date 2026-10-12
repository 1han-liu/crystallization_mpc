#!/usr/bin/env python3
"""Run one isolated four-phase accelerated simulation without old-data cleanup.

The current default Grafana dashboard is reused; this script never creates or
copies a dashboard, starts containers, enables hardware, or deletes old runs.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import sys
from datetime import datetime, timezone
from uuid import uuid4

from crystallization_mpc.apps.controller.acceptance import FourPhaseAcceptance
from crystallization_mpc.apps.controller.config import ControllerSettings
from crystallization_mpc.infra.influxdb.client import InfluxSettings
from crystallization_mpc.infra.influxdb.local_config import load_telemetry_env
from crystallization_mpc.infra.influxdb.write import InfluxWriter

ROOT = Path(__file__).resolve().parents[1]


def read_rabbit_url(path: Path) -> str:
    """Parse config as data, never shell source it or print credentials."""
    value = os.environ.get("RABBIT_URL")
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, raw = line.strip().removeprefix("export ").partition("=")
            if separator and key == "RABBIT_URL":
                tokens = shlex.split(raw, comments=True)
                if len(tokens) != 1:
                    raise ValueError("RabbitMQ configuration assignment is malformed.")
                value = tokens[0]
    # Transport is explicitly in-process for this controlled driver; RabbitMQ
    # credentials remain available in the settings, but no shared queue is used.
    return value or "amqp://guest:guest@127.0.0.1:5673/%2F"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--telemetry-env", type=Path, default=ROOT / ".runtime/grafana-local/controller.env")
    result.add_argument("--rabbit-env", type=Path, default=ROOT / ".runtime/rabbitmq-debug/runtime.env")
    result.add_argument("--output-parent", type=Path, default=ROOT / ".runtime/acceptance-refresh")
    result.add_argument("--max-minutes", type=float, default=120.0, help="Simulation minutes, never greater than 120.")
    result.add_argument("--progress-every", type=int, default=20)
    result.add_argument("--no-influx", action="store_true", help="Local smoke check only; cannot produce full acceptance PASS or Grafana plots.")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid4().hex[:8]
    run_id = "exp_refresh_" + suffix
    directory = args.output_parent.absolute() / run_id
    if args.output_parent.is_symlink() or args.output_parent.absolute().resolve() != args.output_parent.absolute():
        print("Refusing an acceptance output path containing a symbolic link.", file=sys.stderr)
        return 2
    writer = None
    try:
        telemetry = {} if args.no_influx else load_telemetry_env(args.telemetry_env)
        if not args.no_influx and telemetry.get("CONTROLLER_INFLUX_ENABLED") != "true":
            raise ValueError("A complete enabled local telemetry configuration is required for Grafana acceptance.")
        if not args.no_influx:
            writer = InfluxWriter(InfluxSettings(
                url=telemetry["CONTROLLER_INFLUX_URL"], token=telemetry["CONTROLLER_INFLUX_TOKEN"],
                org=telemetry["CONTROLLER_INFLUX_ORG"], bucket=telemetry["CONTROLLER_INFLUX_BUCKET"],
            ))
            # Reversible read-only readiness check before creating the run.
            if writer.client.health().status != "pass":
                raise RuntimeError("InfluxDB is not ready.")
        settings = ControllerSettings(
            rabbit_url=read_rabbit_url(args.rabbit_env), rabbit_exchange="mpcrystal.acceptance." + uuid4().hex,
            rabbit_queue="mpcrystal.acceptance." + uuid4().hex, adapter_spec=None,
            opcua_enabled=False, opcua_endpoint=None, opcua_write_enabled=False,
            influx_enabled=not args.no_influx, influx_url=telemetry.get("CONTROLLER_INFLUX_URL", ""),
            influx_org=telemetry.get("CONTROLLER_INFLUX_ORG", ""), influx_bucket=telemetry.get("CONTROLLER_INFLUX_BUCKET", ""),
            influx_token=telemetry.get("CONTROLLER_INFLUX_TOKEN"), experiment_root=str(directory / "controller"),
        )
        acceptance = FourPhaseAcceptance(settings, directory, run_id=run_id, writer=writer,
                                          max_minutes=args.max_minutes, progress_every=args.progress_every)
        print(f"Run ID: {run_id}\nArtifacts: {directory}\nSource: accelerated numerical simulation / simulated growth.", flush=True)
        report = acceptance.run()
        print(json.dumps({key: report[key] for key in ("run_id", "status", "endpoint_reason", "failure", "simulation_minutes", "default_grafana_url")}, indent=2), flush=True)
        return 0 if report["status"] == "PASS" else 1
    except Exception as exc:
        if writer is not None:
            writer.close()
        # A startup exception may contain an authenticated URL from a library;
        # print its class only. Detailed post-start errors live in run artifacts.
        print(f"Acceptance startup failed: {type(exc).__name__}. No old run was deleted.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
