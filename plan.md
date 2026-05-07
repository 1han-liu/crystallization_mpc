# 计划记录

本文档用于保存后续待执行或已确认的计划内容。每次进入实施前，可以把最终确认的计划记录到这里；实施完成后，再把结果同步到 `memory.md`。

## 当前计划

### 2026-05-07 - Gsensor 功能补齐约束

- 目标：将 MATLAB 版 gsensor 图像测量服务迁移到 Python 项目骨架中。
- 范围：后续实现应沿用 `src/crystallization_mpc/apps/gsensor/` 下的 `detection/imgs/model/morphs/utils` 结构；通讯统一使用 RabbitMQ，不引入 OPC UA。
- 实施步骤：按 MATLAB 函数依赖逐步迁移主循环、图像读取/标定、YOLO 分割、Hough 边界检测、生长速率计算、EKF 滤波和 RabbitMQ 发布。
- 测试计划：先为纯计算和状态转换函数补单元测试；再用样例图像或合成检测结果验证边界位移、生长速率和 EKF 输出；最后验证 gsensor -> controller RabbitMQ 消息。
- 假设/风险：参数和状态结构以 `params_default.yaml` 中 `shared` 与 `gsensor` 参数为基础，并叠加 central 下发或 Gsensor UI 写入的运行时参数；具体消息 payload 需在看到 MATLAB 输出字段后锁定。
- 状态：待执行，等待 MATLAB 源码。

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
