import json
from typing import Any, Dict


def encode_json(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload).encode()


def decode_json(raw: bytes) -> Dict[str, Any]:
    return json.loads(raw.decode())
