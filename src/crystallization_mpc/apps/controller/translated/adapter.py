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
        self.controller.running = True

    def step(
        self,
        sample: GrowthRateSamplePayload,
        process_state: ProcessState | None = None,
    ) -> ControllerStepResult | None:
        # 对应 controller_for_gui.m 中 while true 主循环的一轮。
        # 真正的 MATLAB 转译计算写在 matlab_controller.py 的 step()。
        if process_state is None:
            raise RuntimeError(
                "MatlabControllerAdapter 需要真实过程状态。"
                "使用仿真模式前，需要另外提供明确的仿真输入 Adapter。"
            )

        # sample 取代 MATLAB read_growth_rate.m：里面已有四个 G 值。
        # process_state 取代 measure_controller_data.m 里的设备读取代码：
        # 里面已有 OPC UA 62552 读到的 T、T_j、c、count_middle、T_j_set。
        # 这里把它们拆开，交给 matlab_controller.py 完成一轮计算。
        output = self.controller.step(
            G_u=sample.G_u,
            G_u_KF=sample.G_u_KF,
            G_v=sample.G_v,
            G_v_KF=sample.G_v_KF,
            T=process_state.T,
            T_j=process_state.T_j,
            c=process_state.c,
            count_middle=process_state.count_middle,
            current_T_j_set=process_state.T_j_set,
            dt_s=sample.dt_s,
            frame_seq=sample.frame_seq,
        )

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
