"""这个文件没有对应的 MATLAB 源文件，一般不要修改。

它只做一件事：把 Central、Gsensor 和真实设备送来的数据交给
``matlab_controller.py``，再把计算结果交回现有 Python 工程。
只需要填写 ``matlab_controller.py`` 和转译后的 Controller 计算函数。
"""

from __future__ import annotations

from typing import Any, Mapping

from crystallization_mpc.apps.controller.adapter import ControllerAdapter
from crystallization_mpc.apps.controller.process import ProcessState
from crystallization_mpc.apps.controller.result import ControllerStepResult
from crystallization_mpc.apps.controller.tick import ControllerTickInput
from crystallization_mpc.apps.controller.translated.matlab_controller import (
    MatlabController,
)
from crystallization_mpc.messaging.contracts import GrowthRateSamplePayload


class MatlabControllerAdapter(ControllerAdapter):
    """Python 工程和 MATLAB 转译代码之间的转接头；MATLAB 没有这个类。"""

    def __init__(self, controller: MatlabController | None = None) -> None:
        # 没有对应的 MATLAB 源代码。
        # 程序启动时创建一个 MatlabController，整次实验都使用同一个对象，
        # 这样 MATLAB 原来的 list、EKF 和积分项不会在每一轮计算后丢失。
        self.controller = controller or MatlabController()

    def configure(self, params: Mapping[str, Any], run_id: str) -> None:
        # MATLAB 原来直接从工作区拿 params；Python 改成由 Central 发来。
        # 这里原样交给 matlab_controller.py，组员不需要改。
        self.controller.configure(params, run_id)

    def start(self) -> None:
        # 对应 MATLAB 开始执行 initialize_controller、refresh_controller 和
        # refresh_adaptive 的时刻。真正的 MATLAB 转译内容写在
        # matlab_controller.py 的 start()，不写在这里。
        self.controller.start()

    def step(
        self,
        sample: GrowthRateSamplePayload | ControllerTickInput,
        process_state: ProcessState | None = None,
    ) -> ControllerStepResult | None:
        # 对应 controller_for_gui.m 中 while true 主循环的一轮。
        # 真正的 MATLAB 转译计算写在 matlab_controller.py 的 step()。
        if isinstance(sample, ControllerTickInput):
            tick = sample
        else:
            # Backward-compatible bridge for the pre-scheduler service. The
            # dedicated scheduler sends ControllerTickInput directly.
            controller_dt = float(self.controller.params.get("dt", sample.dt_s))
            tick = ControllerTickInput(
                tick_seq=self.controller.frame_index + 1,
                controller_dt_s=controller_dt,
                elapsed_s=(self.controller.frame_index + 1) * controller_dt,
                growth_sample=sample,
                growth_sample_age_s=0.0,
                process_state=process_state,
            )
        output = self.controller.step(tick)

        # MATLAB 算法还没产生真实结果时，不生成假的 Controller 输出。
        if output is None:
            return None

        # 如果组员已经返回 ControllerStepResult，直接交还外面的程序。
        if isinstance(output, ControllerStepResult):
            return output

        # 如果组员返回字典，就整理成外面的程序要求的 ControllerStepResult。
        if isinstance(output, Mapping):
            return ControllerStepResult.from_mapping(output)

        raise TypeError(
            "MatlabController.step() 必须返回 ControllerStepResult、"
            "结果字典或 None。"
        )

    def stop(self) -> None:
        # 对应 controller_for_gui.m 中 controller_active 变为 false。
        self.controller.stop()

    def add_seed(self, event: Mapping[str, Any]) -> None:
        # 对应 op_section.m 的 add_seed_request。
        self.controller.add_seed(event)

    def set_adaptation(
        self,
        enabled: bool,
        mode: str,
        event: Mapping[str, Any] | None = None,
    ) -> None:
        # 对应 op_section.m 的 adaptive 和 adaptive_mode。
        self.controller.set_adaptation(enabled, mode, event)

    def export_state(self) -> Mapping[str, Any] | None:
        # MATLAB 没有对应代码，这是 Python 工程预留的重启恢复功能。
        return self.controller.export_state()

    def restore_state(
        self,
        params: Mapping[str, Any],
        run_id: str,
        state: Mapping[str, Any],
    ) -> bool:
        # MATLAB 没有对应代码，这是 Python 工程预留的重启恢复功能。
        return self.controller.restore_state(params, run_id, state)


__all__ = ["MatlabControllerAdapter"]
