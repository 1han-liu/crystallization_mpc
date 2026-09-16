import copy
import importlib.util
from pathlib import Path

import pytest

from crystallization_mpc.infra.influxdb.local_config import load_telemetry_env

spec = importlib.util.spec_from_file_location('telemetry_bridge', Path(__file__).resolve().parents[2] / 'scripts/forward_controller_telemetry.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def snapshot():
    return {'current_run_id': 'test-run', 'parameters': {'exp_sim': 'simulation', 'exp_sim_G': 'simulation'},
            'runtime_controls': {'configuration': {'control_target': 'G'}},
            'adaptation': {'enabled': False, 'mode': 'all'},
            'last_control_output': {'run_id': 'test-run', 'tick_seq': 10, 'controller_dt_s': 5.0,
                'elapsed_s': 50.0, 'computed_at': '2026-09-10T12:00:00Z',
                'runtime_configuration': {'control_target': 'sigma', 'adaptation_enabled': True, 'adaptation_mode': 'E_A'},
                'runtime_revision': 2, 'result': {'valid': True, 'T_j_set': 300.0, 'sigma': 0.04, 'target_set': 0.035}}}


def test_bridge_preserves_tick_config_values_and_original_timestamp():
    s = snapshot()
    before = copy.deepcopy(s)
    record = bridge.record_from_status(s, 'test-run')
    assert record.tags()['target'] == 'sigma'
    assert record.tags()['adaptation_mode'] == 'E_A'
    assert record.tags()['adaptation_enabled'] == 'true'
    assert record.fields()['runtime_revision'] == 2
    assert record.fields()['target_set'] == 0.035
    assert record.fields()['sigma'] == 0.04
    assert record.timestamp().isoformat() == '2026-09-10T12:00:00+00:00'
    assert s == before


def test_no_fabricated_output_or_cross_run_data():
    s = snapshot()
    assert bridge.record_from_status(s, 'other-run') is None
    s['last_control_output']['run_id'] = 'other-run'
    assert bridge.record_from_status(s, 'test-run') is None
    s['last_control_output'] = None
    assert bridge.record_from_status(s, 'test-run') is None


@pytest.mark.parametrize('field,value', [('exp_sim', 'experiment'), ('exp_sim_G', 'experiment')])
def test_bridge_rejects_device_or_image_runs(field, value):
    s = snapshot()
    s['parameters'][field] = value
    with pytest.raises(ValueError):
        bridge.record_from_status(s, 'test-run')


def test_local_environment_is_whitelisted_and_does_not_execute_shell(tmp_path):
    path = tmp_path / 'controller.env'
    path.write_text('CONTROLLER_INFLUX_ENABLED=true\nCONTROLLER_INFLUX_URL=http://localhost:8087\n'
                    'CONTROLLER_INFLUX_ORG=test\nCONTROLLER_INFLUX_BUCKET=test\nCONTROLLER_INFLUX_TOKEN=fixture-token\n'
                    'CONTROLLER_OPCUA_ENABLED=true\nEXPERIMENT_ROOT=/outside\n')
    values = load_telemetry_env(path)
    assert len(values) == 5
    assert 'CONTROLLER_OPCUA_ENABLED' not in values
    assert 'EXPERIMENT_ROOT' not in values


def test_missing_local_setup_leaves_native_writing_disabled(tmp_path):
    assert load_telemetry_env(tmp_path / 'missing.env') == {}
    path = tmp_path / 'partial.env'
    path.write_text('CONTROLLER_INFLUX_ENABLED=true\n')
    with pytest.raises(ValueError):
        load_telemetry_env(path)
