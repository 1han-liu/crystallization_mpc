# 计划记录

本文档用于保存后续待执行或已确认的计划内容。每次进入实施前，可以把最终确认的计划记录到这里；实施完成后，再把结果同步到 `memory.md`。

## 当前计划

### 2026-05-07 - Gsensor 功能补齐约束

- 目标：将 MATLAB 版 gsensor 图像测量服务迁移到 Python 项目骨架中。
- 范围：后续实现应沿用 `src/crystallization_mpc/apps/gsensor/` 下的 `detection/imgs/model/morphs/utils` 结构；通讯统一使用 RabbitMQ，不引入 OPC UA。
- 实施步骤：按 MATLAB 函数依赖逐步迁移主循环、图像读取/标定、YOLO 分割、Hough 边界检测、生长速率计算、EKF 滤波和 RabbitMQ 发布。
- 测试计划：先为纯计算和状态转换函数补单元测试；再用样例图像或合成检测结果验证边界位移、生长速率和 EKF 输出；最后验证 gsensor -> controller RabbitMQ 消息。
- 假设/风险：参数和状态结构以 `params_default.yaml` 中 `shared` 与 `gsensor` 参数为基础，并叠加 central 下发或 Gsensor UI 写入的运行时参数；具体消息 payload 需在看到 MATLAB 输出字段后锁定。
- 状态：执行中；Web 初始化 UI/API 闭环已完成，3D 恢复、每帧检测、增长率/EKF 和 RabbitMQ 发布仍等待后续 MATLAB 源码接入。

#### 当前完成情况

- 已完成：Gsensor Web 初始化 session 与 API，支持服务端图片文件夹扫描、first/latest 图片选择、full/non-full 选择、canvas 原图坐标点选、Undo/Reset、corner A/B/C 选择和 overlay 数据返回。
- 已完成：初始化阶段的 2D 几何推进已接入纯计算函数，包括 `calc_intersect`、`calc_foot_point`、`calc_normal`、`reorient_points`；full mode 可由 opposite/adjacent 线求 `u/v/w`，non-full mode 可由 foot point 求 `u/v` 并使用人工 `w`。
- 已完成：删除 MATLAB 桌面 UI 原语直译运行文件，不再保留 `figure/uiwait/getCursorInfo/uicontrol/pause` 等运行路径；保留纯算法/几何转译函数。
- 已完成：新增 `image_folder` gsensor 参数与元数据，供参数抽屉和初始化入口共享。
- 已验证：`python -m compileall src\crystallization_mpc\apps\gsensor tests\test_gsensor_initialization.py` 通过；`pytest tests\test_gsensor_initialization.py tests\test_gsensor_param_telemetry.py tests\test_growth_rate_commands.py` 通过，17 passed。
- 未完成：`recover_3d_all`、`recover_3d`、`show_3d`、`calc_2d_3d_info` 尚未迁移，初始化流程目前在 `ready_for_3d` 停住，不伪造 3D 结果。
- 未完成：`measure_growth_rate -> update_uv_struct -> update_line -> update_EKF_G` 每帧检测链路、YOLO/edge + Hough、增长率/EKF 输出和 gsensor -> controller RabbitMQ 发布尚未接入。
- 未完成：`gsensor_measurement` Influx measurement、检测失败/停止事件和测量结果持久化尚未实现。

#### Gsensor 业务流程核心分层

以下内容按 MATLAB 原实现梳理业务语义。涉及 OPC UA 的函数和节点只作为原通信边界参考；迁移到当前 Python 项目时，服务间通信仍统一映射为 RabbitMQ 消息，不引入 OPC UA。

- 参数/通信层：`parameters_G.m` 定义 `dt_G`、`resolution`、`q2`、`r_diag`、`exp_sim_G`、`params_G.*`；`startApp.m` 在 MATLAB 原流程中把参数和修改项发给 gsensor；`opcua_server.py` 定义原 OPC UA 节点，关键语义节点是 `gsensor_to_controller`；`write_from_gsensor_to_controller.m` 写出 `G_u`、`G_u_KF`、`G_v`、`G_v_KF`。
- 生命周期层：`initialize_gsensor_for_gui.m` 初始化 `G_active`、刷新标志和计时；`initialize_gsensor.m` 清空列表、设置 `G_ii = 0`、记录初始时间；`refresh_gsensor.m` 在实验模式下调用 `initialize_G`。
- 初始化/标定层：`initialize_G.m` 选择图像文件夹，找到第一张图并建立检测结构；`initialize_DSCGR.m` 读取初始图像，手工点选晶体边、角点和 kernel 区域；`get_points.m` 交互式标点，得到 u/v 边界以及 m/u/v/w 点；`recover_3d_all.m`、`calc_2d_3d_info.m` 恢复 3D 几何，得到 u/v 的法向量和初始 Hough 参数。
- 每帧检测层：`measure_growth_rate.m` 按模式取图，`simulation` 写入 missing，`experiment` 查找最新图像，`experiment_with_presaved` 按 `ptr` 逐张回放图像；对 u、v 两个方向分别执行 `update_uv_struct`、`update_line`、`update_EKF_G`。
- 图像算法层：`find_edge_points_yolov.m` 调用 YOLOv8-seg ONNX 模型，输出晶体边界 mask/edge；`calc_masked_image.m` 根据上一帧边界位置裁 ROI；`update_line.m` 在 ROI 内执行 Hough transform，查找当前边界线；位移定义为 `dist = abs(dot(line.point1 - t, n))`。
- 速率估计层：`update_EKF_G.m` 的状态骨架为 `x_G = [distance; growth_rate; acceleration]`；原始速率为 `G = (dist(ii) - dist(ii-1)) / dt_G * resolution`；EKF 输出为 `G_KF = x_G_array(2, ii)`。
- Controller 接收层：`controller_for_gui.m` 在 controller 激活后调用 `read_growth_rate`；`read_growth_rate.m` 在实验模式从原通信通道读取 gsensor 数据，纯仿真模式用过程模型伪造 `G_u/G_v`；迁移后 controller 侧应从 RabbitMQ 消费 gsensor 发布的 `G_u`、`G_u_KF`、`G_v`、`G_v_KF`。

#### Gsensor 初始化与测量流程细化

- 迁移约束：后续等待 MATLAB 源码逐段补齐；除 Python 语言适配、项目路径/API/UI 接入等必要改动外，逻辑部分尽量 1:1 还原，不额外扩展或重构算法行为。
- 图片加载入口：实验员在 Gsensor UI 中 load folder；图片文件名预计带时间标注。初始化时先在文件夹中找到第一张图片，或按交互选择第一张/最新图后进入 `initialize_DSCGR.m`。`initialize_DSCGR.m` 第 2-4 行通过 `fullfile` 与 `imread` 真正读取图像。
- full mode 选择：`initialize_DSCGR.m` 先调用 `choose_is_full.m` 询问晶体是否为 full mode，然后进入 `get_points.m` 执行人工标点。
- 人工选点交互：`get_points.m` 需要依次标注 u/v adjacent side 的两端点与 outer point；full mode 下还要标注 u/v opposite side；non-full mode 下改为标注 w 点；最后标注 4 个 kernel corner 与 4 个 kernel outer point。每次点选本质上通过 `mark_point.m` 等待用户用 data cursor 点图后按 ENTER。
- 2D 几何计算：`get_points.m` 用人工 2D 点计算几何关系；`m` 是 u/v adjacent 两条线的交点；full mode 通过 opposite/adjacent 线求 u/v/w；non-full mode 用 foot point 求 u/v，并使用人工标注的 w；kernel 点保存到 `kernel.k_c_cell` 与 `kernel.k_o_cell`。
- kernel 用途：`kernel.k_c_cell` 与 `kernel.k_o_cell` 后续供 `calc_kernel_mask.m` 在边缘检测中做遮罩与连通区域处理。
- 3D 生成时机：3D 图在初始化阶段、2D 点选完成并选择 corner A/B/C 后生成。`initialize_DSCGR.m` 第 9 行先调用 `choose_corner(I)`，第 10 行调用 `recover_3d_all(...)`。
- 3D 候选生成：`recover_3d_all.m` 第 34 行开始为每个候选方向生成 3D 结果；内部调用 `recover_3d(...)` 计算 `M/W/U/V` 的 3D 坐标，并调用 `show_3d(...)` 展示候选图。
- 3D 展示方式：`show_3d.m` 不是 `plot3` 或 `surf`，而是先 `imshow(I)`，再用 `patch(mesh, ...)` 绘制半透明 3D 面片。Python/UI 迁移时应保留“图像底图 + 半透明面片候选”的交互语义。
- 初始化后信息：3D 选定后，`calc_2d_3d_info.m` 用 `M/W/U/V` 计算 u/v 面的 3D 法向量、投影修正和 Hough 初始参数。
- 后续测量链路：每张新图按 `measure_growth_rate -> update_uv_struct -> update_line -> update_EKF_G` 执行。`update_line.m` 会重新 `imread` 图片，使用 YOLO/edge + Hough 查找当前边线，再计算相对初始边的距离，用于增长率和 EKF 更新。

#### InfluxDB/Grafana 数据持久化约定

- InfluxDB 写入应在补齐 gsensor 功能过程中同步实现，不完全前置，也不等所有图像算法完成后再补。
- RabbitMQ 负责服务间通讯；InfluxDB 负责参数快照、参数变更、测量结果、滤波结果和状态事件的长期持久化。
- Gsensor 的 Influx 数据模型就是 Grafana 后续要读取和展示的数据源结构，需要先定义 measurement、tags、fields。
- Central 下发给 gsensor 的参数也算 gsensor 数据模型的一部分，但应作为参数快照或参数事件存储，而不是混入每条生长速率测量点。
- 推荐先拆两类 measurement：`gsensor_params` 用于记录 `dt_G`、`resolution`、`params_G.*` 等参数来源和值；`gsensor_measurement` 用于记录图像编号、u/v 边界位置、位移、`G_u/G_v` raw、`G_u/G_v` EKF、quality 和错误状态。
- `gsensor_params` 采用一参数一条 point：measurement 为 `gsensor_params`；tags 为 `service`、`run_id`、`source`、`event`、`scope`、`param_key`、`value_type`；fields 为 `value_float`、`value_string`、`value_bool`、`value_json`、`version`、`seq`、`changed`。
- `param_key` 必须与 `params_default.yaml` 中的 key 完全一致，例如 `dt_G`、`resolution`、`ptr_format`、`params_G.width`、`params_G.ratio`；归属用 `scope=shared|gsensor` 表达，不另起参数名。
- 每次收到 central 参数、Gsensor UI 调参、启动测量完成几何标定、每个周期完成检测/EKF、检测失败或停止测量时，都应按对应类型写入 InfluxDB。
- 当前仓库已有基础 Influx 写入封装和正弦波测试脚本；正弦波数据已经能写入并在 Grafana 展示，说明本地 InfluxDB/Grafana 连接可用。
- 仓库中尚未看到 Grafana datasource/dashboard provisioning；当前 Grafana 展示很可能依赖手动配置和 Docker volume。Grafana 自动化配置不阻塞 gsensor 功能实现，可后续单独固化。

#### Influx 写入层清理顺序

- 正弦波生成逻辑属于 `scripts/run_*_influx_test.py`，只是链路诊断工具，不应长期体现在生产写入 API 的默认值中。
- `src/crystallization_mpc/infra/influxdb/write.py` 中的 `source="test_sine"`、`run_id="test_sigma_001"`、`build_sigma_point()`、`InfluxWriter.write_sigma()` 和模块级 `write_sigma()` 属于测试/旧便捷接口，后续可以删除。
- `build_tagged_point()`、`InfluxWriter.write_tagged_fields()`、`_field_value()` 以及 client/settings 逻辑应保留为生产通用写入层。
- 清理顺序：先把 `gsensor_params` 接入启动快照、central 参数更新、Gsensor UI Apply/Reset 等实际写入时机。
- 然后把正弦波脚本改为显式使用 `write_tagged_fields()`，不要依赖 `test_sine` 默认值或 sigma 专用 helper。
- 验证正弦波脚本仍可作为 InfluxDB/Grafana 链路诊断工具使用后，再从 `write.py` 删除正弦波默认值和 sigma 专用 helper。

## 计划模板

```markdown
### YYYY-MM-DD - 计划标题

- 目标：说明计划要达成的结果。
- 范围：说明会修改和不会修改的内容。
- 实施步骤：列出执行顺序和关键落点。
- 测试计划：列出需要运行的检查、测试或人工验证。
- 假设/风险：记录默认选择、前置条件和风险点。
- 状态：待执行 / 执行中 / 已完成 / 已取消。
```

## 历史计划

### 2026-05-07 - 新增 `memory.md` 和 `plan.md`

- 目标：在项目根目录新增两个中文 Markdown 文档，用于后续协作记录。
- 范围：只新增 `memory.md` 和 `plan.md`；不修改代码、配置、README、Docker 或 UI 文件。
- 实施步骤：创建 `memory.md` 的任务记录模板和初始记录；创建 `plan.md` 的计划模板、当前计划区域和本次计划记录。
- 测试计划：检查两个文件存在；读取 Markdown 确认中文内容、模板字段和用途说明清晰；文档-only 改动不运行 pytest。
- 假设/风险：两个文件面向项目协作和编码代理使用；初始记录日期使用 `2026-05-07`；保留已有未提交 UI 静态文件改动不触碰。
- 状态：已完成。
