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
