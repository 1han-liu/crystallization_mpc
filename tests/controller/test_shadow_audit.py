from __future__ import annotations

from copy import deepcopy

import pytest

from crystallization_mpc.apps.controller.shadow_audit import (
    ShadowAuditError,
    capture_shadow_window,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, duration: float) -> None:
        self.value += duration


def status(tick: int, *, writes: int = 0, candidate: float = 304.0) -> dict:
    output = None
    if tick:
        output = {
            "tick_seq": tick,
            "controller_dt_s": 5.0,
            "elapsed_s": tick * 5.0,
            "growth_frame_seq": (tick - 1) // 3 + 1,
            "growth_sample_age_s": float((tick - 1) % 3 * 5),
            "computed_at": "2026-08-26T12:00:00Z",
            "result": {
                "valid": True,
                "error": None,
                "T_j_set": candidate,
                "objective": 0.25,
            },
            "process_state": {
                "T": 306.15,
                "T_j": 305.15,
                "c": 0.31,
                "count_middle": 100.0,
                "T_j_set": 304.15,
                "read_at": "2026-08-26T12:00:00Z",
            },
            "process_write_attempted": False,
            "process_write": None,
        }
    return {
        "status": "running",
        "active": True,
        "current_run_id": "shadow-run-1",
        "scheduler": {"tick_count": tick},
        "adapter": {"safe_noop": False},
        "last_control_output": output,
        "integrations": {
            "opcua": {
                "enabled": True,
                "write_enabled": False,
                "shadow_mode": True,
                "safe_noop": False,
                "connected": True,
                "endpoint": "opc.tcp://equipment:62552",
                "node_ids": {"T": "ns=2;s=T"},
                "read_count": tick,
                "write_count": writes,
                "safe_jacket_range_K": [253.15, 453.15],
            }
        },
    }


def sequence_reader(documents: list[dict]):
    remaining = iter(documents)
    last = documents[-1]

    def read() -> dict:
        nonlocal last
        try:
            last = next(remaining)
        except StopIteration:
            pass
        return deepcopy(last)

    return read


def test_capture_records_consecutive_real_reads_and_zero_writes() -> None:
    clock = FakeClock()
    documents = [status(10)] + [status(tick) for tick in range(11, 19)]
    evidence = capture_shadow_window(
        sequence_reader(documents),
        tick_count=8,
        timeout_s=20.0,
        poll_interval_s=0.5,
        clock=clock,
        sleep=clock.sleep,
        status_url="http://controller/api/status",
    )
    assert [record["tick_seq"] for record in evidence["records"]] == list(
        range(11, 19)
    )
    assert evidence["final"]["opcua_read_count"] == 18
    assert evidence["final"]["opcua_write_count"] == 0
    assert evidence["assertions"]["zero_write_calls"] is True
    assert all(
        record["constraints"]["candidate_within_jacket_range"]
        for record in evidence["records"]
    )


def test_capture_aborts_on_any_write_count() -> None:
    clock = FakeClock()
    with pytest.raises(ShadowAuditError, match="write_count is non-zero"):
        capture_shadow_window(
            sequence_reader([status(0), status(1, writes=1)]),
            tick_count=1,
            timeout_s=2.0,
            poll_interval_s=0.5,
            clock=clock,
            sleep=clock.sleep,
        )


def test_capture_rejects_out_of_bounds_candidate() -> None:
    clock = FakeClock()
    with pytest.raises(ShadowAuditError, match="violates the configured jacket range"):
        capture_shadow_window(
            sequence_reader([status(0), status(1, candidate=500.0)]),
            tick_count=1,
            timeout_s=2.0,
            poll_interval_s=0.5,
            clock=clock,
            sleep=clock.sleep,
        )


def test_capture_rejects_missing_process_read() -> None:
    clock = FakeClock()
    next_status = status(1)
    next_status["last_control_output"]["process_state"] = None
    with pytest.raises(ShadowAuditError, match="process_state must be an object"):
        capture_shadow_window(
            sequence_reader([status(0), next_status]),
            tick_count=1,
            timeout_s=2.0,
            poll_interval_s=0.5,
            clock=clock,
            sleep=clock.sleep,
        )


def test_capture_rejects_disconnected_opcua_adapter() -> None:
    disconnected = status(0)
    disconnected["integrations"]["opcua"]["connected"] = False
    with pytest.raises(ShadowAuditError, match="must be connected"):
        capture_shadow_window(sequence_reader([disconnected]), tick_count=1)


def test_capture_rejects_run_change() -> None:
    clock = FakeClock()
    changed = status(1)
    changed["current_run_id"] = "shadow-run-2"
    with pytest.raises(ShadowAuditError, match="run_id changed"):
        capture_shadow_window(
            sequence_reader([status(0), changed]),
            tick_count=1,
            timeout_s=2.0,
            poll_interval_s=0.5,
            clock=clock,
            sleep=clock.sleep,
        )
