from typing import Dict, List

EXCHANGE = "idp.bus"

QUEUES: Dict[str, str] = {
    "central": "central.in",
    "controller": "controller.in",
    "gsensor": "gsensor.in",
    "shared": "shared.in",
}

BROADCAST_BINDING = "broadcast.#"


def route(src: str, dst: str) -> str:
    return f"{src}.to.{dst}"


def bindings_for(role: str, include_broadcast: bool = True) -> List[str]:
    roles = ["central", "controller", "gsensor", "shared"]
    bindings = [route(src, role) for src in roles if src != role]
    if include_broadcast:
        bindings.append(BROADCAST_BINDING)
    return bindings
