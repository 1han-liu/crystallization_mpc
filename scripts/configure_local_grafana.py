#!/usr/bin/env python3
"""Provision the project's existing dashboards in loopback-only Podman services.

Does not start/stop simulations or delete experiments, volumes or containers.
Random credentials are kept in ignored mode-0600 local files, never printed.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / '.runtime/grafana-local'
NETWORK = 'mpcrystal-observability'
OWNER = str(ROOT)


def private_file(path, text):
    if path.is_symlink():
        raise RuntimeError('Refusing a credential symlink.')
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(text)


def private_env(path, values):
    private_file(path, ''.join(f'{key}={value}\n' for key, value in values.items()))


def podman(*args, check=True):
    return subprocess.run(['podman', *args], check=check, capture_output=True, text=True)


def api(path, body=None, token=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Token ' + token
    request = urllib.request.Request('http://127.0.0.1:8087' + path,
        data=json.dumps(body).encode() if body is not None else None, headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def wait_http(url, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.5)
    raise RuntimeError('Local service health timeout: ' + url)


def ensure_container(name, args):
    if podman('container', 'exists', name, check=False).returncode == 0:
        info = json.loads(podman('inspect', name).stdout)[0]
        if info['Config'].get('Labels', {}).get('mpcrystal.project') != OWNER:
            raise RuntimeError('Container exists but is not owned by this setup: ' + name)
        podman('start', name)
    else:
        podman('run', '-d', '--name', name, '--label', 'mpcrystal.project=' + OWNER,
               '--restart', 'unless-stopped', '--network', NETWORK, *args)


def run():
    STATE.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE, 0o700)
    credentials_path = STATE / 'credentials.json'
    if credentials_path.exists():
        credentials = json.loads(credentials_path.read_text())
    else:
        # Never invent new init credentials for an already populated volume.
        if podman('volume', 'exists', 'mpcrystal-final-influxdb-data', check=False).returncode == 0:
            raise RuntimeError('Existing Influx volume has no matching credentials; refusing initialization.')
        credentials = {'influx_admin_password': secrets.token_urlsafe(24),
                       'influx_admin_token': secrets.token_urlsafe(48),
                       'grafana_admin_password': secrets.token_urlsafe(24),
                       'org': 'crystallization-lab', 'bucket': 'crystallization'}
        private_file(credentials_path, json.dumps(credentials, indent=2) + '\n')
    if podman('network', 'exists', NETWORK, check=False).returncode:
        podman('network', 'create', NETWORK)
    for volume in ('mpcrystal-final-influxdb-data', 'mpcrystal-final-grafana-data'):
        if podman('volume', 'exists', volume, check=False).returncode:
            podman('volume', 'create', '--label', 'mpcrystal.project=' + OWNER, volume)
    init_env = STATE / 'influx-init.env'
    private_env(init_env, {
        'DOCKER_INFLUXDB_INIT_MODE': 'setup', 'DOCKER_INFLUXDB_INIT_USERNAME': 'admin',
        'DOCKER_INFLUXDB_INIT_PASSWORD': credentials['influx_admin_password'],
        'DOCKER_INFLUXDB_INIT_ADMIN_TOKEN': credentials['influx_admin_token'],
        'DOCKER_INFLUXDB_INIT_ORG': credentials['org'], 'DOCKER_INFLUXDB_INIT_BUCKET': credentials['bucket'],
    })
    ensure_container('mpcrystal-final-influxdb', [
        '--network-alias', 'influxdb', '-p', '127.0.0.1:8087:8086',
        '--env-file', str(init_env), '-v', 'mpcrystal-final-influxdb-data:/var/lib/influxdb2',
        'docker.io/library/influxdb:2.7',
    ])
    wait_http('http://127.0.0.1:8087/health')
    # /health can be ready on the temporary first-start server before setup ends.
    deadline = time.monotonic() + 60
    while True:
        try:
            org = next(x for x in api('/api/v2/orgs', token=credentials['influx_admin_token'])['orgs'] if x['name'] == credentials['org'])
            bucket = next(x for x in api('/api/v2/buckets', token=credentials['influx_admin_token'])['buckets'] if x['name'] == credentials['bucket'])
            break
        except (OSError, StopIteration):
            if time.monotonic() > deadline:
                raise RuntimeError('Influx initialization did not complete.')
            time.sleep(0.5)
    for action in ('read', 'write'):
        key = action + '_token'
        if key not in credentials:
            authorization = api('/api/v2/authorizations', {
                'orgID': org['id'], 'description': 'MPCrystal local ' + action,
                'permissions': [{'action': action, 'resource': {'type': 'buckets', 'id': bucket['id'], 'orgID': org['id']}}],
            }, credentials['influx_admin_token'])
            credentials[key] = authorization['token']
            private_file(credentials_path, json.dumps(credentials, indent=2) + '\n')
    private_env(STATE / 'controller.env', {
        'CONTROLLER_INFLUX_ENABLED': 'true', 'CONTROLLER_INFLUX_URL': 'http://127.0.0.1:8087',
        'CONTROLLER_INFLUX_ORG': credentials['org'], 'CONTROLLER_INFLUX_BUCKET': credentials['bucket'],
        'CONTROLLER_INFLUX_TOKEN': credentials['write_token'],
    })
    grafana_env = STATE / 'grafana.env'
    private_env(grafana_env, {
        'INFLUX_TOKEN': credentials['read_token'], 'INFLUX_ORG': credentials['org'],
        'INFLUX_BUCKET': credentials['bucket'], 'GF_SECURITY_ADMIN_USER': 'admin',
        'GF_SECURITY_ADMIN_PASSWORD': credentials['grafana_admin_password'],
        'GF_USERS_ALLOW_SIGN_UP': 'false', 'GF_AUTH_ANONYMOUS_ENABLED': 'true',
        'GF_AUTH_ANONYMOUS_ORG_ROLE': 'Viewer',
        'GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH': '/var/lib/grafana/dashboards/crystallization-mpc.json',
    })
    ensure_container('mpcrystal-final-grafana', [
        '-p', '127.0.0.1:3000:3000', '--env-file', str(grafana_env),
        '-v', 'mpcrystal-final-grafana-data:/var/lib/grafana',
        '-v', str(ROOT / 'grafana/provisioning/datasources') + ':/etc/grafana/provisioning/datasources:ro',
        '-v', str(ROOT / 'grafana/provisioning/dashboards') + ':/etc/grafana/provisioning/dashboards:ro',
        '-v', str(ROOT / 'grafana/dashboards') + ':/var/lib/grafana/dashboards:ro',
        'docker.io/grafana/grafana:10.4.3',
    ])
    wait_http('http://127.0.0.1:3000/api/health')
    print('InfluxDB ready: http://127.0.0.1:8087', flush=True)
    print('Grafana ready: http://127.0.0.1:3000/d/crystallization-mpc-analysis?from=now-15m&to=now&refresh=5s', flush=True)
    print('Local anonymous access is Viewer only; secrets are in .runtime/grafana-local (not in Git).', flush=True)


if __name__ == '__main__':
    try:
        run()
    except subprocess.CalledProcessError as exc:
        # Podman args contain file paths, not secrets. Do not dump inspect/env.
        print('Podman setup failed: ' + (exc.stderr or '')[-1500:])
        raise SystemExit(1)
