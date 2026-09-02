from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from crystallization_mpc.apps.controller.adapter import ControllerAdapter
from crystallization_mpc.apps.controller.config import ControllerSettings
from crystallization_mpc.apps.controller.process import (
    ProcessAdapter,
    ProcessState,
    ProcessWriteResult,
)
from crystallization_mpc.apps.controller.result import ControllerStepResult
from crystallization_mpc.apps.controller.service import ControllerService, ControllerState
from crystallization_mpc.apps.controller.tick import ControllerTickInput
from crystallization_mpc.apps.controller.algorithm.integration import (
    CrystallizationControllerAdapter,
)
from crystallization_mpc.messaging.contracts import GrowthRateSamplePayload


@dataclass
class FakeClock:
    value: float = 100.0

    def __call__(self) -> float:
        return self.value


class RecordingAdapter(ControllerAdapter):
    def __init__(self, output: ControllerStepResult | None = None) -> None:
        self.output = output or ControllerStepResult(T_j_set=300.0)
        self.ticks: list[ControllerTickInput] = []
        self.running = False

    def configure(self, params: Mapping[str, Any], run_id: str) -> None:
        self.params = dict(params)
        self.run_id = run_id

    def start(self) -> None:
        self.running = True

    def step(self, sample: Any, process_state: ProcessState | None = None) -> ControllerStepResult | None:
        assert isinstance(sample, ControllerTickInput)
        self.ticks.append(sample)
        return self.output

    def stop(self) -> None:
        self.running = False


class RecordingProcess(ProcessAdapter):
    def __init__(self) -> None:
        self.connected = False
        self.read_count = 0
        self.write_count = 0

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def read_state(self) -> ProcessState:
        self.read_count += 1
        return ProcessState(
            T=306.15,
            T_j=305.15,
            c=0.31,
            count_middle=100.0,
            T_j_set=304.15,
            read_at="2026-08-26T12:00:00Z",
        )

    def write_jacket_setpoint(self, setpoint_K: float) -> ProcessWriteResult:
        self.write_count += 1
        return ProcessWriteResult(
            setpoint_K=setpoint_K,
            setpoint_C=setpoint_K - 273.15,
            written_at="2026-08-26T12:00:01Z",
            verified=True,
            readback_K=setpoint_K,
        )

    def status(self) -> Mapping[str, Any]:
        return {
            "connected": self.connected,
            "read_count": self.read_count,
            "write_count": self.write_count,
        }


def settings(*, opcua: bool, write: bool = False) -> ControllerSettings:
    return ControllerSettings(
        rabbit_url="amqp://guest:guest@localhost/%2F",
        rabbit_exchange="test",
        rabbit_queue="test.controller",
        adapter_spec=None,
        opcua_enabled=opcua,
        opcua_endpoint="opc.tcp://test:62552" if opcua else None,
        influx_enabled=False,
        influx_url="http://test:8086",
        influx_org="test",
        influx_bucket="test",
        opcua_write_enabled=write,
    )


def sample(
    frame: int, *, valid: bool = True, dt_s: float = 15.0
) -> GrowthRateSamplePayload:
    values = (3e-8, 3e-8, 3e-8, 3e-8) if valid else (None, None, None, None)
    return GrowthRateSamplePayload(
        run_id="run-1",
        frame_seq=frame,
        image_name=f"frame-{frame}.png",
        captured_at="2026-08-26T12:00:00Z",
        processed_at="2026-08-26T12:00:01Z",
        dt_s=dt_s,
        valid=valid,
        status="measuring" if valid else "error",
        G_u=values[0],
        G_u_KF=values[1],
        G_v=values[2],
        G_v_KF=values[3],
        error=None if valid else "bad frame",
    )


def start_service(
    service: ControllerService, *, run_type: str = "experiment"
) -> None:
    service._apply_parameters(
        {
            "version": 1,
            "params": {
                "dt": 5.0,
                "dt_G": 15.0,
                "run_type": run_type,
                "growth_rate_source": (
                    "live_gsensor" if run_type == "experiment" else "simulated"
                ),
            },
        }
    )
    service._start_experiment(
        {
            "run_id": "run-1",
            "parameter_version": 1,
            "started_at": "2026-08-26T12:00:00Z",
            "adaptation_enabled": False,
            "adaptation_mode": "E_A",
        }
    )


def test_three_controller_ticks_reuse_one_growth_frame_without_extra_ticks() -> None:
    clock = FakeClock()
    adapter = RecordingAdapter()
    process = RecordingProcess()
    service = ControllerService(
        settings(opcua=True),
        adapter=adapter,
        process_adapter=process,
        monotonic_clock=clock,
    )
    start_service(service)
    accepted = service._accept_sample(sample(1).to_dict())
    assert accepted["cached"] is True and accepted["adapter_called"] is False
    assert not adapter.ticks

    for now in (105.0, 110.0, 115.0):
        clock.value = now
        service._control_tick_once(now=now)
    assert [tick.tick_seq for tick in adapter.ticks] == [1, 2, 3]
    assert [tick.growth_sample.frame_seq for tick in adapter.ticks] == [1, 1, 1]
    assert [tick.growth_sample_age_s for tick in adapter.ticks] == [5.0, 10.0, 15.0]
    assert process.read_count == 3
    assert process.write_count == 0
    assert service.control_tick_count == 3
    assert service.valid_sample_count == 1
    assert service.last_control_output is not None
    assert service.last_control_output["tick_seq"] == 3
    assert service.last_control_output["growth_frame_seq"] == 1
    assert service.last_control_output["growth_sample_age_s"] == 15.0


def test_invalid_and_duplicate_frames_do_not_replace_valid_cache() -> None:
    clock = FakeClock()
    adapter = RecordingAdapter()
    service = ControllerService(
        settings(opcua=False), adapter=adapter, monotonic_clock=clock
    )
    start_service(service, run_type="simulation")
    service._accept_sample(sample(1).to_dict())
    service._accept_sample(sample(2, valid=False).to_dict())
    duplicate = service._accept_sample(sample(2, valid=False).to_dict())
    assert duplicate["duplicate"] is True
    assert service.last_valid_sample is not None
    assert service.last_valid_sample.frame_seq == 1


def test_variable_growth_frame_intervals_only_replace_cache() -> None:
    adapter = RecordingAdapter()
    service = ControllerService(settings(opcua=False), adapter=adapter)
    start_service(service, run_type="simulation")
    service._accept_sample(sample(1, dt_s=7.0).to_dict())
    service._accept_sample(sample(2, dt_s=19.0).to_dict())
    assert adapter.ticks == []
    service._control_tick_once(now=105.0)
    assert len(adapter.ticks) == 1
    assert adapter.ticks[0].controller_dt_s == 5.0
    assert adapter.ticks[0].growth_sample is not None
    assert adapter.ticks[0].growth_sample.frame_seq == 2
    assert adapter.ticks[0].growth_sample.dt_s == 19.0


def test_shadow_reads_process_but_default_gate_makes_zero_writes() -> None:
    clock = FakeClock()
    process = RecordingProcess()
    service = ControllerService(
        settings(opcua=True, write=False),
        adapter=RecordingAdapter(ControllerStepResult(T_j_set=299.0)),
        process_adapter=process,
        monotonic_clock=clock,
    )
    start_service(service)
    service._accept_sample(sample(1).to_dict())
    status = service._control_tick_once(now=105.0)
    assert process.read_count == 1
    assert process.write_count == 0
    assert status["process_write_attempted"] is False
    assert service.status()["integrations"]["opcua"]["shadow_mode"] is True


def test_explicit_write_gate_allows_valid_setpoint_write() -> None:
    clock = FakeClock()
    process = RecordingProcess()
    service = ControllerService(
        settings(opcua=True, write=True),
        adapter=RecordingAdapter(ControllerStepResult(T_j_set=299.0)),
        process_adapter=process,
        monotonic_clock=clock,
    )
    start_service(service)
    service._accept_sample(sample(1).to_dict())
    status = service._control_tick_once(now=105.0)
    assert process.write_count == 1
    assert status["process_write_attempted"] is True
    assert status["process_write_succeeded"] is True


def test_simulation_tick_runs_with_no_process_and_no_growth_message() -> None:
    adapter = RecordingAdapter()
    service = ControllerService(settings(opcua=False), adapter=adapter)
    start_service(service, run_type="simulation")
    result = service._control_tick_once(now=105.0)
    assert result["executed"] is True
    assert result["growth_frame_seq"] is None
    assert adapter.ticks[0].process_state is None
    assert service.state == ControllerState.RUNNING


def test_invalid_result_never_passes_write_gate() -> None:
    process = RecordingProcess()
    service = ControllerService(
        settings(opcua=True, write=True),
        adapter=RecordingAdapter(
            ControllerStepResult(valid=False, error="optimizer unavailable")
        ),
        process_adapter=process,
    )
    start_service(service)
    service._accept_sample(sample(1).to_dict())
    status = service._control_tick_once(now=105.0)
    assert process.write_count == 0
    assert status["output_valid"] is False
    assert status["process_write_attempted"] is False


def test_noop_default_produces_no_control_output() -> None:
    service = ControllerService(settings(opcua=False))
    start_service(service, run_type="simulation")
    status = service._control_tick_once(now=105.0)
    assert status["output_generated"] is False
    assert service.control_output_count == 0


def test_adapter_exception_moves_service_to_error_and_disconnects_process() -> None:
    class FailingAdapter(RecordingAdapter):
        def step(self, sample: Any, process_state: ProcessState | None = None):
            raise RuntimeError("state corruption")

    process = RecordingProcess()
    service = ControllerService(
        settings(opcua=True),
        adapter=FailingAdapter(),
        process_adapter=process,
    )
    start_service(service)
    service._accept_sample(sample(1).to_dict())
    try:
        service._control_tick_once(now=105.0)
    except RuntimeError as exc:
        assert "adapter step failed" in str(exc)
    else:
        raise AssertionError("Expected adapter failure")
    assert service.state == ControllerState.ERROR
    assert process.connected is False


def test_experiment_without_process_reads_is_rejected() -> None:
    service = ControllerService(settings(opcua=False), adapter=RecordingAdapter())
    service._apply_parameters(
        {
            "version": 1,
            "params": {
                "dt": 5.0,
                "dt_G": 15.0,
                "run_type": "experiment",
                "growth_rate_source": "live_gsensor",
            },
        }
    )
    try:
        service._start_experiment(
            {
                "run_id": "run-1",
                "parameter_version": 1,
                "started_at": "2026-08-26T12:00:00Z",
            }
        )
    except ValueError as exc:
        assert "requires CONTROLLER_OPCUA_ENABLED" in str(exc)
    else:
        raise AssertionError("Expected experiment read-gate rejection")


def test_write_gate_cannot_be_enabled_without_read_gate() -> None:
    try:
        settings(opcua=False, write=True)
    except ValueError as exc:
        assert "requires CONTROLLER_OPCUA_ENABLED" in str(exc)
    else:
        raise AssertionError("Expected invalid OPC write-only configuration")


def test_algorithm_controller_shadow_window_reads_eight_times_and_never_writes() -> None:
    clock = FakeClock()
    process = RecordingProcess()
    service = ControllerService(
        settings(opcua=True, write=False),
        adapter=CrystallizationControllerAdapter(),
        process_adapter=process,
        monotonic_clock=clock,
    )
    start_service(service)
    service._accept_sample(sample(1).to_dict())
    for tick_index in range(1, 9):
        now = 100.0 + tick_index * 5.0
        clock.value = now
        result = service._control_tick_once(now=now)
        assert result["output_generated"] is True
        assert result["output_valid"] is True
        assert result["process_write_attempted"] is False
    assert process.read_count == 8
    assert process.write_count == 0
    assert service.control_output_count == 8
