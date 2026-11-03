import json
from typing import Any, Dict

from crystallization_mpc.infra.numeric import encode_nonfinite


def encode_json(payload: Dict[str, Any]) -> bytes:
    return json.dumps(encode_nonfinite(payload), allow_nan=False).encode()


def decode_json(raw: bytes) -> Dict[str, Any]:
    return json.loads(raw.decode())
