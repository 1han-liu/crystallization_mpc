"""Read-only evidence capture for an already running Controller shadow window.

This module deliberately observes only the Controller status HTTP endpoint.  It
does not publish lifecycle commands, connect to OPC UA, or expose any method
that can write an equipment setpoint.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.request import Request, urlopen


StatusReader = Callable[[], Mapping[str, Any]]


class ShadowAuditError(RuntimeError):
    """The observed run did not satisfy the read-only shadow contract."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_controller_status(url: str, *, timeout_s: float = 5.0) -> Mapping[str, Any]:
    """Read one JSON status document without mutating the Controller service."""

    request = Request(url, method="GET", headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout_s) as response:  # noqa: S310 - operator URL
        document = json.load(response)
    if not isinstance(document, Mapping):
        raise ShadowAuditError("Controller status response must be a JSON object.")
    return document


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ShadowAuditError(f"Controller status {name} must be an object.")
    return value


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ShadowAuditError(f"Controller status {name} must be a non-negative integer.")
    return value


def _finite_optional(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ShadowAuditError(f"Controller status {name} must be numeric or null.")
    result = float(value)
    if not math.isfinite(result):
        raise ShadowAuditError(f"Controller status {name} must be finite.")
    return result


def _shadow_state(status: Mapping[str, Any]) -> tuple[Mapping[str, Any], int, int, int]:
    if status.get("status") != "running" or status.get("active") is not True:
        raise ShadowAuditError("Controller must already be RUNNING for shadow capture.")

    adapter = _mapping(status.get("adapter"), "adapter")
    if adapter.get("safe_noop") is True:
        raise ShadowAuditError("Translated Controller adapter is not enabled.")

    scheduler = _mapping(status.get("scheduler"), "scheduler")
    tick_count = _nonnegative_int(scheduler.get("tick_count"), "scheduler.tick_count")

    integrations = _mapping(status.get("integrations"), "integrations")
    opcua = _mapping(integrations.get("opcua"), "integrations.opcua")
    if opcua.get("enabled") is not True:
        raise ShadowAuditError("CONTROLLER_OPCUA_ENABLED must be true.")
    if opcua.get("write_enabled") is not False:
        raise ShadowAuditError("CONTROLLER_OPCUA_WRITE_ENABLED must be false.")
    if opcua.get("shadow_mode") is not True:
        raise ShadowAuditError("Controller does not report read-only shadow mode.")
    if opcua.get("safe_noop") is True:
        raise ShadowAuditError("Real OPC UA process reads are not enabled.")
    if opcua.get("connected") is not True:
        raise ShadowAuditError("Real OPC UA process adapter must be connected.")

    read_count = _nonnegative_int(opcua.get("read_count"), "opcua.read_count")
    write_count = _nonnegative_int(opcua.get("write_count"), "opcua.write_count")
    if write_count != 0:
        raise ShadowAuditError("OPC UA write_count is non-zero; aborting shadow audit.")
    return opcua, tick_count, read_count, write_count


def _run_id(status: Mapping[str, Any]) -> str:
    value = status.get("current_run_id")
    if not isinstance(value, str) or not value.strip():
        raise ShadowAuditError("Controller status current_run_id is required.")
    return value.strip()


def _bounds(opcua: Mapping[str, Any]) -> tuple[float, float]:
    values = opcua.get("safe_jacket_range_K")
    if not isinstance(values, (list, tuple)) or len(values) != 2:
        raise ShadowAuditError("OPC UA safe_jacket_range_K must contain two bounds.")
    lower = _finite_optional(values[0], "opcua.safe_jacket_range_K[0]")
    upper = _finite_optional(values[1], "opcua.safe_jacket_range_K[1]")
    assert lower is not None and upper is not None
    if lower >= upper:
        raise ShadowAuditError("OPC UA jacket bounds are invalid.")
    return lower, upper


def _evidence_record(
    status: Mapping[str, Any], opcua: Mapping[str, Any], expected_tick: int
) -> dict[str, Any] | None:
    output_value = status.get("last_control_output")
    if output_value is None:
        return None
    output = _mapping(output_value, "last_control_output")
    tick_seq = _nonnegative_int(output.get("tick_seq"), "last_control_output.tick_seq")
    if tick_seq < expected_tick:
        return None
    if tick_seq != expected_tick:
        raise ShadowAuditError(
            f"Status polling missed Controller tick {expected_tick}; next observed tick was "
            f"{tick_seq}. Reduce --poll-interval and repeat the audit."
        )
    if output.get("process_write_attempted") is not False:
        raise ShadowAuditError(f"Controller tick {tick_seq} attempted an OPC UA write.")
    if output.get("process_write") is not None:
        raise ShadowAuditError(f"Controller tick {tick_seq} reports an OPC UA write result.")

    process_state = _mapping(output.get("process_state"), "last_control_output.process_state")
    result = _mapping(output.get("result"), "last_control_output.result")
    valid = result.get("valid")
    if not isinstance(valid, bool):
        raise ShadowAuditError("Controller result valid must be boolean.")
    candidate = _finite_optional(result.get("T_j_set"), "result.T_j_set")
    objective = _finite_optional(result.get("objective"), "result.objective")
    failure_reason = result.get("error")
    if failure_reason is not None and not isinstance(failure_reason, str):
        raise ShadowAuditError("Controller result error must be text or null.")
    if valid and candidate is None:
        raise ShadowAuditError(f"Valid Controller tick {tick_seq} has no T_j_set candidate.")

    lower, upper = _bounds(opcua)
    within_bounds = candidate is None or lower <= candidate <= upper
    if not within_bounds:
        raise ShadowAuditError(
            f"Controller tick {tick_seq} candidate {candidate} K violates the configured "
            f"jacket range [{lower}, {upper}] K."
        )

    return {
        "tick_seq": tick_seq,
        "controller_dt_s": _finite_optional(
            output.get("controller_dt_s"), "last_control_output.controller_dt_s"
        ),
        "elapsed_s": _finite_optional(
            output.get("elapsed_s"), "last_control_output.elapsed_s"
        ),
        "growth_frame_seq": output.get("growth_frame_seq"),
        "growth_sample_age_s": _finite_optional(
            output.get("growth_sample_age_s"),
            "last_control_output.growth_sample_age_s",
        ),
        "process_state": dict(process_state),
        "candidate_T_j_set_K": candidate,
        "constraints": {
            "jacket_range_K": [lower, upper],
            "candidate_within_jacket_range": within_bounds,
        },
        "solver": {
            "status": "valid" if valid else "invalid",
            "objective": objective,
            "failure_reason": failure_reason,
        },
        "process_write_attempted": False,
        "process_write": None,
        "computed_at": output.get("computed_at"),
    }


def capture_shadow_window(
    status_reader: StatusReader,
    *,
    tick_count: int = 8,
    timeout_s: float = 180.0,
    poll_interval_s: float = 0.5,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    status_url: str | None = None,
) -> dict[str, Any]:
    """Capture consecutive future ticks and prove that the window made zero writes."""

    if isinstance(tick_count, bool) or not isinstance(tick_count, int) or tick_count < 1:
        raise ValueError("tick_count must be a positive integer.")
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("timeout_s must be finite and positive.")
    if not math.isfinite(poll_interval_s) or poll_interval_s <= 0:
        raise ValueError("poll_interval_s must be finite and positive.")

    started_at = _utc_now()
    initial_status = status_reader()
    initial_opcua, baseline_tick, baseline_reads, baseline_writes = _shadow_state(
        initial_status
    )
    run_id = _run_id(initial_status)
    _bounds(initial_opcua)
    expected_tick = baseline_tick + 1
    deadline = clock() + timeout_s
    records: list[dict[str, Any]] = []
    final_status = initial_status
    final_reads = baseline_reads
    final_writes = baseline_writes

    while len(records) < tick_count:
        if clock() >= deadline:
            raise ShadowAuditError(
                f"Timed out after observing {len(records)}/{tick_count} consecutive ticks."
            )
        sleep(poll_interval_s)
        final_status = status_reader()
        opcua, scheduler_tick, final_reads, final_writes = _shadow_state(final_status)
        if _run_id(final_status) != run_id:
            raise ShadowAuditError("Controller run_id changed during shadow capture.")
        if scheduler_tick < expected_tick:
            continue
        record = _evidence_record(final_status, opcua, expected_tick)
        if record is None:
            raise ShadowAuditError(
                f"Controller tick {expected_tick} produced no auditable control output."
            )
        records.append(record)
        expected_tick += 1

    if final_writes != 0:
        raise ShadowAuditError("OPC UA write_count changed during shadow capture.")
    if final_reads - baseline_reads < tick_count:
        raise ShadowAuditError(
            "OPC UA read_count did not increase once per captured Controller tick."
        )

    return {
        "schema_version": 1,
        "capture_mode": "controller_status_api_read_only",
        "status_url": status_url,
        "run_id": run_id,
        "started_at": started_at,
        "completed_at": _utc_now(),
        "required_tick_count": tick_count,
        "captured_tick_count": len(records),
        "baseline": {
            "tick_count": baseline_tick,
            "opcua_read_count": baseline_reads,
            "opcua_write_count": baseline_writes,
            "opcua_endpoint": initial_opcua.get("endpoint"),
            "opcua_node_ids": initial_opcua.get("node_ids"),
        },
        "final": {
            "tick_count": _nonnegative_int(
                _mapping(final_status.get("scheduler"), "scheduler").get("tick_count"),
                "scheduler.tick_count",
            ),
            "opcua_read_count": final_reads,
            "opcua_write_count": final_writes,
        },
        "assertions": {
            "real_opcua_reads_enabled": True,
            "same_run_id_for_entire_window": True,
            "opcua_write_gate_disabled": True,
            "shadow_mode": True,
            "consecutive_ticks": True,
            "one_or_more_reads_per_tick": True,
            "zero_write_calls": True,
            "zero_write_attempts": True,
            "all_candidates_within_configured_jacket_range": True,
        },
        "records": records,
    }


def write_evidence(document: Mapping[str, Any], output_path: Path) -> None:
    """Atomically write a JSON evidence artifact."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture a strictly read-only Controller OPC UA shadow window."
    )
    parser.add_argument(
        "--status-url",
        default="http://127.0.0.1:8002/api/status",
        help="Controller status API (GET only).",
    )
    parser.add_argument("--ticks", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Destination JSON evidence file.",
    )
    args = parser.parse_args(argv)

    reader = lambda: read_controller_status(args.status_url)
    try:
        evidence = capture_shadow_window(
            reader,
            tick_count=args.ticks,
            timeout_s=args.timeout,
            poll_interval_s=args.poll_interval,
            status_url=args.status_url,
        )
        write_evidence(evidence, args.output)
    except (OSError, ValueError, ShadowAuditError) as exc:
        parser.exit(2, f"shadow audit failed: {exc}\n")
    print(
        f"shadow audit passed: {evidence['captured_tick_count']} consecutive ticks, "
        f"{evidence['final']['opcua_read_count'] - evidence['baseline']['opcua_read_count']} "
        f"OPC UA reads, 0 writes; evidence={args.output}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())


__all__ = [
    "ShadowAuditError",
    "capture_shadow_window",
    "main",
    "read_controller_status",
    "write_evidence",
]
