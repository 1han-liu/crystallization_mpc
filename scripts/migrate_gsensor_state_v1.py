"""One-time v2 -> extended-v1 conversion; stop GSensor before using --apply.

Runtime code accepts only v1. This explicitly invoked tool is the sole reader
of old v2 snapshots. It preserves all state fields and sidecar references.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from uuid import uuid4


def convert_document(document: dict) -> dict:
    if not isinstance(document, dict):
        raise ValueError("Processing state must be an object")
    result = deepcopy(document)
    version = result.get("schema_version")
    if type(version) is not int or version not in (1, 2):
        raise ValueError("Only processing schema 1 or 2 can be converted")
    run_id = result.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("Missing run_id")
    processor = result.get("processor")
    if processor is not None:
        if not isinstance(processor, dict):
            raise ValueError("Processor must be an object")
        pv = processor.get("schema_version")
        if type(pv) is not int or pv not in (1, 2):
            raise ValueError("Unsupported processor schema")
        if processor.get("run_id") != run_id:
            raise ValueError("Processor run_id mismatch")
        if pv == 2 and not isinstance(processor.get("alignment"), dict):
            raise ValueError("Version-2 processor must retain its alignment metadata")
        processor["schema_version"] = 1
    result["schema_version"] = 1
    return result


def prepare(path: Path) -> tuple[bytes, dict, dict]:
    if path.name != "gsensor_processing_state.json" or path.is_symlink():
        raise ValueError("Pass an explicit, non-symlink gsensor_processing_state.json")
    original = path.read_bytes()
    document = json.loads(original)
    converted = convert_document(document)
    if path.parent.name != document["run_id"]:
        raise ValueError("Snapshot run_id does not match its parent experiment directory")
    return original, document, converted


def migrate_file(path: Path, *, apply: bool = False) -> dict:
    original, document, converted = prepare(path)
    record = {"path": str(path.resolve()), "changed": document != converted,
              "applied": False, "backup": None,
              "before_sha256": hashlib.sha256(original).hexdigest()}
    if not record["changed"] or not apply:
        return record
    # Back up the exact bytes before overwriting; never overwrite an older backup.
    backup = path.with_name(f"{path.name}.before-unified-v1-{uuid4().hex}.bak")
    with backup.open("xb") as stream:
        stream.write(original)
        stream.flush()
        os.fsync(stream.fileno())
    mode = stat.S_IMODE(path.stat().st_mode)
    os.chmod(backup, mode)
    payload = (json.dumps(converted, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".state-v1-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        if path.is_symlink() or path.read_bytes() != original:
            raise RuntimeError("State changed during migration; stop GSensor before retrying")
        os.replace(temporary, path)
        temporary = None
        if json.loads(path.read_bytes()) != converted:
            raise RuntimeError(f"Verification failed; original backup is {backup}")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    record.update(applied=True, backup=str(backup.resolve()),
                  after_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--apply", action="store_true", help="Write conversions with backups (default: dry-run)")
    args = parser.parse_args()
    # Validate every target before mutating any of them.
    for path in args.paths:
        prepare(path)
    for path in args.paths:
        print(json.dumps(migrate_file(path, apply=args.apply), ensure_ascii=False))


if __name__ == "__main__":
    main()
