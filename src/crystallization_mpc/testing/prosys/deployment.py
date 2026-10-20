"""Generate an isolated, localhost-only Podman test stack without reading .env.

The real Prosys desktop application is external. No substitute OPC UA server is
started here. Generated credentials and state stay below .runtime/prosys-test.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from urllib.parse import urlsplit

import yaml

PROJECT = "mpcrystal-prosys"
PORTS = {"central": (18000, 8000), "controller": (18002, 8002),
         "gsensor": (18001, 8001), "plant": (18010, 8010),
         "influxdb": (8088, 8086), "grafana": (3001, 3000)}
NODE_IDS = {"T": "Local.iControl.Experiment1.Trends.Tr.Value",
            "T_J": "Local.iControl.Experiment1.Trends.Tj.Value",
            "C": "Local.iControl.Experiment1.Trends.load.Value",
            "COUNT_MIDDLE": "Local.iControl.Experiment1.Trends.count.Value",
            "T_J_SET": "iCache.Temperature1"}


def paper_parameter_document(root: Path) -> dict:
    """Freeze paper initial values in this test profile, not production defaults."""
    document = yaml.safe_load((root / "params_default.yaml").read_text(encoding="utf-8"))
    overrides = {"shared": {"dt": 15.0, "dt_G": 15.0},
                 "controller": {"params.k_0": 2e15}}
    for section, values in overrides.items():
        entries = document["params"][section]
        for key, value in values.items():
            found = next((entry for entry in entries if entry["key"] == key), None)
            if found is None:
                entries.append({"key": key, "default": value})
            else:
                found["default"] = value
    return document


def write_paper_defaults(root: Path, state: Path) -> Path:
    target = state / "params_paper.yaml"
    document = paper_parameter_document(root)
    if target.exists():
        if yaml.safe_load(target.read_text(encoding="utf-8")) != document:
            raise FileExistsError("Existing paper defaults differ; preserve and inspect them.")
        return target
    with target.open("x", encoding="utf-8") as stream:
        yaml.safe_dump(document, stream, sort_keys=False)
    return target


def validate_endpoint(endpoint: str) -> str:
    url = urlsplit(endpoint)
    if (url.scheme != "opc.tcp" or not url.port or url.username or url.password
            or url.query or url.fragment
            or url.hostname not in {"localhost", "127.0.0.1", "host.containers.internal"}):
        raise ValueError("Only an explicit local Prosys opc.tcp endpoint is allowed by this test profile.")
    # Inside containers localhost is the container, not the desktop host.
    return endpoint.replace(url.hostname, "host.containers.internal", 1)


def compose_document(root: Path, state: Path, endpoint: str, token: str,
                     password: str, namespace: int = 2) -> dict:
    endpoint = validate_endpoint(endpoint)
    if not isinstance(namespace, int) or isinstance(namespace, bool) or namespace < 1:
        raise ValueError("Namespace index must be a positive integer.")
    image = "localhost/mpcrystal-prosys-app:local"
    influx = {"INFLUX_URL": "http://influxdb:8086", "INFLUX_ORG": "prosys-test",
              "INFLUX_BUCKET": "crystallization", "INFLUX_TOKEN": token}
    rabbit = {"RABBIT_URL": "amqp://guest:guest@rabbitmq:5672/%2F",
              "RABBIT_EXCHANGE": "idp.bus", "RABBIT_HEARTBEAT_SEC": "0"}
    device = {"OPCUA_ENDPOINT": endpoint, "OPCUA_NAMESPACE_INDEX": str(namespace),
              **{f"OPCUA_{key}_NODE": value for key, value in NODE_IDS.items()},
              "OPCUA_AUTH_METHOD": "Anonymous", "OPCUA_SECURITY_POLICY": "None",
              "OPCUA_SECURITY_MODE": "None", "OPCUA_USERNAME": "", "OPCUA_PASSWORD": "",
              "OPCUA_CERTIFICATE": "", "OPCUA_PRIVATE_KEY": "", "OPCUA_SERVER_CERTIFICATE": "",
              "OPCUA_TIMEOUT_S": "5.0", "OPCUA_JACKET_MIN_K": "253.15",
              "OPCUA_JACKET_MAX_K": "453.15", "OPCUA_VERIFY_WRITE": "true",
              "OPCUA_VERIFY_TOLERANCE_K": "0.05"}
    shared = {**rabbit, **influx, "EXPERIMENT_ROOT": "/data/experiments",
              "PROSYS_TEST_MODE": "1",
              "PARAMS_DEFAULT_FILE": "/app/params_default.yaml",
              "PARAMS_FILE": "/data/experiments/_system/params_runtime.yaml",
              "PARAM_META_FILE": "/app/param_meta.yaml", "UI_MODE": "development"}
    experiments = f"{state / 'experiments'}:/data/experiments"

    def app(name: str, module: str, env: dict) -> dict:
        service = {"image": image, "command": ["uvicorn", module, "--host", "0.0.0.0",
                    "--port", str(PORTS[name][1])], "environment": {**shared, **env},
                   "volumes": [experiments], "restart": "no"}
        service["ports"] = [f"127.0.0.1:{PORTS[name][0]}:{PORTS[name][1]}"]
        return service

    services = {}
    services["central"] = app("central", "crystallization_mpc.apps.central.ui.app:web_app", {
        "PARAMS_DEFAULT_FILE": "/data/test/params_paper.yaml",
        "RABBIT_QUEUE": "central.in", "CONTROLLER_STATUS_URL": "http://controller:8002/api/status",
        "GSENSOR_STATUS_URL": "http://gsensor:8001/api/status",
        "CENTRAL_RUNTIME_INFLUX_ENABLED": "true", "CONTROLLER_INFLUX_ENABLED": "true",
        "CONTROLLER_INFLUX_URL": "http://influxdb:8086", "EXPERIMENT_EXPORT_ENABLED": "true",
        "EXPERIMENT_HOST_ROOT_DISPLAY": str(state / "experiments"),
        "CONTROLLER_PROCESS_ENABLED": "true", "PROCESS_DEVICE_ID": "prosys-test",
        "OPCUA_WRITER_AUDIT_DB": "/data/opcua_writer/commands.sqlite3", "TELEGRAF_INTERVAL": "1s",
        "GRAFANA_EXPERIMENT_URL": "http://127.0.0.1:3001/d/crystallization-mpc-analysis/crystallization-mpc-analysis"})
    services["central"]["build"] = {"context": str(root), "dockerfile": "services/mpc/Dockerfile"}
    services["central"]["volumes"].append("writer_data:/data/opcua_writer:ro")
    services["central"]["volumes"].append(f"{state / 'params_paper.yaml'}:/data/test/params_paper.yaml:ro")
    services["controller"] = app("controller", "crystallization_mpc.apps.controller.app:web_app", {
        "RABBIT_QUEUE": "controller.in",
        "CONTROLLER_ADAPTER": "crystallization_mpc.apps.controller.algorithm.integration:CrystallizationControllerAdapter",
        "CONTROLLER_PROCESS_ENABLED": "true", "CONTROLLER_PROCESS_WRITE_ENABLED": "true",
        "CONTROLLER_INFLUX_ENABLED": "true", "CONTROLLER_INFLUX_URL": "http://influxdb:8086",
        "PROCESS_DEVICE_ID": "prosys-test", "PROCESS_NAMESPACE_INDEX": str(namespace),
        **{f"PROCESS_{key}_NODE": value for key, value in NODE_IDS.items()},
        "PROCESS_SAMPLE_MAX_AGE_S": "5", "PROCESS_STARTUP_TIMEOUT_S": "10",
        "PROCESS_COMMAND_TTL_S": "5", "PROCESS_WRITE_TIMEOUT_S": "5",
        "PROCESS_VERIFY_TOLERANCE_K": "0.05"})
    services["gsensor"] = app("gsensor", "crystallization_mpc.testing.prosys.gsensor:app", {
        "PROSYS_PLANT_URL": "http://plant:8010", "PROSYS_G_SAMPLE_INTERVAL_S": "15",
        "PROSYS_G_WARMUP_S": "120", "PROSYS_PLANT_MAX_AGE_S": "5",
        "GSENSOR_INFLUX_ENABLED": "true"})
    services["plant"] = app("plant", "crystallization_mpc.testing.prosys.plant_app:web_app", {
        **device, "PROSYS_TEST_STATE_FILE": "/data/plant/state.json"})
    services["plant"]["volumes"] = [f"{state / 'plant'}:/data/plant"]
    services["opcua-writer"] = {
        "image": image, "command": ["python", "-m", "crystallization_mpc.apps.opcua_writer"],
        "environment": {**shared, **device, "OPCUA_WRITER_QUEUE": "opcua_writer.in",
                        "OPCUA_WRITER_JOURNAL": "/data/commands.sqlite3"},
        "volumes": [experiments, "writer_data:/data"], "restart": "no"}
    services["telegraf"] = {
        "image": "docker.io/library/telegraf:1.40.1",
        # OPC UA/AMQP need neither ICMP nor privileged-port capabilities.
        # Skip the image's root setcap/setpriv path under rootless Podman.
        "user": "telegraf:telegraf", "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "command": ["telegraf", "--config", "/etc/telegraf/telegraf.conf", "--config", "/etc/telegraf/opcua.conf"],
        "environment": {**influx, **rabbit, **device, "PROCESS_DEVICE_ID": "prosys-test",
                        "TELEGRAF_INTERVAL": "1s", "TELEGRAF_FLUSH_INTERVAL": "1s"},
        "volumes": [f"{root / 'config/telegraf/telegraf.conf'}:/etc/telegraf/telegraf.conf:ro",
                    f"{root / 'config/telegraf/opcua.conf'}:/etc/telegraf/opcua.conf:ro",
                    "telegraf_data:/var/lib/telegraf"], "restart": "no"}
    services["rabbitmq"] = {"image": "docker.io/library/rabbitmq:3-management", "hostname": "prosys-rabbitmq",
        "ports": ["127.0.0.1:5674:5672", "127.0.0.1:15674:15672"], "volumes": ["rabbit_data:/var/lib/rabbitmq"],
        "healthcheck": {"test": ["CMD", "rabbitmq-diagnostics", "-q", "check_running"],
                        "interval": "5s", "timeout": "5s", "retries": 20}, "restart": "no"}
    services["influxdb"] = {"image": "docker.io/library/influxdb:2.7", "ports": ["127.0.0.1:8088:8086"],
        "environment": {"DOCKER_INFLUXDB_INIT_MODE": "setup", "DOCKER_INFLUXDB_INIT_USERNAME": "prosys-test",
                        "DOCKER_INFLUXDB_INIT_PASSWORD": password, "DOCKER_INFLUXDB_INIT_ORG": influx["INFLUX_ORG"],
                        "DOCKER_INFLUXDB_INIT_BUCKET": influx["INFLUX_BUCKET"], "DOCKER_INFLUXDB_INIT_ADMIN_TOKEN": token},
        "volumes": ["influx_data:/var/lib/influxdb2"], "healthcheck": {
            "test": ["CMD", "influx", "ping", "-host", "http://localhost:8086"],
            "interval": "5s", "timeout": "5s", "retries": 20}, "restart": "no"}
    services["grafana"] = {"image": "docker.io/grafana/grafana:10.4.3", "ports": ["127.0.0.1:3001:3000"],
        "environment": {**influx, "GF_AUTH_ANONYMOUS_ENABLED": "true", "GF_AUTH_ANONYMOUS_ORG_ROLE": "Viewer",
                        "GF_SECURITY_ADMIN_PASSWORD": password,
                        "GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH": "/var/lib/grafana/dashboards/crystallization-mpc.json"},
        "volumes": ["grafana_data:/var/lib/grafana",
            f"{root / 'grafana/provisioning/datasources'}:/etc/grafana/provisioning/datasources:ro",
            f"{root / 'grafana/provisioning/dashboards'}:/etc/grafana/provisioning/dashboards:ro",
            f"{root / 'grafana/dashboards'}:/var/lib/grafana/dashboards:ro"], "restart": "no"}
    for name in ("central", "controller", "gsensor", "opcua-writer", "telegraf"):
        services[name]["depends_on"] = {"rabbitmq": {"condition": "service_healthy"},
                                       "influxdb": {"condition": "service_healthy"}}
    return {"name": PROJECT, "services": services,
            "volumes": {v: {} for v in ("rabbit_data", "influx_data", "grafana_data", "writer_data", "telegraf_data")}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "build", "up", "status", "stop"))
    parser.add_argument("--endpoint")
    parser.add_argument("--namespace", type=int, default=2)
    parser.add_argument("--services", nargs="*", default=[])
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[4]
    state = root / ".runtime/prosys-test"
    compose = state / "compose.yaml"
    if state.resolve() != state:
        raise ValueError("Refusing a test state directory containing a symlink.")
    if args.action == "prepare":
        if not args.endpoint:
            parser.error("prepare requires the actual --endpoint of the local Prosys test server")
        if compose.exists():
            raise FileExistsError("Existing test deployment retained; stop and inspect it instead of overwriting.")
        state.mkdir(parents=True, exist_ok=True)
        for name in ("experiments", "plant", "reports"):
            (state / name).mkdir(exist_ok=True)
        token, password = secrets.token_urlsafe(36), secrets.token_urlsafe(18)
        document = compose_document(root, state, args.endpoint, token, password, args.namespace)
        write_paper_defaults(root, state)
        with compose.open("x", encoding="utf-8") as stream:
            os.chmod(compose, 0o600)
            yaml.safe_dump(document, stream, sort_keys=False)
        with (state / "telemetry.json").open("x", encoding="utf-8") as stream:
            os.chmod(stream.name, 0o600)
            json.dump({"url": "http://127.0.0.1:8088", "org": "prosys-test",
                       "bucket": "crystallization", "token": token}, stream)
        print(f"Prepared isolated test configuration: {compose}")
        return 0
    if not compose.is_file():
        parser.error("Run prepare first; no production .env or compose file is used.")
    command = [sys.executable, "-m", "podman_compose", "-p", PROJECT, "-f", str(compose)]
    command += {"build": ["build", "central"], "up": ["up", "-d"],
                "status": ["ps"], "stop": ["stop"]}[args.action]
    if args.action in {"up", "stop"}:
        command.extend(args.services)
    return subprocess.call(command, cwd=root)


if __name__ == "__main__":
    raise SystemExit(main())
