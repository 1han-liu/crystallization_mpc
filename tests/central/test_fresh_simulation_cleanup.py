"""Cleanup safety tests: all deletion targets are pytest temporary fixtures."""
import importlib.util
import json
import os
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    'fresh_launcher', Path(__file__).resolve().parents[2] / 'scripts/run_fresh_simulation.py',
)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def test_portable_broker_config_precedence(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, 'ROOT', tmp_path)
    monkeypatch.delenv('RABBIT_URL', raising=False)
    with pytest.raises(RuntimeError, match='config/rabbitmq.env.example'):
        launcher.read_broker_url()
    legacy = tmp_path / '.runtime/rabbitmq-debug/runtime.env'
    legacy.parent.mkdir(parents=True)
    legacy.write_text('RABBIT_URL=amqp://legacy:5673/%2F\n')
    assert launcher.read_broker_url() == 'amqp://legacy:5673/%2F'
    portable = tmp_path / 'config/rabbitmq.env'
    portable.parent.mkdir()
    portable.write_text('export RABBIT_URL="amqps://portable:5671/%2F" # local\n')
    assert launcher.read_broker_url() == 'amqps://portable:5671/%2F'
    monkeypatch.setenv('RABBIT_URL', 'amqp://environment:5672/%2F')
    assert launcher.read_broker_url() == 'amqp://environment:5672/%2F'
    assert launcher.read_broker_url(portable) == 'amqps://portable:5671/%2F'
    with pytest.raises(RuntimeError):
        launcher.read_broker_url(tmp_path / 'missing.env')


@pytest.mark.parametrize('content', ['RABBIT_URL=', 'RABBIT_URL="broken',
                                    'RABBIT_URL=amqp://a\nRABBIT_URL=amqp://b',
                                    'RABBIT_URL=http://user:secret@host/',
                                    'RABBIT_URL=amqp://user:secret@host:invalid/'])
def test_invalid_broker_file_is_rejected_without_disclosing_secret(tmp_path, content):
    path = tmp_path / 'broker.env'
    path.write_text(content)
    with pytest.raises(RuntimeError) as caught:
        launcher.read_broker_url(path)
    assert 'secret' not in str(caught.value)


def test_broker_file_does_not_execute_shell(tmp_path):
    marker = tmp_path / 'must-not-exist'
    path = tmp_path / 'broker.env'
    path.write_text(f'OTHER=$(touch {marker})\nRABBIT_URL=amqp://broker:5673/%2F\n')
    assert launcher.read_broker_url(path) == 'amqp://broker:5673/%2F'
    assert not marker.exists()


def session(parent, name='session-old', **overrides):
    directory = parent / name
    directory.mkdir(parents=True)
    report = {'directory': str(directory), 'exchange': 'mpcrystal.manual.fixture',
              'status': 'ready', 'transport_cleanup': 'PASS', **overrides}
    (directory / 'launcher.json').write_text(json.dumps(report))
    return directory


def test_removes_old_images_and_annotations_only_inside_managed_sessions(tmp_path):
    parent = tmp_path / 'manual-simulations'
    old = session(parent)
    (old / 'images').mkdir()
    (old / 'images/raw.png').write_bytes(b'fixture image')
    (old / 'annotations.json').write_text('{}')
    outside = tmp_path / 'rabbitmq-debug/images'
    outside.mkdir(parents=True)
    (outside / 'raw.png').write_bytes(b'original image')
    unrelated = parent / 'notes.txt'
    unrelated.write_text('not a managed session')
    assert launcher.cleanup_previous_sessions(parent) == [str(old)]
    assert not old.exists()
    assert (outside / 'raw.png').read_bytes() == b'original image'
    assert unrelated.exists()
    assert launcher.cleanup_previous_sessions(parent) == []


@pytest.mark.parametrize('case', ['missing_marker', 'wrong_owner', 'unfinished', 'bad_json'])
def test_validates_all_sessions_before_any_deletion(tmp_path, case):
    parent = tmp_path / 'manual-simulations'
    valid = session(parent, 'session-a')
    bad = session(parent, 'session-z')
    marker = bad / 'launcher.json'
    if case == 'missing_marker':
        marker.unlink()
    elif case == 'bad_json':
        marker.write_text('invalid json')
    else:
        report = json.loads(marker.read_text())
        report.update({'directory': str(tmp_path)} if case == 'wrong_owner' else {'finished': False})
        marker.write_text(json.dumps(report))
    with pytest.raises(RuntimeError):
        launcher.cleanup_previous_sessions(parent)
    assert valid.exists() and bad.exists()


def test_live_process_blocks_cleanup(tmp_path):
    parent = tmp_path / 'manual-simulations'
    old = session(parent, finished=True, child_pids=[os.getpid()])
    with pytest.raises(RuntimeError, match='仍存在'):
        launcher.cleanup_previous_sessions(parent)
    assert old.exists()


def test_session_symlink_is_rejected(tmp_path):
    parent = tmp_path / 'manual-simulations'
    parent.mkdir()
    target = tmp_path / 'external'
    target.mkdir()
    (parent / 'session-link').symlink_to(target, target_is_directory=True)
    with pytest.raises(RuntimeError):
        launcher.cleanup_previous_sessions(parent)
    assert target.exists()


def test_root_symlink_is_rejected(tmp_path):
    target = tmp_path / 'external'
    target.mkdir()
    parent = tmp_path / 'manual-simulations'
    parent.symlink_to(target, target_is_directory=True)
    with pytest.raises(RuntimeError):
        launcher.cleanup_previous_sessions(parent)


def test_nested_image_symlink_does_not_delete_source(tmp_path):
    parent = tmp_path / 'manual-simulations'
    old = session(parent)
    source = tmp_path / 'source-images'
    source.mkdir()
    (source / 'raw.png').write_bytes(b'original image')
    (old / 'images').symlink_to(source, target_is_directory=True)
    launcher.cleanup_previous_sessions(parent)
    assert (source / 'raw.png').read_bytes() == b'original image'
