""" MATLAB Controller 接进 Python 时，主要填写这个文件。

最直接的对应关系：

1. MATLAB ``source_codes/subroutines_controller`` 里的计算函数：
   一个 ``.m`` 文件转成一个同名或容易认出的 ``.py`` 文件，继续放在
   ``translated/subroutines_controller`` 下面。这里不要求把所有代码塞进一个文件。

2. MATLAB ``source_codes/controller_for_gui.m`` 里的主循环：
   不用整段照抄。它在 Python 中被拆成了 ``start()`` 和 ``step()``：

   - 点击 Start 时只做一次的内容，写进 ``start()``；
   - MATLAB ``while true`` 每轮都会做的计算，写进 ``step()``。

3. MATLAB ``source_codes/gui_main/snipptets_controller`` 里的小脚本：
   按下面每个函数旁边写明的文件名，转译到对应位置。

4. 下面这些 MATLAB 旧通信代码不要转译进本文件：

   - ``connect_opcua_node``、``receive_params``、``receive_modified``：
     已由 RabbitMQ 和 Central 取代；
   - ``read_from_gsensor_to_controller``：已由 RabbitMQ 取代；
   - ``create_nodes``、``readValue``、``writeValue``：
     已由外面的 ``process.py`` 统一处理真实设备 OPC UA；
   - 画图、按钮和弹窗：已由 Central UI 处理。
"""

from __future__ import annotations

import copy
from typing import Any, Mapping

from crystallization_mpc.apps.controller.result import ControllerStepResult


class MatlabController:
    """保存 MATLAB Controller 原来放在工作区里的变量。"""

    def __init__(self) -> None:
        # 这些是 Python 工程新增的变量，不需要去 MATLAB 中寻找。
        self.params: dict[str, Any] = {}
        self.run_id: str | None = None
        self.configured = False
        self.running = False

        # MATLAB 来源：gui_main/op_section.m。
        # adaptive 是“是否开始参数自适应”，adaptive_mode 是“自适应哪个参数”。
        self.adaptation_enabled = False
        self.adaptation_mode = "E_A"
        self._reset_runtime_state()

    def _reset_runtime_state(self) -> None:
        """每次新实验开始时，清空上一次实验留下的数据。"""

        # MATLAB 来源：
        # - gui_main/snipptets_controller/initialize_controller_for_gui.m
        # - subroutines_controller/utils/initialize_lists.m
        # - subroutines_controller/utils/initialize_G.m
        # - gui_main/snipptets_controller/refresh_controller.m
        # - gui_main/snipptets_controller/refresh_adaptive.m

        # 对应 MATLAB 变量 ii，表示现在算到第几轮。
        self.frame_index = 0

        # 对应 MATLAB 中的同名 list，用来保存每一轮的结果。
        self.T_list: list[float] = []
        self.T_j_list: list[float] = []
        self.c_list: list[float] = []
        self.count_middle_list: list[float] = []
        self.G_measure_list: list[float] = []
        self.G_measure_KF_list: list[float] = []
        self.sigma_list: list[float] = []
        self.T_j_set_list: list[float] = []

        # 组员需要补充：MATLAB 里下一轮还要继续使用的变量都写成 self.xxx。
        # 例如 refresh_controller.m 里的 EKF、int_e_target_dt、
        # int_e_dT_dt_dt、int_e_T_dt，不能只写成 step() 里面的临时变量。
        self.algorithm_state: dict[str, Any] = {}

    def configure(self, params: Mapping[str, Any], run_id: str) -> None:
        """接收 Central 发来的本次实验参数。"""

        # 这个函数没有需要转译的 MATLAB 源文件。
        # MATLAB 原来直接从工作区读取 params；Python 改成由 Central 把
        # params 和 run_id 送到这里。组员只需用 self.params 读取实验参数。
        if not isinstance(params, Mapping):
            raise TypeError("Controller 参数必须是键值映射。")
        if not str(run_id).strip():
            raise ValueError("Controller 必须提供 run_id。")
        self.params = copy.deepcopy(dict(params))
        self.run_id = str(run_id).strip()
        self.configured = True
        self.running = False
        self._reset_runtime_state()

    def start(self) -> None:
        """实验人员点击 Start 后只运行一次。"""

        if not self.configured:
            raise RuntimeError("调用 start() 前必须先配置 MatlabController。")
        self._reset_runtime_state()

        # ===== 这里对应 MATLAB“实验刚开始时运行一次”的代码 =====
        # 按下面顺序转译：
        #
        # 1. gui_main/snipptets_controller/initialize_controller.m
        #    把 ii、时间、size_list_seed、size_list 等算法初始值放进 self.xxx。
        #    该文件里的 create_nodes()、readValue() 不要复制，设备读取在外面完成。
        #
        # 2. gui_main/snipptets_controller/refresh_controller.m
        #    创建 Controller 用的 EKF，并把三个积分项清零。
        #
        # 3. gui_main/snipptets_controller/refresh_adaptive.m
        #    创建 G_measure、n、k_0、E_A 参数自适应用的 EKF。
        #
        # 4. subroutines_controller/utils/initialize_lists.m
        #    和 subroutines_controller/utils/initialize_G.m
        #    建立 MATLAB 原来使用的各个历史结果 list。
        #
        # 组员完成以上内容后，删除下面的 NotImplementedError。
        raise NotImplementedError(
            "转译后的 Controller 初始化逻辑尚未实现。"
        )

    def step(
        self,
        *,
        G_u: float,
        G_u_KF: float,
        G_v: float,
        G_v_KF: float,
        T: float,
        T_j: float,
        c: float,
        count_middle: float,
        current_T_j_set: float,
        dt_s: float,
        frame_seq: int,
    ) -> ControllerStepResult | Mapping[str, Any] | None:
        """每收到一组新的 Gsensor 数据就运行一次。"""

        if not self.running:
            raise RuntimeError("MatlabController 当前没有运行。")

        # 外面的程序已经准备好了输入，直接使用即可：
        # G_u、G_u_KF、G_v、G_v_KF：Gsensor 结果，单位 m/s。
        # T、T_j、current_T_j_set：设备温度，单位 K。
        # c、count_middle：设备提供的过程数据。
        # dt_s：两次 Gsensor 测量之间相隔多少秒。
        # frame_seq：现在处理第几组 Gsensor 结果。

        # ===== 这里对应 controller_for_gui.m 的 while true 主循环 =====
        # 每收到一张新图片算出的四个 G 值，Python 就会调用一次 step()。
        # 请保持 MATLAB 主循环的计算顺序：
        #
        # 1. gui_main/snipptets_controller/record_controller_time.m
        #    更新 MATLAB 变量 ii 和本轮时间；Python 中可用 frame_seq 和 dt_s。
        #
        # 2. gui_main/snipptets_controller/measure_controller_data.m
        #    把本轮 T、T_j、c、count_middle 保存进相应 list。
        #    不要复制 create_nodes()、read_value()、safe_read_value()，因为这些数值
        #    已经作为上面的函数参数传进来了。
        #
        # 3. gui_main/snipptets_controller/control_target.m
        #    它只是依次执行下面三个 MATLAB 文件：
        #    - calculate_target.m：算温度/浓度变化、EKF、sigma、G 和控制误差；
        #    - calculate_lag_time.m：算 t_lag；
        #    - calculate_T_j_set.m：最终算出新的 T_j_set。
        #
        # 4. MATLAB 的 read_growth_rate.m 不要转译。
        #    它原来负责读取 G_u、G_u_KF、G_v、G_v_KF；这四个值现在已经由
        #    RabbitMQ 送到 step() 的参数中，直接使用即可。
        #
        # 5. 如果 self.adaptation_enabled 为 True，执行
        #    gui_main/snipptets_controller/execute_growth_parameters_adaption.m，
        #    用四个 G 值更新 G_measure、G_measure_KF 和选中的生长参数。
        #
        # 6. 执行 gui_main/snipptets_controller/record_growth_parameters.m，
        #    把 n、k_0、E_A 等本轮结果保存到相应 self.xxx_list。
        #
        # 7. controller_for_gui.m 最后的 writeValue(...) 不要复制。
        #    step() 只需要返回 T_j_set，外面的 process.py 会检查后写入 OPC UA。

        # 计算完成后，至少像下面这样返回真实结果：
        # return {
        #     "sigma": sigma,
        #     "G_measure": G_measure,
        #     "G_measure_KF": G_measure_KF,
        #     "T_j_set": T_j_set,
        # }
        # 外面的程序会负责检查 T_j_set、写 OPC UA 和写 InfluxDB。
        # 如果这一轮还没有真实结果，就 return None，不要填写假数值。
        #
        # 完成以上内容后，删除下面的 NotImplementedError。
        raise NotImplementedError(
            "转译后的 Controller 单轮计算逻辑尚未实现。"
        )

    def stop(self) -> None:
        """实验人员点击 Stop/End 后运行。"""

        # 对应 controller_for_gui.m 中 controller_active 变为 false 后停止计算。
        # 这里不需要复制 MATLAB 的 OPC UA 或 while 循环代码，只把算法停下来。
        self.running = False

    def add_seed(self, event: Mapping[str, Any]) -> None:
        """实验人员点击 Add Seed 后运行。"""

        # MATLAB 来源：
        # - gui_main/op_section.m 中的 add_seed_request 按钮；
        # - gui_main/snipptets_controller/measure_controller_data.m 中
        #   ``if add_seed_request`` 的两段代码。
        # Python 不复制按钮，只把 MATLAB 点击按钮后修改的 size_list、mark_seed
        # 等算法变量写在这里。
        return None

    def set_adaptation(
        self,
        enabled: bool,
        mode: str,
        event: Mapping[str, Any] | None = None,
    ) -> None:
        """实验人员修改参数自适应开关或模式后运行。"""

        # MATLAB 来源：gui_main/op_section.m 中的 adaptive 和 adaptive_mode。
        # enabled 对应 adaptive；mode 对应 adaptive_mode。
        # step() 执行 execute_growth_parameters_adaption.m 时读取这两个值。
        self.adaptation_enabled = enabled
        self.adaptation_mode = mode

    def export_state(self) -> Mapping[str, Any] | None:
        """保存当前算法数据，供 Controller 程序重启后继续实验。"""

        # 没有对应的 MATLAB 源文件，这是 Python 工程为了重启恢复而预留的功能。
        # 组员暂时不做就保持 None；以后需要时再保存各个 list、积分项和 EKF 数值。
        return None

    def restore_state(
        self,
        params: Mapping[str, Any],
        run_id: str,
        state: Mapping[str, Any],
    ) -> bool:
        """把 export_state() 保存的数据重新放回算法。"""

        # 没有对应的 MATLAB 源文件，这是 Python 工程为了重启恢复而预留的功能。
        # 暂时不做就保持 False；成功恢复全部数据后改为返回 True。
        return False


__all__ = ["MatlabController"]
