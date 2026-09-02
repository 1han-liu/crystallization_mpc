from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional
import time


def utc_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time()*1000)%1000:03d}Z"


@dataclass
class Envelope: # 统一消息格式
    ver: int
    ts: str
    src: str # 发送方
    dst: str # 接收方
    msg_type: str
    name: str
    seq: int
    correlation_id: Optional[str]
    payload: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ver": self.ver,
            "ts": self.ts,
            "src": self.src,
            "dst": self.dst,
            "msg_type": self.msg_type,
            "name": self.name,
            "seq": self.seq,
            "correlation_id": self.correlation_id,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Envelope":
        return cls(
            ver=int(data.get("ver", 1)),
            ts=str(data.get("ts", utc_ts())),
            src=str(data.get("src", "")),
            dst=str(data.get("dst", "")),
            msg_type=str(data.get("msg_type", "")),
            name=str(data.get("name", "")),
            seq=int(data.get("seq", 0)),
            correlation_id=data.get("correlation_id"),
            payload=dict(data.get("payload", {})),
        )


def build_envelope(
    src: str,
    dst: str,
    msg_type: str,
    name: str,
    seq: int,
    payload: Dict[str, Any],
    correlation_id: Optional[str] = None,
) -> Dict[str, Any]:
    env = Envelope(
        ver=1,
        ts=utc_ts(),
        src=src,
        dst=dst,
        msg_type=msg_type,
        name=name,
        seq=seq,
        correlation_id=correlation_id,
        payload=payload,
    )
    return env.to_dict()
