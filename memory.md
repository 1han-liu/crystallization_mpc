# 任务记忆

本文档用于记录本项目协作过程中已经完成的任务、关键决策、验证结果和遗留事项。每次任务完成后，在“任务记录”中追加一条记录，便于后续代理快速接续上下文。

## 记录模板

```markdown
### YYYY-MM-DD - 任务标题

- 任务：简要说明本次要解决的问题。
- 变更文件：列出主要新增或修改的文件。
- 验证：记录已执行的检查、测试或未运行测试的原因。
- 备注：记录关键决策、限制、后续注意事项或未处理问题。
```

## 任务记录

### 2026-05-07 - 创建项目代理指南

- 任务：根据当前项目内容生成根目录 `agent.md`，作为后续编码代理的项目指南。
- 变更文件：新增 `agent.md`。
- 验证：读取 `agent.md` 确认中文内容和 UTF-8 显示正常；检查 `git status`，确认未触碰已有 UI 静态文件改动。
- 备注：文档覆盖项目概览、FastAPI/RabbitMQ/InfluxDB 架构、关键入口、常用命令、开发约束和测试提示。

### 2026-05-07 - 补充 scripts 正弦波测试说明

- 任务：根据用户补充，将 `scripts` 中基于正弦波的 InfluxDB 测试脚本写入代理指南。
- 变更文件：更新 `agent.md`。
- 验证：搜索 `scripts/run_*_influx_test.py`，确认多份脚本使用 `math.sin` 生成测试遥测；读取 `agent.md` 相关段落确认说明已加入。
- 备注：这些脚本用于生成温度、浓度、sigma、目标值、滞后量等测试数据，通常使用 `source=test_sine` 标签，并要求 InfluxDB 配置可用。

### 2026-05-07 - 新增任务记忆与计划记录文档

- 任务：新增 `memory.md` 和 `plan.md`，用于后续记录已完成任务与计划内容。
- 变更文件：新增 `memory.md`、`plan.md`。
- 验证：文档-only 改动，不运行 pytest；完成后读取文件并检查 `git status`。
- 备注：两个文件均面向项目协作和编码代理使用，不作为最终用户说明书。

### 2026-05-07 - Gsensor UI 参数侧边栏与实验调参

- 任务：在独立 Gsensor UI 左侧增加参数侧边栏，支持实验过程中查看和修改 gsensor 所需参数；`Apply` 后立即更新当前 gsensor 内存参数并保存到 `params_runtime.yaml`，不修改 `params_default.yaml`。
- 变更文件：更新 `src/crystallization_mpc/apps/gsensor/app.py`、`src/crystallization_mpc/apps/gsensor/ui/static/index.html`、`app.js`、`styles.css`、`tests/test_growth_rate_commands.py`。
- 验证：`pytest tests\test_growth_rate_commands.py` 通过；`python -m compileall src\crystallization_mpc\apps\gsensor tests\test_growth_rate_commands.py` 通过。
- 备注：`Reset` 最终定义为恢复 gsensor UI 参数到 `params_default.yaml` 的初始值，并写回 `params_runtime.yaml`；controller 参数保留 runtime 中已有值，不被 Gsensor UI 重置。

### 2026-05-07 - Docker 构建上下文与 compose 警告修复

- 任务：修复 Docker build 因 pytest 临时目录 `.pytest-tmp*` 权限异常导致构建上下文发送失败的问题，并移除 obsolete 的 compose `version` 字段。
- 变更文件：更新 `.dockerignore`、`.gitignore`、`docker-compose.yml`。
- 验证：尝试运行 `docker compose build central gsensor`；项目侧 `.pytest-tmp*` 上下文问题已处理，但当前环境 Docker 配置目录 `C:\Users\Jack\.docker` 权限阻止进一步验证。
- 备注：`.pytest-tmp*/` 已加入 `.dockerignore` 和 `.gitignore`；后续 Docker 若失败，应优先检查 Docker 用户配置权限而非项目构建上下文。

### 2026-05-07 - 诊断面板折叠与 Gsensor 刷新行为调整

- 任务：将 Central UI 的 `Derived Preview`、`Publish Result` 和 Gsensor UI 的 `Active Parameters`、`Last Message` 改为默认折叠，仅作为故障排查工具；去掉 Gsensor UI 每 2 秒自动刷新。
- 变更文件：更新 `src/crystallization_mpc/apps/central/ui/static/index.html`、`app.js`、`styles.css`，以及 `src/crystallization_mpc/apps/gsensor/ui/static/index.html`、`app.js`、`styles.css`。
- 验证：`pytest tests\test_growth_rate_commands.py` 通过；`python -m compileall src\crystallization_mpc\apps\central src\crystallization_mpc\apps\gsensor` 通过。
- 备注：Gsensor UI 状态现在只在页面加载、点击 `Refresh`、`Apply`/`Reset` 后、窗口聚焦或页面重新可见时刷新；主内容布局改为按内容高度排列，避免 Gsensor UI 主板块被撑满页面。

### 2026-05-07 - 确认 Gsensor 功能补齐约束

- 任务：记录后续补齐 gsensor MATLAB 图像测量服务时的关键实现约束。
- 变更文件：更新 `memory.md`、`plan.md`。
- 验证：文档-only 改动，不运行 pytest；读取文档确认约束已记录。
- 备注：数据通讯全部通过 RabbitMQ 实现，不引入 OPC UA；gsensor 参数和状态结构以 `params_default.yaml` 中 `shared` 与 `gsensor` 参数为基础，并叠加 central 下发或 UI 写入的运行时参数。

### 2026-05-07 - 设计 Gsensor 参数 Influx 数据模型

- 任务：完成最小可用的 `gsensor_params` Influx 数据模型，固定参数记录的 measurement、tags、fields 和参数值序列化规则。
- 变更文件：新增 `src/crystallization_mpc/apps/gsensor/telemetry.py`、`tests/test_gsensor_param_telemetry.py`；更新 `src/crystallization_mpc/infra/influxdb/write.py`、`docs/interfaces.md`、`plan.md`、`memory.md`。
- 验证：运行 `pytest tests\test_gsensor_param_telemetry.py tests\test_growth_rate_commands.py`；运行 `python -m compileall src\crystallization_mpc\apps\gsensor src\crystallization_mpc\infra\influxdb tests\test_gsensor_param_telemetry.py`。
- 备注：`gsensor_params` 采用一参数一条 point；`param_key` 必须与 `params_default.yaml` 中的 key 完全一致；`scope=shared|gsensor` 表达参数归属，`value_float/value_string/value_bool/value_json` 避免 Influx 字段类型冲突。

### 2026-05-07 - 补充 Gsensor 业务流程分层

- 任务：把 MATLAB 原 gsensor 业务流程按参数/通信、生命周期、初始化/标定、每帧检测、图像算法、速率估计和 controller 接收层写入计划记录。
- 变更文件：更新 `plan.md`、`memory.md`。
- 验证：文档-only 改动，不运行 pytest；读取 `plan.md` 确认新增分层内容和 RabbitMQ 迁移约束一致。
- 备注：OPC UA 相关函数和节点仅作为 MATLAB 原流程通信边界参考；当前 Python 项目迁移时仍统一映射到 RabbitMQ，不引入 OPC UA。

### 2026-05-07 - Gsensor Web 初始化交互闭环

- 任务：按计划将 gsensor 初始化中的 MATLAB 桌面 UI 交互改为 FastAPI session + Web canvas 点选流程，并删除不再作为运行代码的 MATLAB UI 直译文件。
- 变更文件：新增 `src/crystallization_mpc/apps/gsensor/initialization.py`、`tests/test_gsensor_initialization.py`；更新 `src/crystallization_mpc/apps/gsensor/app.py`、`src/crystallization_mpc/apps/gsensor/ui/static/index.html`、`app.js`、`styles.css`、`params_default.yaml`、`param_meta.yaml`；删除 `choose_is_full`、`choose_corner`、`get_points`、`get_side` 以及 `annotate_point`、`make_line`、`make_arrow`、`mark_point`、`create_button`、`calc_button_position`、`initialize_DSCGR` 等 MATLAB 桌面 UI 直译运行文件。
- 验证：`python -m compileall src\crystallization_mpc\apps\gsensor tests\test_gsensor_initialization.py` 通过；`pytest tests\test_gsensor_initialization.py tests\test_gsensor_param_telemetry.py tests\test_growth_rate_commands.py` 通过，结果为 17 passed，另有 pytest cache 权限 warning。
- 备注：保留纯算法/几何转译函数，例如 `calc_intersect`、`calc_foot_point`、`calc_normal`、`reorient_points`、`calc_kernel_mask`；Web 初始化当前完成 2D 点选、full/non-full 分支、kernel 点、corner 选择和 overlay 数据，3D 恢复、`calc_2d_3d_info`、YOLO/Hough、增长率和 RabbitMQ 发布仍等待后续 MATLAB 源码接入。端口 `8001` 在当前 Windows 环境绑定失败，前台验证 `8011` 可启动。

### 2026-05-21 - 迁移 Gsensor recover_3d

- 任务：将 MATLAB `recover_3d.m` 转换到 Python `apps/gsensor/morphs`，保持固定 `m/w/u/v` 2D 坐标、`M.z = 0`，通过角度误差优化求解 `W/U/V` z 值的业务逻辑。
- 变更文件：新增 `src/crystallization_mpc/apps/gsensor/morphs/recover_3d.py`、`tests/test_recover_3d.py`；更新 `pyproject.toml`、`plan.md`。
- 验证：`python -m compileall src\crystallization_mpc\apps\gsensor\morphs tests\test_recover_3d.py` 通过；`pytest tests\test_recover_3d.py tests\test_gsensor_initialization.py` 通过，结果为 8 passed，另有 pytest cache 权限 warning。
- 备注：MATLAB `fmincon` 用 SciPy `minimize(method="SLSQP")` 对应实现，线性不等式与上下界按原方向分支迁移；`get_angles_d` 与 `calc_angle_d` 当前按缺失依赖占位，后续收到源码后补齐真实实现。

### 2026-05-22 - 迁移 Gsensor get_angles_d

- 任务：将 MATLAB `get_angles_d.m` 转换到 Python `apps/gsensor/morphs`，按 corner `A/B/C` 返回 `UMW`、`VMW`、`UMV` 三个理论角度。
- 变更文件：新增 `src/crystallization_mpc/apps/gsensor/morphs/get_angles_d.py`、`tests/test_get_angles_d.py`；更新 `plan.md`。
- 验证：`python -m compileall src\crystallization_mpc\apps\gsensor\morphs tests\test_get_angles_d.py tests\test_recover_3d.py` 通过；`pytest tests\test_get_angles_d.py tests\test_recover_3d.py` 通过。
- 备注：`recover_3d` 的角度表依赖已补齐；`calc_angle_d` 仍等待后续 MATLAB 源码接入。

### 2026-05-22 - 迁移 Gsensor calc_angle_d

- 任务：将 MATLAB `calc_angle_d.m` 转换到 Python `apps/gsensor/morphs`，按 `acosd(dot(line1,line2)/(norm(line1)*norm(line2)))` 计算向量夹角。
- 变更文件：新增 `src/crystallization_mpc/apps/gsensor/morphs/calc_angle_d.py`、`tests/test_calc_angle_d.py`；更新 `src/crystallization_mpc/apps/gsensor/morphs/recover_3d.py`、`plan.md`。
- 验证：`python -m compileall src\crystallization_mpc\apps\gsensor\morphs tests\test_calc_angle_d.py tests\test_get_angles_d.py tests\test_recover_3d.py` 通过；`pytest tests\test_calc_angle_d.py tests\test_get_angles_d.py tests\test_recover_3d.py` 通过。
- 备注：`recover_3d` 的 `get_angles_d` 与 `calc_angle_d` 依赖均已补齐，已移除缺失依赖占位导入；零范数输入保持 MATLAB 公式语义，结果为 NaN。

### 2026-05-22 - 拆分迁移 Gsensor recover_3d_all

- 任务：将 MATLAB `recover_3d_all.m` 拆分迁移为后端 3D 候选生成与 Web 候选选择流程；不迁移 MATLAB `figure/axes/tiledlayout/uiwait/button` 桌面 UI。
- 变更文件：新增 `src/crystallization_mpc/apps/gsensor/morphs/recover_3d_all.py`、`tests/test_recover_3d_all.py`；更新 `src/crystallization_mpc/apps/gsensor/initialization.py`、`app.py`、`ui/static/index.html`、`app.js`、`styles.css`、`tests/test_gsensor_initialization.py`、`plan.md`。
- 验证：`python -m compileall src\crystallization_mpc\apps\gsensor tests\test_recover_3d_all.py` 通过；`pytest tests\test_recover_3d_all.py tests\test_gsensor_initialization.py tests\test_calc_angle_d.py tests\test_get_angles_d.py tests\test_recover_3d.py` 通过，结果为 22 passed；`pytest tests\test_growth_rate_commands.py` 通过，结果为 7 passed；另有 pytest cache 权限 warning；实际调用 `recover_3d_all` 验证 full/non-full 候选数量为 1/16。
- 备注：full mode 生成 `choice=2`、`direction=outwards`；non-full mode 按 MATLAB 原 16 个 direction 顺序生成候选。初始化状态改为 `ready_for_corner -> ready_for_3d_choice -> ready_for_3d`，最终选择保存 `selected_3d_choice` 与 `recovered_3d`；真实 `show_3d` 半透明 patch 展示等待后续 MATLAB 源码。

### 2026-05-22 - Gsensor 初始化改为手动选择本地图片文件夹

- 任务：将 Gsensor 初始化图片入口从依赖 Docker/服务器内文件夹路径，改为仅通过浏览器手动选择本地文件夹并上传图片；不再保留服务器路径输入作为后备。
- 变更文件：更新 `.gitignore`、`src/crystallization_mpc/apps/gsensor/app.py`、`src/crystallization_mpc/apps/gsensor/ui/static/index.html`、`app.js`、`styles.css`、`tests/test_gsensor_initialization.py`、`plan.md`。
- 验证：`python -m compileall src\crystallization_mpc\apps\gsensor tests\test_gsensor_initialization.py` 通过；`pytest tests\test_recover_3d_all.py tests\test_calc_angle_d.py tests\test_get_angles_d.py tests\test_recover_3d.py tests\test_gsensor_initialization.py tests\test_growth_rate_commands.py` 通过，结果为 31 passed，另有 pytest cache 权限 warning。
- 备注：前端使用 `webkitdirectory` 文件夹选择控件并以 JSON/base64 上传支持的图片扩展名，避免新增 `python-multipart` 运行依赖；后端保存到 `.runtime/gsensor_uploads` 并复用现有 `start_folder` 扫描逻辑，公开初始化入口只保留上传 API，`.runtime/` 已加入 `.gitignore`。

### 2026-05-22 - 移除 Gsensor 初始化服务器路径后备

- 任务：删除 Gsensor 初始化中手动输入服务器文件夹路径的后备方案，只保留浏览器手动选择本地文件夹并上传图片的入口。
- 变更文件：更新 `docker-compose.yml`、`params_default.yaml`、`params_runtime.yaml`、`param_meta.yaml`、`src/crystallization_mpc/apps/gsensor/app.py`、`src/crystallization_mpc/apps/gsensor/ui/static/index.html`、`app.js`、`styles.css`、`tests/test_gsensor_initialization.py`、`tests/test_gsensor_param_telemetry.py`、`plan.md`、`memory.md`。
- 验证：`.venv\Scripts\python -m compileall src\crystallization_mpc\apps\gsensor tests\test_gsensor_initialization.py tests\test_gsensor_param_telemetry.py` 通过；`.venv\Scripts\python -m pytest tests\test_gsensor_initialization.py tests\test_gsensor_param_telemetry.py tests\test_growth_rate_commands.py` 通过，结果为 24 passed，另有 pytest cache 权限 warning。
- 备注：公开 API 删除 `/api/initialization/folder`，前端删除 Server Folder 输入；Docker 不再挂载 `GSENSOR_IMAGE_HOST_DIR:/data/images`；`image_folder` 不再作为 gsensor 参数发布。上传后的服务端临时目录仍用于后端读取和返回初始化图片。

### 2026-05-22 - 调整 Gsensor Image Marking Reset 语义

- 任务：将 Image Marking 的 `Reset` 从删除整个初始化 session 改为仅保留当前 load 的图片，清空 full/non-full 选择、点选、corner 和 3D 候选/选择结果，避免用户 reset 后必须重新 load 图片。
- 变更文件：更新 `src/crystallization_mpc/apps/gsensor/initialization.py`、`tests/test_gsensor_initialization.py`、`memory.md`。
- 验证：`.venv\Scripts\python -m compileall src\crystallization_mpc\apps\gsensor tests\test_gsensor_initialization.py` 通过；`.venv\Scripts\python -m pytest tests\test_gsensor_initialization.py tests\test_growth_rate_commands.py` 通过，结果为 19 passed，另有 pytest cache 权限 warning。
- 备注：UI 传入 `session_id` 时 reset 保留当前图片，并回到刚 load 完图片后等待选择 full/non-full 的步骤；无 `session_id` 的 `reset()` 仍作为服务内部/测试清理入口删除当前 active session。

### 2026-05-28 - Gsensor 3D 预览叠加 2D footprint

- 任务：在生成的 3D 候选预览中同时展示由选点数据得到的 2D footprint，方便实验员比较 non-full 模式下异常 3D 候选。
- 变更文件：更新 `src/crystallization_mpc/apps/gsensor/utils/show_3d.py`、`src/crystallization_mpc/apps/gsensor/ui/static/app.js`、`src/crystallization_mpc/apps/gsensor/ui/static/index.html`、`tests/test_show_3d.py`、`memory.md`。
- 验证：`.venv\Scripts\python -m compileall src\crystallization_mpc\apps\gsensor tests\test_show_3d.py` 通过；设置 `GSENSOR_UPLOAD_ROOT=tests\.tmp_gsensor_uploads` 后运行 `.venv\Scripts\python -m pytest tests\test_show_3d.py tests\test_recover_3d_all.py tests\test_gsensor_initialization.py -p no:cacheprovider` 通过，结果为 17 passed。
- 备注：`show_3d` payload 现在携带 `reference_2d`，使用 M/W/U/V 的原始 x/y 并固定 z=0；前端 3D canvas 将该 footprint 作为蓝色虚线参考层和 3D patch 同时绘制。首次未设置测试上传目录时，`tests/test_gsensor_initialization.py` 中两个上传 API 用例因本地 `.runtime/gsensor_uploads` 权限失败，隔离到测试目录后通过。

### 2026-05-28 - Gsensor 3D 候选视角快照

- 任务：在实验员旋转 3D 候选时保留当前视角快照，并在切换候选时沿用上一候选的 yaw/pitch，减少 non-full 多候选比较时的重复手动旋转。
- 变更文件：更新 `src/crystallization_mpc/apps/gsensor/ui/static/app.js`、`src/crystallization_mpc/apps/gsensor/ui/static/index.html`、`src/crystallization_mpc/apps/gsensor/ui/static/styles.css`、`memory.md`。
- 验证：`.venv\Scripts\python -m compileall src\crystallization_mpc\apps\gsensor tests\test_show_3d.py` 通过；设置 `GSENSOR_UPLOAD_ROOT=tests\.tmp_gsensor_uploads` 后运行 `.venv\Scripts\python -m pytest tests\test_show_3d.py tests\test_recover_3d_all.py tests\test_gsensor_initialization.py -p no:cacheprovider` 通过，结果为 17 passed；`git diff --check` 无空白错误。
- 备注：前端在 3D canvas pointer up 时更新当前候选快照，切换候选前也会保存当前画面；快照按初始化 session + corner 作用域隔离，重新选择 corner 或新 session 会清空旧快照。当前环境没有 `node` 命令，因此未执行 JS 语法检查。
