"""Migration changes version labels, never alignment data or experiment content."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "state_migration", Path(__file__).resolve().parents[2] / "scripts/migrate_gsensor_state_v1.py"
)
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


def document():
    return {"schema_version": 2, "run_id": "example-run", "initialization": {"points": {"u": [1, 2]}},
            "processed_images": [{"image_name": "first.png"}], "algorithm_params": {"alignment_method": "loftr"},
            "processor": {"schema_version": 2, "run_id": "example-run", "frame_seq": 7,
                          "edges": [{"kalman": {"x": [1, 2, 3]}}],
                          "alignment": {"method": "loftr", "initialized": True,
                                        "sidecar": {"path": "alignment_state.npz", "sha256": "unchanged"}}}}


def test_conversion_preserves_every_nonversion_field():
    original = document()
    untouched = deepcopy(original)
    result = migration.convert_document(original)
    assert original == untouched
    expected = deepcopy(original)
    expected["schema_version"] = expected["processor"]["schema_version"] = 1
    assert result == expected
    assert migration.convert_document(result) == result


def test_migration_backup_dry_run_and_idempotency(tmp_path):
    folder = tmp_path / "example-run"
    folder.mkdir()
    path = folder / "gsensor_processing_state.json"
    source = (json.dumps(document()) + "\n").encode()
    path.write_bytes(source)
    sidecar = folder / "alignment_state.npz"
    sidecar.write_bytes(b"sidecar is not rewritten by format migration")
    assert not migration.migrate_file(path)["applied"]
    assert path.read_bytes() == source
    assert not list(folder.glob("*.bak"))
    record = migration.migrate_file(path, apply=True)
    assert Path(record["backup"]).read_bytes() == source
    assert json.loads(path.read_text()) == migration.convert_document(document())
    assert sidecar.read_bytes() == b"sidecar is not rewritten by format migration"
    after = path.read_bytes()
    assert not migration.migrate_file(path, apply=True)["changed"]
    assert path.read_bytes() == after
    assert len(list(folder.glob("*.bak"))) == 1


@pytest.mark.parametrize("bad", [None, 0, 3, True, "2", 2.0])
def test_invalid_versions_not_converted(bad):
    value = document()
    value["schema_version"] = bad
    with pytest.raises(ValueError):
        migration.convert_document(value)


def test_missing_alignment_or_wrong_run_rejected():
    value = document()
    value["processor"].pop("alignment")
    with pytest.raises(ValueError, match="alignment"):
        migration.convert_document(value)
    value = document()
    value["processor"]["run_id"] = "another-run"
    with pytest.raises(ValueError, match="run_id"):
        migration.convert_document(value)


def test_uninitialized_snapshot_converts_without_fabricating_processor():
    state = {"schema_version": 2, "run_id": "example-run", "processor": None}
    assert migration.convert_document(state) == {**state, "schema_version": 1}
