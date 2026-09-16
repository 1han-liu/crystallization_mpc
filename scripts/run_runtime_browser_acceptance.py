#!/usr/bin/env python3
"""Isolated browser -> Central HTTP -> real RabbitMQ -> numerical Controller test.

Only test-owned processes/queues are removed. Existing services and images are
never stopped or touched. Credentials are read without evaluating shell code.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def rabbit_url(path: Path) -> str:
    for line in path.read_text().splitlines():
        line = line.strip().removeprefix("export ")
        if line.startswith("RABBIT_URL="):
            parts = shlex.split(line.partition("=")[2], comments=True)
            if len(parts) != 1 or not parts[0].startswith(("amqp://", "amqps://")):
                raise ValueError("Invalid RABBIT_URL assignment.")
            return parts[0]
    raise ValueError("RABBIT_URL not found in the selected environment file.")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def http_json(url: str, body=None):
    request = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.load(response)


def child(role: str, output: Path, port: int):
    import uvicorn
    from crystallization_mpc.apps.controller.algorithm.integration import CrystallizationControllerAdapter
    from crystallization_mpc.apps.controller.config import ControllerSettings
    from crystallization_mpc.apps.controller.service import ControllerService
    from fastapi import FastAPI
    from crystallization_mpc.apps.central.run_configuration import RunConfiguration

    exchange = os.environ["ACCEPTANCE_EXCHANGE"]
    if role == "controller":
        service = ControllerService(ControllerSettings(
            rabbit_url=os.environ["RABBIT_URL"], rabbit_exchange=exchange,
            rabbit_queue=exchange + ".controller", adapter_spec=None,
            opcua_enabled=False, opcua_write_enabled=False, opcua_endpoint=None,
            influx_enabled=os.getenv("CONTROLLER_INFLUX_ENABLED") == "true",
            influx_url=os.getenv("CONTROLLER_INFLUX_URL", "http://unused"),
            influx_org=os.getenv("CONTROLLER_INFLUX_ORG", "unused"),
            influx_bucket=os.getenv("CONTROLLER_INFLUX_BUCKET", "unused"),
            influx_token=os.getenv("CONTROLLER_INFLUX_TOKEN"), experiment_root=str(output / "controller"),
        ), adapter=CrystallizationControllerAdapter())

        @asynccontextmanager
        async def lifespan(app):
            service.start()
            try:
                yield
            finally:
                service.stop()

        app = FastAPI(lifespan=lifespan)
        app.get("/api/status")(service.status)
        app.get("/api/runtime-history")(service.runtime_history_page)
    else:
        from crystallization_mpc.apps.central.ui import app as module
        service = module.CentralService(publisher=module.CentralApp(
            url=os.environ["RABBIT_URL"], exchange=exchange, queue_name=exchange + ".central",
        ))
        module.service = service
        # Only the first start prepares a test run. Restarts use the same
        # immutable startup files and per-run request journal.
        if service.experiments.current_run_id() is None:
            service.update_run_configuration(RunConfiguration(
                run_type="simulation", growth_rate_source="simulated",
            ).to_dict())
            defaults = service.params_payload()
            defaults["controller"]["c_init"] = 0.4  # Isolated valid-input fixture only.
            defaults["controller"]["sigma_set"] = 0.035
            service.save_params(module.ParamsUpdate(**defaults))
            service.create_experiment(label="Isolated Central runtime acceptance")
            service.start_experiment()
        app = module.web_app

        @app.post("/__acceptance__/" + os.environ["ACCEPTANCE_TOKEN"] + "/disconnect")
        def disconnect_test_publisher():
            # Test-only server route, never mounted by the production entrypoint.
            # Break only this producer's TCP destination, not the shared broker.
            with service._lock:
                service.publisher.close()
                service.publisher.url = "amqp://127.0.0.1:1/%2F?socket_timeout=1&stack_timeout=2"
            return {"disconnected": True}

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def run(args):
    import pika

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    url = rabbit_url(args.rabbit_env)
    exchange = "runtime.acceptance." + uuid4().hex
    ports = {"controller": free_port(), "central": free_port()}
    while ports["controller"] == ports["central"]:
        ports["central"] = free_port()
    env = dict(os.environ)
    env.update({
        "RABBIT_URL": url, "ACCEPTANCE_EXCHANGE": exchange, "ACCEPTANCE_TOKEN": uuid4().hex,
        "EXPERIMENT_ROOT": str(output / "central"),
        "PARAMS_FILE": str(output / "params_runtime.yaml"),
        "RUN_CONFIGURATION_FILE": str(output / "run_configuration.json"),
        "PARAMS_DEFAULT_FILE": str(ROOT / "params_default.yaml"),
        "PARAM_META_FILE": str(ROOT / "param_meta.yaml"),
        "OPERATION_META_FILE": str(ROOT / "operation_meta.yaml"),
        "CONTROLLER_STATUS_URL": f"http://127.0.0.1:{ports['controller']}/api/status",
        "CONTROLLER_OPCUA_ENABLED": "false", "CONTROLLER_OPCUA_WRITE_ENABLED": "false",
        "CONTROLLER_INFLUX_ENABLED": "false", "UI_MODE": "development",
        "ACCEPTANCE_URL": f"http://127.0.0.1:{ports['central']}",
        "ACCEPTANCE_OUTPUT": str(output), "NODE_PATH": str(args.node_modules),
        "CHROME_PATH": args.chrome,
    })
    if os.getenv("ACCEPTANCE_TELEMETRY_ENV"):
        from crystallization_mpc.infra.influxdb.local_config import load_telemetry_env
        env.update(load_telemetry_env(Path(os.environ["ACCEPTANCE_TELEMETRY_ENV"])))
        env["CENTRAL_RUNTIME_INFLUX_ENABLED"] = "true"
    processes, logs = {}, []
    report = {"status": "FAIL", "ports": ports, "exchange": exchange,
              "scope": "Real browser, real HTTP, real RabbitMQ, real numerical algorithm. No GSensor/images/devices.",
              "cleanup": {}}

    def stop(role):
        process = processes.pop(role, None)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        return process.returncode if process else None

    def start(role):
        log_path = output / f"{role}-{time.time_ns()}.log"
        log = os.fdopen(os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w")
        logs.append(log)
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--service", role,
             "--output", str(output), "--port", str(ports[role])],
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        processes[role] = process
        endpoint = "/api/status" if role == "controller" else "/api/system/status"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"{role} exited; inspect its private local log.")
            try:
                result = http_json(f"http://127.0.0.1:{ports[role]}{endpoint}")
                if role != "controller" or result.get("consumer", {}).get("status") == "consuming":
                    return
            except (OSError, ValueError):
                pass
            time.sleep(0.15)
        raise RuntimeError(f"{role} readiness timeout.")

    driver = None
    try:
        # Explicit connection/authentication check; do not print its URL.
        connection = pika.BlockingConnection(pika.URLParameters(url))
        connection.close()
        start("controller")
        start("central")
        print("Isolated Central and Controller ready; browser acceptance starting.", flush=True)
        driver = subprocess.Popen(
            ["node", str(ROOT / "tests/central/runtime_browser_acceptance.cjs")],
            cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        processes["browser"] = driver
        for line in driver.stdout:
            if line.startswith("CONTROL "):
                operation = json.loads(line[len("CONTROL "):])
                action = operation["action"]
                if action == "disconnect-publisher":
                    reply = http_json(env["ACCEPTANCE_URL"] + "/__acceptance__/" +
                                      env["ACCEPTANCE_TOKEN"] + "/disconnect", {})
                elif action in {"database-offline", "database-online"} and env.get("ACCEPTANCE_DB_FAULT_FILE"):
                    fault = Path(env["ACCEPTANCE_DB_FAULT_FILE"])
                    if action == "database-offline":
                        fault.write_text("offline")
                    else:
                        fault.unlink(missing_ok=True)
                    reply = {"ok": True}
                elif action in {"restart-central", "restart-controller", "stop-controller", "start-controller"}:
                    role = "central" if "central" in action else "controller"
                    if action.startswith(("stop-", "restart-")):
                        stop(role)
                    if action.startswith(("start-", "restart-")):
                        start(role)
                    reply = {"ok": True}
                else:
                    raise ValueError("Unknown browser control operation.")
                driver.stdin.write(json.dumps(reply) + "\n")
                driver.stdin.flush()
            else:
                # Browser driver emits only test descriptions and assertions.
                print(line.rstrip(), flush=True)
        if driver.wait(timeout=5) != 0:
            raise RuntimeError("Browser acceptance failed; inspect browser-report.json.")
        report["browser"] = json.loads((output / "browser-report.json").read_text())
        report["status"] = report["browser"]["status"]
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc).replace(url, "[redacted broker URL]")
        print(report["error"], flush=True)
    finally:
        for role in ("browser", "central", "controller"):
            stop(role)
        for log in logs:
            log.close()
        try:
            connection = pika.BlockingConnection(pika.URLParameters(url))
            channel = connection.channel()
            for role in ("central", "controller"):
                channel.queue_delete(queue=exchange + "." + role)
            channel.exchange_delete(exchange=exchange)
            connection.close()
            report["cleanup"]["broker_resources"] = "PASS: only this test's queues/exchange removed"
        except Exception as exc:
            report["cleanup"]["broker_resources"] = type(exc).__name__
            report["status"] = "FAIL"
        report["cleanup"]["owned_processes"] = "PASS" if not processes else "FAIL"
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"Acceptance {report['status']}: {output / 'report.json'}", flush=True)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".runtime/central-runtime-controls" /
                        datetime.now(timezone.utc).strftime("browser-%Y%m%dT%H%M%S%fZ"))
    parser.add_argument("--rabbit-env", type=Path, default=ROOT / ".runtime/rabbitmq-debug/runtime.env")
    parser.add_argument("--node-modules", type=Path, default=Path.home() /
                        ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules")
    parser.add_argument("--chrome", default=shutil.which("google-chrome") or "/usr/bin/chromium")
    parser.add_argument("--service", choices=["central", "controller"])
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if args.service:
        child(args.service, args.output, args.port)
    else:
        sys.exit(run(args))
