#!/usr/bin/env python3
"""Real isolated UI/RabbitMQ/Controller/InfluxDB/Grafana acceptance.

Uses a unique test bucket, token and ephemeral Grafana container. Cleans only
resources created by this invocation; leaves the user's services untouched.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from build_runtime_dashboard import build_dashboard

ROOT = Path(__file__).resolve().parents[1]


def main():
    identifier = uuid4().hex
    output = ROOT / ".runtime/run-configuration-acceptance" / identifier
    output.mkdir(parents=True, exist_ok=False)
    credentials = json.loads((ROOT / ".runtime/grafana-local/credentials.json").read_text())
    bucket_id = auth_id = None
    container = "mpcrystal-events-test-" + identifier
    container_created = False
    proxy = None
    report = {"status": "FAIL", "output": str(output)}

    def api(path, data=None, method=None):
        request = urllib.request.Request("http://127.0.0.1:8087" + path,
            data=json.dumps(data).encode() if data is not None else None, method=method,
            headers={"Content-Type": "application/json", "Authorization": "Token " + credentials["influx_admin_token"]})
        with urllib.request.urlopen(request, timeout=10) as response:
            text = response.read()
            return json.loads(text) if text else None

    def private(path, text):
        with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as stream:
            stream.write(text)

    try:
        org = next(item for item in api("/api/v2/orgs")["orgs"] if item["name"] == credentials["org"])
        bucket_name = "runtime-acceptance-" + identifier
        bucket_id = api("/api/v2/buckets", {"orgID": org["id"], "name": bucket_name})["id"]
        auth = api("/api/v2/authorizations", {"orgID": org["id"], "description": bucket_name,
                   "permissions": [{"action": action, "resource": {"type": "buckets", "id": bucket_id, "orgID": org["id"]}}
                                   for action in ("read", "write")]})
        auth_id = auth["id"]
        token = auth["token"]
        dashboard_dir = output / "dashboards"
        dashboard_dir.mkdir()
        (dashboard_dir / "crystallization-mpc.json").write_text(json.dumps(build_dashboard()))
        env_file = output / "grafana.env"
        private(env_file, f"INFLUX_TOKEN={token}\nINFLUX_ORG={credentials['org']}\nINFLUX_BUCKET={bucket_name}\n"
                "GF_AUTH_ANONYMOUS_ENABLED=true\nGF_AUTH_ANONYMOUS_ORG_ROLE=Viewer\nGF_USERS_ALLOW_SIGN_UP=false\n"
                f"GF_SECURITY_ADMIN_PASSWORD={uuid4().hex}\n")
        telemetry_file = output / "telemetry.env"
        fault_file = output / "database-offline"

        class DatabaseProxy(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Request headers may contain credentials.

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if fault_file.exists():
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b'{"message":"isolated acceptance outage"}')
                    return
                request = urllib.request.Request("http://127.0.0.1:8087" + self.path, data=body,
                    headers={key: value for key, value in self.headers.items() if key.lower() not in {"host", "content-length"}})
                try:
                    response = urllib.request.urlopen(request, timeout=5)
                except urllib.error.HTTPError as exc:
                    response = exc
                with response:
                    self.send_response(response.status)
                    self.end_headers()
                    self.wfile.write(response.read())

        proxy = ThreadingHTTPServer(("127.0.0.1", 0), DatabaseProxy)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        private(telemetry_file, f"CONTROLLER_INFLUX_ENABLED=true\nCONTROLLER_INFLUX_URL=http://127.0.0.1:{proxy.server_port}\n"
                f"CONTROLLER_INFLUX_ORG={credentials['org']}\nCONTROLLER_INFLUX_BUCKET={bucket_name}\nCONTROLLER_INFLUX_TOKEN={token}\n")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        command = ["podman", "run", "-d", "--rm", "--name", container,
                   "--label", "mpcrystal.acceptance=" + identifier, "--network", "mpcrystal-observability",
                   "-p", f"127.0.0.1:{port}:3000", "--env-file", str(env_file),
                   "-v", str(ROOT / "grafana/provisioning/datasources") + ":/etc/grafana/provisioning/datasources:ro",
                   "-v", str(ROOT / "grafana/provisioning/dashboards") + ":/etc/grafana/provisioning/dashboards:ro",
                   "-v", str(dashboard_dir) + ":/var/lib/grafana/dashboards:ro", "docker.io/grafana/grafana:10.4.3"]
        subprocess.run(command, check=True, capture_output=True)
        container_created = True
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 60
        while True:
            try:
                with urllib.request.urlopen(base + "/api/health", timeout=2) as response:
                    if response.status == 200:
                        break
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError("Isolated Grafana readiness timeout")
                time.sleep(.5)
        print("Isolated Grafana ready; unique test bucket created. Starting full-chain acceptance.", flush=True)
        environment = {**os.environ, "ACCEPTANCE_GRAFANA_URL": base, "ACCEPTANCE_INFLUX_BUCKET": bucket_name,
                       "ACCEPTANCE_TELEMETRY_ENV": str(telemetry_file), "ACCEPTANCE_DB_FAULT_FILE": str(fault_file)}
        process = subprocess.run([sys.executable, str(ROOT / "scripts/run_runtime_browser_acceptance.py"),
                                  "--output", str(output / "platform")], env=environment, cwd=ROOT)
        report["status"] = "PASS" if process.returncode == 0 else "FAIL"
        report["grafana_port"] = port
    except Exception as exc:
        report["error"] = type(exc).__name__  # Do not emit secret-bearing client exceptions.
    finally:
        if proxy is not None:
            proxy.shutdown()
            proxy.server_close()
        if container_created:
            subprocess.run(["podman", "stop", "--time", "3", container], check=True, capture_output=True)
        if auth_id:
            api("/api/v2/authorizations/" + auth_id, method="DELETE")
        if bucket_id:
            api("/api/v2/buckets/" + bucket_id, method="DELETE")
        report["cleanup"] = "Only this invocation's container, token and test bucket removed; test data deleted. Evidence retained locally."
        (output / "report.json").write_text(json.dumps(report, indent=2))
    print(f"Observability acceptance {report['status']}: {output / 'report.json'}", flush=True)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
