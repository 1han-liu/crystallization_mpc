#!/usr/bin/env python3
"""Delete this launcher's finished sessions, then start a fresh manual simulation.

Only verified session-* directories under .runtime/manual-simulations are
removed, including their images, annotations, state and logs. No backups.
Other experiment directories are outside this command's cleanup scope.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit
from uuid import uuid4

from crystallization_mpc.apps.central.run_configuration import RunConfiguration, RunConfigurationStore
from crystallization_mpc.infra.influxdb.local_config import load_telemetry_env

ROOT = Path(__file__).resolve().parents[1]


def read_broker_url(env_path=None):
    """Explicit file > environment > portable config > legacy local config.

    Never execute an env file or echo a secret-bearing URL in an error.
    An explicitly chosen bad file must not silently use another broker.
    """
    value = None
    if env_path is None and 'RABBIT_URL' in os.environ:
        value = os.environ['RABBIT_URL']
    else:
        path = Path(env_path) if env_path is not None else ROOT / 'config/rabbitmq.env'
        if env_path is None and not path.exists():
            path = ROOT / '.runtime/rabbitmq-debug/runtime.env'
        try:
            assignments = []
            for line in path.read_text().splitlines():
                line = line.strip().removeprefix('export ')
                if line.startswith('RABBIT_URL='):
                    words = shlex.split(line.partition('=')[2], comments=True)
                    if len(words) != 1:
                        raise ValueError('Empty or malformed assignment')
                    assignments.append(words[0])
            if len(assignments) != 1:
                raise ValueError('Exactly one assignment is required')
            value = assignments[0]
        except (OSError, ValueError, UnicodeError):
            raise RuntimeError('RabbitMQ 配置缺失或不可读。复制 config/rabbitmq.env.example 为 config/rabbitmq.env 并填写连接地址，或使用 --rabbit-env FILE / RABBIT_URL。') from None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {'amqp', 'amqps'} or not parsed.hostname
                or any(c.isspace() for c in value) or any(c in value for c in '<>')
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            raise ValueError('Invalid URL')
    except (TypeError, ValueError):
        raise RuntimeError('RABBIT_URL 无效：需要完整 amqp:// 或 amqps:// 地址；特殊字符必须 URL 编码。') from None
    return value


def fetch(port, endpoint):
    with urllib.request.urlopen(f'http://127.0.0.1:{port}{endpoint}', timeout=2) as response:
        return json.load(response)


def cleanup_previous_sessions(parent: Path) -> list[str]:
    """Validate the entire list before deleting; refuse unknown/unfinished data."""
    if parent.name != 'manual-simulations' or parent.is_symlink() or parent.resolve() != parent.absolute():
        raise RuntimeError('拒绝清理：仿真根目录必须是原位置的普通目录，不能经过符号链接。')
    candidates = sorted(parent.glob('session-*'))
    for directory in candidates:
        marker = directory / 'launcher.json'
        if directory.is_symlink() or not directory.is_dir() or marker.is_symlink() or not marker.is_file():
            raise RuntimeError(f'拒绝清理未确认归属或尚未结束的目录：{directory}')
        try:
            report = json.loads(marker.read_text())
        except (OSError, ValueError) as exc:
            raise RuntimeError(f'拒绝清理，无法读取会话记录：{directory}') from exc
        if (not isinstance(report, dict) or report.get('directory') != str(directory)
                or not str(report.get('exchange', '')).startswith('mpcrystal.manual.')
                or 'transport_cleanup' not in report
                or report.get('finished', True) is not True):
            raise RuntimeError(f'拒绝清理未确认结束的本脚本会话：{directory}')
        for pid in report.get('child_pids', []):
            if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
                raise RuntimeError(f'会话进程记录无效：{directory}')
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                pass
            raise RuntimeError(f'会话进程 {pid} 仍存在，拒绝删除：{directory}')
    deleted = []
    for directory in candidates:
        shutil.rmtree(directory)  # Does not follow nested image symlinks.
        deleted.append(str(directory))
        print(f'已永久删除旧仿真会话（含其中图片和标注，无备份）：{directory}', flush=True)
    return deleted


def run(args):
    import pika

    for port in (args.central_port, args.controller_port):
        with socket.socket() as sock:
            # Match the server's bind semantics: a closed previous session in
            # TIME_WAIT is not a running process occupying the listening port.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(('127.0.0.1', port))
            except OSError:
                raise RuntimeError(f'端口 {port} 已占用。请先在旧 Central / Controller 终端按 Ctrl+C；本命令不会强杀其他进程。')
    if args.central_port == args.controller_port:
        raise ValueError('Central and Controller need different ports.')
    telemetry_env = load_telemetry_env(ROOT / '.runtime/grafana-local/controller.env')
    url = read_broker_url(getattr(args, 'rabbit_env', None))
    connection = pika.BlockingConnection(pika.URLParameters(url))
    connection.close()
    parent = ROOT / '.runtime/manual-simulations'
    if parent.is_symlink() or parent.resolve() != parent.absolute():
        raise RuntimeError('拒绝使用经过符号链接的仿真目录。')
    parent.mkdir(parents=True, exist_ok=True)
    # Retain this lock for the launcher lifetime, also across different ports.
    with (parent / '.launcher.lock').open('a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('另一个全新仿真脚本仍在运行，请先按 Ctrl+C 关闭。') from exc
        deleted = cleanup_previous_sessions(parent)
        return launch_session(args, url, parent, deleted, telemetry_env)


def launch_session(args, url, parent, deleted, telemetry_env):
    import pika

    directory = Path(tempfile.mkdtemp(prefix='session-', dir=parent))
    exchange = 'mpcrystal.manual.' + uuid4().hex
    config_path = directory / 'central/run_configuration.json'
    RunConfigurationStore(config_path).save(RunConfiguration(
        run_type='simulation', growth_rate_source='simulated',
        controller_mode='MPC', control_target='sigma',
        adaptation_enabled=False, adaptation_mode='E_A',
    ))
    env = dict(os.environ)
    env.update({
        'RABBIT_URL': url, 'RABBIT_EXCHANGE': exchange,
        'UI_MODE': 'development', 'CONTROL_TARGET': 'sigma',
        'PARAMS_FILE': str(directory / 'params_runtime.yaml'),
        'PARAMS_DEFAULT_FILE': str(ROOT / 'params_default.yaml'),
        'PARAM_META_FILE': str(ROOT / 'param_meta.yaml'),
        'OPERATION_META_FILE': str(ROOT / 'operation_meta.yaml'),
        'RUN_CONFIGURATION_FILE': str(config_path),
        'CONTROLLER_STATUS_URL': f'http://127.0.0.1:{args.controller_port}/api/status',
        'CONTROLLER_ADAPTER': 'crystallization_mpc.apps.controller.algorithm.integration:CrystallizationControllerAdapter',
        'CONTROLLER_OPCUA_ENABLED': 'false', 'CONTROLLER_OPCUA_WRITE_ENABLED': 'false',
        'CONTROLLER_INFLUX_ENABLED': 'false',
    })
    # A configured local dashboard receives every future tick natively.
    # Whitelist contains no device switches; OPC UA remains disabled.
    env.update(telemetry_env)
    env.pop('EXPERIMENT_HOST_ROOT_DISPLAY', None)
    processes, logs = [], []
    report = {'directory': str(directory), 'exchange': exchange, 'status': 'starting',
              'finished': False, 'child_pids': [], 'deleted_previous_sessions': deleted}
    marker = directory / 'launcher.json'
    marker.write_text(json.dumps(report, indent=2) + '\n')
    try:
        for role, port, module in (
            ('controller', args.controller_port, 'crystallization_mpc.apps.controller.app:web_app'),
            ('central', args.central_port, 'crystallization_mpc.apps.central.ui.app:web_app'),
        ):
            child_env = {**env, 'EXPERIMENT_ROOT': str(directory / role), 'RABBIT_QUEUE': exchange + '.' + role}
            log = os.fdopen(os.open(directory / (role + '.log'), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w')
            logs.append(log)
            child = subprocess.Popen(
                [sys.executable, '-m', 'uvicorn', module, '--host', '127.0.0.1', '--port', str(port)],
                cwd=ROOT, env=child_env, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            processes.append(child)
            report['child_pids'].append(child.pid)
            marker.write_text(json.dumps(report, indent=2) + '\n')
            deadline = time.monotonic() + 25
            ready = False
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise RuntimeError(f'{role} 启动失败，查看 {directory / (role + ".log")}')
                try:
                    state = fetch(port, '/api/status' if role == 'controller' else '/api/system/status')
                    ready = role == 'central' or state.get('consumer', {}).get('status') == 'consuming'
                    if ready:
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(0.2)
            if not ready:
                raise RuntimeError(f'{role} 启动超时。日志位于 {directory}')
        settings = fetch(args.central_port, '/api/run-configuration')
        state = fetch(args.central_port, '/api/system/status')
        if settings['locked'] or settings['configuration']['run_type'] != 'simulation' or state.get('current_experiment'):
            raise RuntimeError('Fresh-session verification failed.')
        report['status'] = 'ready'
        print(f'全新仿真已启动：http://127.0.0.1:{args.central_port}/', flush=True)
        if env.get('CONTROLLER_INFLUX_ENABLED') == 'true':
            print('已启用实时数据写入；Grafana：http://127.0.0.1:3000/（数据库历史不随本地会话清理）', flush=True)
        print(f'本次目录：{directory}\nSimulation + Simulated / MPC / sigma / 适应关闭。\n已清理 {len(deleted)} 个旧仿真会话；未创建实验。按 Ctrl+C 关闭本次两个服务。', flush=True)
        if args.check:
            print('Fresh-session check PASS', flush=True)
        else:
            while all(child.poll() is None for child in processes):
                time.sleep(1)
            raise RuntimeError(f'服务已退出，请查看本次目录中的日志：{directory}')
    except KeyboardInterrupt:
        print('\n关闭本次服务；本次实验、图片和日志将在下次启动本脚本时删除。', flush=True)
    finally:
        for child in reversed(processes):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        for log in logs:
            log.close()
        # Unique transport is never reused; remove only this launcher's own resources.
        try:
            connection = pika.BlockingConnection(pika.URLParameters(url))
            channel = connection.channel()
            for role in ('central', 'controller'):
                channel.queue_delete(queue=exchange + '.' + role)
            channel.exchange_delete(exchange=exchange)
            connection.close()
            report['transport_cleanup'] = 'PASS'
        except Exception:
            report['transport_cleanup'] = 'FAILED: broker unavailable; unique resources were not reused'
            print('本次专用消息队列未能清理；不影响此前已完成的本地会话删除。', flush=True)
        report['finished'] = True
        marker.write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--central-port', type=int, default=8000)
    parser.add_argument('--controller-port', type=int, default=8002)
    parser.add_argument('--rabbit-env', type=Path, help='RabbitMQ env file (overrides RABBIT_URL and default files); never pass secrets as CLI arguments.')
    parser.add_argument('--check', action='store_true', help='Also deletes previous sessions; check fresh startup, then stop owned processes.')
    args = parser.parse_args()
    def stop_requested(*_):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, stop_requested)
    try:
        run(args)
    except Exception as exc:
        # Avoid leaking broker credentials from connection exception strings.
        print(f'启动失败：{type(exc).__name__}' if not isinstance(exc, (RuntimeError, ValueError)) else str(exc), file=sys.stderr)
        sys.exit(1)
