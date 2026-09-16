#!/usr/bin/env python3
"""Read-only live-run bridge for a Controller started without Influx writing.

No experiment commands, replay or fabricated history. Polling is best-effort;
missed ticks are counted, not synthesized. Future runs use native writing.
"""
from __future__ import annotations
import argparse
import fcntl
import json
import os
from pathlib import Path
import time
import urllib.request

from crystallization_mpc.infra.influxdb.local_config import load_telemetry_env
from crystallization_mpc.apps.controller.result import ControllerStepResult
from crystallization_mpc.apps.controller.telemetry import ControllerMeasurementRecord, write_controller_measurement
from crystallization_mpc.apps.controller.tick import ControllerTickInput
from crystallization_mpc.infra.influxdb.client import InfluxSettings
from crystallization_mpc.infra.influxdb.write import InfluxWriter

ROOT = Path(__file__).resolve().parents[1]


def record_from_status(status, run_id):
    if status.get('current_run_id') != run_id:
        return None
    parameters = status.get('parameters', {})
    if parameters.get('exp_sim', parameters.get('run_type')) != 'simulation':
        raise ValueError('This bridge is restricted to the selected numerical simulation.')
    if parameters.get('exp_sim_G', parameters.get('growth_rate_source')) not in ('simulation', 'simulated'):
        raise ValueError('This bridge does not reconstruct image input samples.')
    output = status.get('last_control_output')
    if not output or output.get('run_id') != run_id:
        return None
    config = output['runtime_configuration']  # Tick configuration, NOT later UI changes.
    return ControllerMeasurementRecord(
        run_id=run_id,
        tick=ControllerTickInput(tick_seq=output['tick_seq'], controller_dt_s=output['controller_dt_s'],
                                 elapsed_s=output['elapsed_s']),
        result=ControllerStepResult(**output['result']), computed_at=output['computed_at'],
        adaptation_enabled=config['adaptation_enabled'], adaptation_mode=config['adaptation_mode'],
        control_target=config['control_target'], runtime_revision=output['runtime_revision'],
    )


def run(args):
    values = load_telemetry_env(ROOT / '.runtime/grafana-local/controller.env')
    if not values:
        raise RuntimeError('Run configure_local_grafana.py first.')
    output = ROOT / '.runtime/grafana-local/forwarder-status.json'
    with output.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = {'run_id': args.run_id, 'pid': os.getpid(), 'written': 0, 'missed_ticks': 0,
                  'last_tick': None, 'last_computed_at': None, 'last_error': None, 'mode': 'status_bridge'}
        if output.exists():
            prior = json.loads(output.read_text())
            if prior.get('run_id') == args.run_id:
                report.update({k: prior[k] for k in ('written', 'missed_ticks', 'last_tick', 'last_computed_at')})
        writer = InfluxWriter(InfluxSettings(url=values['CONTROLLER_INFLUX_URL'],
            token=values['CONTROLLER_INFLUX_TOKEN'], org=values['CONTROLLER_INFLUX_ORG'],
            bucket=values['CONTROLLER_INFLUX_BUCKET']))
        try:
            while True:
                try:
                    with urllib.request.urlopen(args.url, timeout=3) as response:
                        status = json.load(response)
                    if status.get('current_run_id') != args.run_id:
                        report['mode'] = 'stopped_run_changed'
                        break
                    if status.get('integrations', {}).get('influxdb', {}).get('enabled'):
                        report['mode'] = 'stopped_native_writer_enabled'
                        break
                    record = record_from_status(status, args.run_id)
                    if record and record.computed_at != report['last_computed_at']:
                        write_controller_measurement(writer, record)
                        if report['last_tick'] is not None:
                            report['missed_ticks'] += max(0, record.tick.tick_seq - report['last_tick'] - 1)
                        report.update(written=report['written'] + 1, last_tick=record.tick.tick_seq,
                                      last_computed_at=record.computed_at, last_error=None)
                except Exception as exc:
                    report['last_error'] = type(exc).__name__  # No auth details in public status/logs.
                temporary = output.with_suffix('.tmp')
                temporary.write_text(json.dumps(report, indent=2) + '\n')
                temporary.replace(output)
                time.sleep(1)
        finally:
            writer.close()
            output.write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--url', default='http://127.0.0.1:8002/api/status')
    run(parser.parse_args())
