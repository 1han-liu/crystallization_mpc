# Telegraf—RabbitMQ 过程读写接入

Controller 只通过 RabbitMQ 接收过程数据和发出设定值。Telegraf 负责读取五个 OPC UA 节点；独立 `opcua-writer` 负责将 `T_j_set` 写回设备并发布结果。Controller 不再直连 OPC UA，也不会在通信失败时回退到直连。

```text
OPC UA ── Telegraf 1.40.1 ── telegraf.to.process ── Controller
OPC UA ←─ opcua-writer   ←─ controller.to.opcua_writer ←─ Controller
               └────────── opcua_writer.to.process ────→ Controller
```

三个专用队列均使用 `idp.bus` topic exchange。过程数据、写入结果的接收由 Controller 适配器自己的线程和 Pika 连接完成；普通 Controller 消费者处理参数、实验命令、GSensor 消息。等待首份数据、等待设备回执不会依赖持有业务锁的普通消息线程。

## 组员模拟器需要提供什么

默认是**一个 endpoint 下的五个节点**。下表中的字符串是 NodeId 的 identifier，不是五个 endpoint。完整 NodeId 为 `ns=2;s=<identifier>`；如实验室配置不同，通过环境变量修改。

可直接交给模拟器开发者的完整约定见 [OPC UA 模拟器五节点接口说明](opcua-simulator-interface.md)，包含 endpoint、初值、单位、写权限和验收条件。

| 字段 | 默认 identifier | OPC UA 值的单位 | 最小测试类型 | 权限 |
|---|---|---|---|---|
| `T` | `Local.iControl.Experiment1.Trends.Tr.Value` | ℃ | Double | 读 |
| `T_j` | `Local.iControl.Experiment1.Trends.Tj.Value` | ℃ | Double | 读 |
| `c` | `Local.iControl.Experiment1.Trends.load.Value` | g/g H₂O，溶解溶质/水质量比 | Double | 读 |
| `count_middle` | `Local.iControl.Experiment1.Trends.count.Value` | 原始计数标量，不缩放；真实统计口径待确认 | Double | 读 |
| `T_j_set` | `iCache.Temperature1` | ℃ | Double | 读、写 |

组员先提供好质量、有限数值即可，不需要实现结晶动力学。模拟器需保留写入后的设定值，以便读回验证。生产节点类型通过 OPC UA 查询后按原类型写入；首次联调需核实实际类型能够表示小数温度。`c` 和 `count_middle` 必须非负，温度转换到 Kelvin 后必须为正。

组员模拟器可先提供固定初值 `25 ℃、23 ℃、0.32、17、22 ℃`。在尚未写回设定值时，Controller 应看到 `298.15 K、296.15 K、0.32、17、295.15 K`。正式 Controller 使用配置的算法计算设定值，输出不固定为某个测试温度；写回时应以该周期实际输出为准。

## 数据与命令契约

读取路由 `telegraf.to.process` 的消息直接采用 Telegraf 批量 JSON，不套业务 envelope：

```json
{"metrics":[
  {"name":"crystallizer_raw","tags":{"device":"crystallizer","id":"ns=2;s=Local.iControl.Experiment1.Trends.Tr.Value"},"fields":{"T":25.0,"Quality":"The operation succeeded. StatusGood (0x0)"},"timestamp":1791964800123456789}
]}
```

示例只展示一个节点，`Quality` 文本取自固定版本容器的实际输出；交付一个 `ProcessState` 必须集齐同设备、同纳秒时间戳的五个节点。完整轮次可以跨多条消息到达，一条消息也可包含多个轮次。时间戳应由实际采集生成，不能复制示例时间。不要把纳秒整数通过 JavaScript `Number` 中转，否则可能丢失精度。

采集时间使用 `time_source="collection_start"`，是同一次 Telegraf 采集的标签，不代表设备上的原子快照。错误质量、错误节点映射、非有限数值或缺字段不会形成新状态。默认五秒后旧状态过期；启动默认最多等待十秒。重连后必须取得连接建立之后的新完整轮次。检查点仅保留历史诊断，不恢复为新测量。

命令和回执使用现有 envelope，`ver=1`，`correlation_id=command_id`：

| 项目 | 命令 | 结果 |
|---|---|---|
| 路由 | `controller.to.opcua_writer` | `opcua_writer.to.process` |
| `src` / `dst` | `controller` / `opcua_writer` | `opcua_writer` / `process` |
| `msg_type` | `command` | `event` |
| `name` | `process.setpoint.write` | `process.setpoint.result` |
| payload | `command_id, run_id, run_started_at, tick_seq, setpoint_K, expires_at` | 相同命令及实验/周期标识，`status, error, write_result` |

时间字段采用带时区的 ISO 8601。`tick_seq` 是 Controller 周期序号。`status` 为 `success`、`failed` 或 `unknown`。成功的 `write_result` 包含 `setpoint_K, setpoint_C, written_at, verified, readback_K`。Controller 只有收到身份匹配、读回验证通过的结果才记录成功；发布到 RabbitMQ 不代表设备已写入。

默认命令有效期与等待回执时间均为五秒。超时为结果未知并进入 Controller 现有错误状态，不生成新命令自动补发。`opcua-writer` 的 SQLite 日志在写前记录 `executing`，写后记录结果；重复命令返回持久化结果。重启遇到未完成记录时返回 `unknown`，禁止猜测并重新写设备。这并不承诺物理动作“恰好一次”。消息消费 ACK 必须晚于结果发布确认。

## 配置迁移与生产启动

已有 `.env` 不会被自动修改。参考 `.env.example` 手工迁移；旧 `CONTROLLER_OPCUA_*` 不再启用设备直连。

| 旧配置 | 新配置 |
|---|---|
| `CONTROLLER_OPCUA_ENABLED` | `CONTROLLER_PROCESS_ENABLED` |
| `CONTROLLER_OPCUA_WRITE_ENABLED` | `CONTROLLER_PROCESS_WRITE_ENABLED` |
| `CONTROLLER_OPCUA_ENDPOINT` | `OPCUA_ENDPOINT`，仅提供给 Telegraf/writer |
| `CONTROLLER_OPCUA_NAMESPACE_INDEX` | `OPCUA_NAMESPACE_INDEX` |
| `CONTROLLER_OPCUA_T_NODE` | `OPCUA_T_NODE` |
| `CONTROLLER_OPCUA_T_J_NODE` | `OPCUA_T_J_NODE` |
| `CONTROLLER_OPCUA_C_NODE` | `OPCUA_C_NODE` |
| `CONTROLLER_OPCUA_COUNT_MIDDLE_NODE` | `OPCUA_COUNT_MIDDLE_NODE` |
| `CONTROLLER_OPCUA_T_J_SET_NODE` | `OPCUA_T_J_SET_NODE` |
| `CONTROLLER_OPCUA_TIMEOUT_S` | `OPCUA_TIMEOUT_S` |
| `CONTROLLER_OPCUA_JACKET_MIN_K/MAX_K` | `OPCUA_JACKET_MIN_K/MAX_K` |
| `CONTROLLER_OPCUA_VERIFY_WRITE` | `OPCUA_VERIFY_WRITE`，写入服务必须为 true，false 拒绝启动 |
| `CONTROLLER_OPCUA_VERIFY_TOLERANCE_K` | `OPCUA_VERIFY_TOLERANCE_K`，默认 0.05 |

Compose 将节点映射复制为 Controller 的 `PROCESS_NAMESPACE_INDEX` 和 `PROCESS_*_NODE`，用于校验消息中的 NodeId；同时将读回容差映射为 `PROCESS_VERIFY_TOLERANCE_K`。Controller 不接收 endpoint、密码或设备证书。直接运行 Python 时需自行提供对应的 `PROCESS_*` 节点映射和容差。`PROCESS_DEVICE_ID` 必须与 Telegraf `tags.device` 相同。一个 Controller 实例应拥有自己的过程数据/结果队列，不能与另一个实验 Controller 竞争消费同一队列。

新增通信参数为 `PROCESS_SAMPLE_MAX_AGE_S=5`、`PROCESS_STARTUP_TIMEOUT_S=10`、`PROCESS_COMMAND_TTL_S=5`、`PROCESS_WRITE_TIMEOUT_S=5`；Telegraf 的 `TELEGRAF_INTERVAL/TELEGRAF_FLUSH_INTERVAL` 默认 `1s`。五秒和一秒是通信测试默认值，现场再核实允许的控制周期、网络时延、服务器负载和时钟同步。

设备认证配置由两项服务共同使用：`OPCUA_AUTH_METHOD`（Anonymous/UserName/Certificate）、`OPCUA_USERNAME/PASSWORD`、`OPCUA_SECURITY_POLICY/SECURITY_MODE`、`OPCUA_CERTIFICATE/PRIVATE_KEY/SERVER_CERTIFICATE`。证书目录由 `OPCUA_CERT_HOST_ROOT` 挂载到 `/run/opcua-certs`；文件路径填容器内路径。空证书加 None/Anonymous 是测试服务器默认配置，真实认证仍需现场配置。不要提交私钥或实验室密码。

正式切换先结束活动实验，再修改通信配置并启动新服务：

```powershell
docker compose --profile process-io up -d --build
docker compose logs --tail 100 telegraf opcua-writer controller
```

只有明确配置 `CONTROLLER_PROCESS_ENABLED=true` 才接收设备数据；要写回还需 `CONTROLLER_PROCESS_WRITE_ENABLED=true`。数值模拟保留两项为 false。writer 执行记录保存在 `opcua_writer_data` volume；普通重启保留它。不要把删除执行记录当成重试命令的方法，也不要自动重放历史实验命令。

访问 Controller `/api/status` 中的 `integrations.process`，检查连接状态、最新完整状态、年龄、缺失字段、最后命令、结果和错误。`integrations.opcua` 已迁移；首版没有新增操作页面或 Grafana 看板。

## 对接组员模拟器并验收

以下步骤使用交付的 Compose 服务和组员提供的 OPC UA 模拟器。模拟器由组员单独启动，Compose 不创建模拟器。先按 [五节点接口说明](opcua-simulator-interface.md) 核实 endpoint、初值、类型、权限和认证，再由集成方配置 `.env`。同机 Windows 宿主模拟器的地址通常为 `opc.tcp://host.docker.internal:4840`；其他部署位置应使用实际可达地址。

### 先验证读取

结束已有实验后，在 `.env` 中启用过程读取、关闭写回，并配置正式算法适配器：

```dotenv
CONTROLLER_PROCESS_ENABLED=true
CONTROLLER_PROCESS_WRITE_ENABLED=false
CONTROLLER_ADAPTER=crystallization_mpc.apps.controller.algorithm.integration:CrystallizationControllerAdapter
```

从仓库根目录启动服务并检查日志：

```powershell
docker compose --profile process-io up -d --build
docker compose --profile process-io ps
docker compose logs --tail 100 telegraf opcua-writer controller
```

在 Central 页面 `http://localhost:8000` 配置并启动新的 `experiment` 实验，使用已发布的有效参数及其所需的生长率/图像输入。服务启动本身不会自动创建实验；过程适配器在实验启动时连接并等待完整数据。生产算法仍需满足其正常输入条件，五个假值只替代设备过程测量。

在 PowerShell 读取状态：

```powershell
$controllerStatus = Invoke-RestMethod 'http://localhost:8002/api/status'
$controllerStatus.integrations.process | ConvertTo-Json -Depth 8
$controllerStatus.last_control_output | ConvertTo-Json -Depth 8
```

在实验运行期间，检查 `integrations.process` 的 `connected=true`、`sample_valid=true`、`missing_fields=[]`，且 `sample_age_s` 在配置有效期内。`last_state` 应包含五个换算后的初值；`last_control_output.process_state` 则显示算法实际使用的测量。模拟器把 `T` 从 `25.0` 改为 `26.0 ℃` 后，后续采集及控制周期应看到 `299.15 K`。此阶段 `write_enabled=false`，Controller 不应产生过程写入。

### 再验证写回和回执

先结束只读实验，将 `.env` 中的 `CONTROLLER_PROCESS_WRITE_ENABLED` 改为 `true`，再运行同一条 `docker compose --profile process-io up -d --build` 命令应用配置，然后在 Central 启动新的实验。

等待算法生成有效设定值，重新读取上述状态并检查一个已完成的控制周期：

| 检查位置 | 预期结果 |
|---|---|
| `last_control_output.result.T_j_set` | 本周期算法实际输出 `S_K`，单位 K |
| `last_control_output.process_write` | `setpoint_K=S_K`，`setpoint_C=S_K-273.15`，`verified=true`，`readback_K` 与 `S_K` 之差不超过配置容差 |
| `integrations.process.last_command` / `last_write_result` | 同一请求的 `command_id`、`run_id`、`run_started_at`、`tick_seq` 匹配，结果 `status=success`；新请求仍在途时等待其完成再核对 |
| 模拟器节点或独立 OPC UA 客户端 | 最近一次成功写入对应的 `T_j_set` 为 `S_K-273.15 ℃` |

正式 MPC 输出取决于参数和测量，不保证为 `310.15 K`。例如某周期实际输出恰为 `310.15 K` 时，节点应为 `37.0 ℃`。组员还可在 Controller 实验停止时独立向模拟器写 `37.0 ℃` 并立即读回，以自检节点写权限；该自检仅验证模拟器，不代表整条 RabbitMQ 链路通过。若算法未生成有效设定值，应先查看 `last_control_output.result` 及 Controller 日志，不能把“尚未发出命令”判为 writer 写入失败。

完成观察后在 Central 停止实验，确认模拟器保持最后写入的设定值。停止实验会断开 Controller 的过程连接，因此停止后的 `connected=false` 或 `sample_valid=false` 不代表此前的读写周期失败；已完成周期的输入和写回结果保留在 `last_control_output` 中。

联调时记录 endpoint、节点映射、服务版本、实验 `run_id`、测量变化、匹配命令/回执和独立读回值。组员模拟器和真实硬件应分别验收，不能把模拟器上的结果当作现场设备验收。故障恢复检查应确认过期数据不再用于新周期，恢复后新实验必须取得新完整测量；不要复制历史消息或重放历史控制命令来模拟恢复。

## Telegraf 固定版本运行约束

**Telegraf 采集轮次验收必须常驻运行。** 本配置固定 1.40.1；`--test`/`--once` 绕过采集开始时间初始化，不可作为轮次验收方式。Compose 启动正常 Telegraf 进程，通过 Controller 状态和 AMQP 输出检查完整采集轮次。

**1.40.1 配置兼容问题：** 实际容器验证发现，这个版本的 `buildInput` 支持 `time_source`，但未使用字段检查的白名单漏掉了它，直接放在 `[[inputs.opcua]]` 中会启动失败。本项目保持官方 `telegraf:1.40.1` 镜像，采用该版本支持的旧式 `[opcua]` 输入表，仍创建同一个 `inputs.opcua` 插件并使用相同运行逻辑。通过两个显式 `--config` 参数，先加载 `telegraf.conf`（agent/output），最后加载 `opcua.conf`（单个输入）。**顺序不能颠倒，不能再追加配置文件或 `--config-directory`**，否则后续加载会再次触发该版本错误的未使用字段检查。升级 Telegraf 时应重新验收并考虑恢复现代配置形式；不要删掉 `time_source` 或改为按到达时间拼接五个节点。

## 实现依据

- [固定版本 OPC UA 输入配置](https://github.com/influxdata/telegraf/blob/v1.40.1/plugins/inputs/opcua/sample.conf)：节点、认证与时间来源选项。
- [固定版本采集时间处理](https://github.com/influxdata/telegraf/blob/v1.40.1/models/running_input.go)：同一轮的 `collection_start` 覆盖规则。
- [固定版本配置解析](https://github.com/influxdata/telegraf/blob/v1.40.1/config/config.go)：`buildInput`、`missingTomlField` 与旧式输入表解析路径；用于保留官方镜像的配置兼容处理。
- [固定版本测试路径](https://github.com/influxdata/telegraf/blob/v1.40.1/agent/agent.go)：测试模式与正常采集路径的区别。
- [AMQP 输出插件](https://docs.influxdata.com/telegraf/v1/output-plugins/amqp/)：exchange、routing key 和批量序列化配置。
