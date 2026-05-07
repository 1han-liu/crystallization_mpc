# Crystallization MPC Agent 指南

本文件面向后续编码代理，用于快速理解本仓库的结构、运行方式和协作约束。除非用户明确要求，不要把它当作用户手册扩写，也不要用它替代源码事实。

## 项目概览

这是一个 Python 包项目，包名为 `crystallization_mpc`，面向结晶过程的 MPC 系统集成骨架。当前重点是通过消息总线连接多个应用组件，并提供参数管理与基础 UI。

主要组件：

- `central`：中央控制 UI 和参数发布入口。
- `gsensor`：生长速率传感器 UI 和占位测量服务。
- `controller`：控制器相关包目录和配置骨架。
- `infra`：RabbitMQ、InfluxDB 等基础设施封装。
- `messaging`：消息 envelope、路由、编码和序列号工具。

## 架构与数据路径

- Web 层使用 FastAPI，静态页面位于各应用的 `ui/static` 目录。
- RabbitMQ topic exchange 名称为 `idp.bus`。
- 默认队列包括 `central.in`、`controller.in`、`gsensor.in`，路由键格式为 `{src}.to.{dst}`。
- 广播绑定使用 `broadcast.#`。
- 消息 envelope 由 `crystallization_mpc.messaging.schema.build_envelope` 生成，包含 `ver`、`ts`、`src`、`dst`、`msg_type`、`name`、`seq`、`correlation_id`、`payload`。
- 参数默认值在 `params_default.yaml`，运行时参数通常写入 `params_runtime.yaml`。
- 参数展示和发布元数据在 `param_meta.yaml`，操作面板元数据在 `operation_meta.yaml`。
- InfluxDB/Grafana 用于遥测存储和看板。接口文档中当前 measurement 包括 `crystallizer`，计划占位包括 `sensor_g`。

## 关键入口

- Central UI：`crystallization_mpc.apps.central.ui.app:web_app`
  - 默认 Docker 端口：`8000`
  - 负责加载参数、应用派生参数、发布参数消息、发送 `growth_rate.start` / `growth_rate.stop` 命令。
- Gsensor UI：`crystallization_mpc.apps.gsensor.app:web_app`
  - 默认 Docker 端口：`8001`
  - 负责接收参数更新和生长速率启停命令，并维护占位测量状态。
- 消息路由：`src/crystallization_mpc/messaging/routing.py`
- 参数加载、保存和派生计算：`src/crystallization_mpc/apps/central/params.py`
- RabbitMQ 连接、声明、发布、消费：`src/crystallization_mpc/infra/rabbitmq/`
- InfluxDB 查询和写入封装：`src/crystallization_mpc/infra/influxdb/`

注意：README 中部分运行说明可能落后于 `docker-compose.yml`。判断当前可运行服务时，以源码和 compose 文件为准。

## 常用命令

本项目要求 Python `>=3.10`。不要提交本地虚拟环境。

Windows PowerShell：

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -e .
```

运行测试：

```powershell
.venv\Scripts\python -m pytest
```

Docker 快速启动：

```powershell
docker compose up --build
```

Docker 暴露端口：

- Central UI：`http://localhost:8000`
- Gsensor UI：`http://localhost:8001`
- RabbitMQ：`localhost:5673`
- RabbitMQ management UI：`http://localhost:15673`
- InfluxDB：`http://localhost:8087`
- Grafana：`http://localhost:3000`

本地直接运行 FastAPI 应用时，可使用 uvicorn 指向对应 `web_app`，例如：

```powershell
.venv\Scripts\python -m uvicorn crystallization_mpc.apps.central.ui.app:web_app --host 0.0.0.0 --port 8000
.venv\Scripts\python -m uvicorn crystallization_mpc.apps.gsensor.app:web_app --host 0.0.0.0 --port 8001
```

## 开发约束

- 不要提交 `.venv/`、`__pycache__/`、`.pytest_cache/`、`.pytest-tmp*/`、`*.egg-info/`、`build/`、`dist/` 等生成文件。
- 仓库可能已有用户未提交改动。修改前先查看相关文件，不能回退或覆盖与任务无关的用户改动。
- 保持现有消息 envelope 字段和路由约定。新增消息时优先复用 `build_envelope`、`route`、`bindings_for`。
- 保持参数文件的三段结构：`shared`、`gsensor`、`controller`。Gsensor UI 写运行时参数时应保留 controller 参数。
- 派生参数应集中在 `central.params.apply_derived_params` 一类的现有路径中维护，避免在 UI 层重复计算。
- 修改 UI 时保持 FastAPI 静态文件结构，不要把独立页面逻辑散落到无关目录。
- 不要随意更改 Docker 端口、RabbitMQ exchange、队列名或环境变量名称，除非任务明确要求。
- 根目录中的早期单文件脚本如 `ekf.py`、`io_rabbit.py`、`io_influx.py` 看起来是历史或实验性代码；涉及生产路径时优先检查 `src/crystallization_mpc/` 下的实现。
- 转译matlab代码时，不要添加新的内容，如果当前函数调用的函数代码结构里没有，直接使用占位符，我会后续补上
- 等你逐段发 MATLAB 源码后，再按模块迁移。 除了 Python 语言适配、项目路径/API/UI 接入这些必要改动，算法逻辑尽量 1:1 还原。 保留 MATLAB 的业务顺序、变量语义、几何计算方式和边界条件。 如果发现 Python 化时必须改变实现方式，必须先说明原因，不擅自扩展功能或重构逻辑。

## 测试提示

- 单元测试位于 `tests/`。
- `tests/test_growth_rate_commands.py` 覆盖 central 生长速率命令、gsensor 命令处理、UI 参数写入和 reset 行为。
- `tests/test_central_startup_publish.py` 依赖 RabbitMQ；如果 `pika` 未安装或 RabbitMQ 不可用，测试会 skip。
- `scripts/run_*_influx_test.py` 中有多份基于正弦波的 InfluxDB 写入测试脚本，用于生成温度、浓度、sigma、目标值、滞后量等测试遥测；这些脚本通常使用 `source=test_sine` 标签，并要求 InfluxDB 配置可用。
- 文档-only 改动通常不需要启动 Docker 或运行完整测试。若改动消息、参数、RabbitMQ 或 UI API 行为，至少运行相关 pytest；涉及真实消息流时启动 RabbitMQ 后再验证集成测试。

## 当前协作边界

生成或更新本文档时，不应修改 Python API、消息 schema、配置 schema、Docker 服务或 UI 文件。若用户只要求项目代理指南，保持变更范围为根目录 `agent.md`。
