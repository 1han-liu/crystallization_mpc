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

采集时间使用 `time_source="collection_start"`，是同一次 Telegraf 采集的标签，不代表设备上的原子快照。错误质量、错误节点映射、非有限数值或缺字段不会形成新状态。默认五秒后旧状态过期；启动后的首份有效采样默认以十秒为超时判定。这些采样时间用于判断数据是否超时，不再作为自动结束实验的等待上限；RabbitMQ 连接本身的失败仍按故障处理。重连后必须取得连接建立之后的新完整轮次。检查点仅保留历史诊断，不恢复为新测量。

当同一轮五项数据齐全、质量良好、时间新鲜，且除 `c < 0` 外均通过接口校验时，Controller 将负浓度作为可恢复的测量等待：保持当前实验与采集连接，暂停算法计算和设备写入，不推进算法周期或控制时间，也不把负浓度替换为零或继续使用旧浓度。启动时遇到这种情况也保留同一实验，等待有效初值。收到新的、五项全部有效且 `c >= 0` 的采样后，同一实验自动初始化或继续控制，无须 End 后重建实验。恢复后的首个有效周期用于更新测量基线，设备写入继续暂停；后续有效周期才可按原有写入开关发送命令。此行为不改变等待期间设备本身最后一次设定值，现场控制软件继续运行。

当 Controller 与 RabbitMQ 保持连接，但采集数据流静默、旧采样过期，或启动等待结束后仍只有部分字段时，保留当前实验并无限期等待，`wait_reason="process_data_timeout"`。这种情况可由 OPC UA 采集中断或通信延迟引起；Controller 保持消费连接，暂停算法计算与新的设备写入，不推进算法周期或控制时间。负浓度等待期间若采集数据流随后静默，也转为这种超时等待。恢复条件是进入等待之后的新采集轮次：同一设备、同一轮次的五项数据齐全、质量 Good、有限数值、符合接口范围且浓度非负，采集时间在 `PROCESS_SAMPLE_MAX_AGE_S` 内；旧缓存、部分字段或只有 TCP 连接成功均不能恢复控制。满足条件后仍使用同一 run，首个恢复周期更新测量基线且不写设备，后续周期按原有写入开关工作。等待没有新的时长上限，操作员仍可随时 End Experiment。

等待期间，`integrations.process.waiting_for_data=true`，并提供 `wait_reason`、`waiting_since` 和 `last_invalid_state`。负浓度的原因是 `negative_concentration`，`last_invalid_state.raw_c` 保留原始负值；网页显示 **Waiting for valid process data**。通信等待的原因是 `process_data_timeout`，网页显示 **Waiting for fresh process data. Communication timed out.**；它没有浓度数值时不补零。提示均说明算法与设备写入暂停。Telegraf 能采集时继续保留原始 `crystallizer_raw`；没有新算法计算时不会生成新的 `controller_measurement` 曲线点。

等待不放宽安全校验。新收到的数据若明确出现坏质量、错误 NodeId、非有限值、负计数、绝对温度无效或消息格式错误，仍按故障处理。Controller 与 RabbitMQ 的连接失败、停止或中断，以及已发送写命令但回执未知、算法异常和持久化失败也仍是故障。真正故障停止后的实验需要 End Experiment，不会自动恢复；容器重启后也不会因保存了等待状态而自动启动设备控制。

启用实时 GSensor 后，Controller 也会等待新鲜有效的 G 测量。默认 `dt_G=15s`，G 新鲜度上限为 `2 × dt_G=30s`；这只是判断测量是否陈旧的阈值，不再是终止实验的时限。首次测量尚未准备好或既有 G 超过新鲜度上限时，同一实验保持 `running`，暂停控制计算、适应和设备写入，持续等待，不补造 G 或沿用过期值。该等待同样适用于 σ 目标；明确关闭 GSensor 时仍走原有的非相机输入逻辑。

G 等待状态见 Controller `growth_input.growth_wait`：原因是 `growth_data_timeout` 或 `waiting_for_fresh_valid_sample`，并保留 `waiting_since`，必要时保留 `last_valid_at` 和检查边界。网页中性显示 **Waiting for fresh valid G data**。新测量必须满足原有 run、版本、激活代次、初始化代次、对齐代次、有效性和新鲜度检查，其他必需过程输入也必须有效，才会继续同一实验。恢复首周期用于更新测量基线且不写设备，后续周期才按原写入开关工作。等待时 GSensor 可继续处理和发布图像结果；没有新的控制计算时，Grafana 的 Controller 曲线不会新增点。其他真实故障以及 Controller 重启后的人工确认要求不变。

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

If setpoint feedback and the writable target have different NodeIds, set `OPCUA_T_J_SET_READ_NODE`. Telegraf reads `T_j_set` from this override and Controller validates the same NodeId; empty or unset preserves `OPCUA_T_J_SET_NODE`. The writer always uses `OPCUA_T_J_SET_NODE` for both writes and immediate readback verification. A lab read-only trend example is `Local.iControl.Experiment1.Trends.Tjset.Value`; verify its physical meaning and Celsius units before accepting it as jacket setpoint feedback. Read access to this trend does not establish a writable device target.

Compose 将节点映射复制为 Controller 的 `PROCESS_NAMESPACE_INDEX` 和 `PROCESS_*_NODE`，用于校验消息中的 NodeId；同时将读回容差映射为 `PROCESS_VERIFY_TOLERANCE_K`。Controller 不接收 endpoint、密码或设备证书。直接运行 Python 时需自行提供对应的 `PROCESS_*` 节点映射和容差。`PROCESS_DEVICE_ID` 必须与 Telegraf `tags.device` 相同。一个 Controller 实例应拥有自己的过程数据/结果队列，不能与另一个实验 Controller 竞争消费同一队列。

新增通信参数为 `PROCESS_SAMPLE_MAX_AGE_S=5`、`PROCESS_STARTUP_TIMEOUT_S=10`、`PROCESS_COMMAND_TTL_S=5`、`PROCESS_WRITE_TIMEOUT_S=5`；Telegraf 的 `TELEGRAF_INTERVAL/TELEGRAF_FLUSH_INTERVAL` 默认 `1s`。五秒和一秒是通信测试默认值，现场再核实允许的控制周期、网络时延、服务器负载和时钟同步。

设备认证配置由两项服务共同使用：`OPCUA_AUTH_METHOD`（Anonymous/UserName/Certificate）、`OPCUA_USERNAME/PASSWORD`、`OPCUA_SECURITY_POLICY/SECURITY_MODE`、`OPCUA_CERTIFICATE/PRIVATE_KEY/SERVER_CERTIFICATE`。证书目录由 `OPCUA_CERT_HOST_ROOT` 挂载到 `/run/opcua-certs`；文件路径填容器内路径。空证书加 None/Anonymous 是测试服务器默认配置，真实认证仍需现场配置。不要提交私钥或实验室密码。

writer 使用固定版本 `asyncua==2.0.1`。可选的 `OPCUA_SERVER_URI` 仅传给 writer，填写 Server 在 `GetEndpoints` 中公布的 `Server.ApplicationUri`；不填写时为 `None`。它与 `OPCUA_ENDPOINT` 的网络地址不同：Docker 使用 `host.docker.internal` 访问宿主机时，也不能据此拼接或改写 Server URI。某些 Server 会严格核对 `CreateSession.ServerUri`；遇到 `BadServerUriInvalid` 时，核实并配置实际 ApplicationUri 后重新构建、重建 writer。同步客户端通过 `client.aio_obj.server_uri` 设置该字段，直接给 `client.server_uri` 赋值不会修改实际请求。用户名、证书和安全策略仍按上面的配置处理。

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

## Experiment storage and CSV exports

### 归档规则与数据范围

新实验目录采用 `UTC创建时间_实验名称`，例如 `20261014T190821_051Z_Cooling test`。Windows 不允许的字符会替换，空名称使用 `Unnamed`，重复目录追加序号。内部唯一标识仍为原来的 `exp_<UTC时间>_<随机标识>`，记录在 `experiment.json` 的 `run_id` 中；同名实验也有不同 ID。已有以 ID 命名的目录保持原样，查找时读取 manifest，不依赖目录等于 ID。

```text
EXPERIMENT_HOST_ROOT/
  _system/
    .central_experiment_state.json
    .central_run_configuration.json
    .central_runtime_history.json
    .central_runtime_requests.json
    .controller_runtime_state.json
    .controller_runtime_history.json
    .gsensor_experiment_state.json
    params_runtime.yaml
    *_telemetry.sqlite3          # 持久待发送队列及本地审计记录
    migration_backup/           # 迁移旧状态时的备份
  20261014T190821_051Z_Cooling test/
    experiment.json
    params_snapshot.yaml
    capture.json                # 本次设备、节点、单位边界及运行配置
    images/                     # 相机输入及原有图像处理产物按既有规则保存
    gsensor_processing_state.json
    controller_finalized.json
    gsensor_finalized.json
    archive.json                # 导出状态、数据覆盖检查、行数和错误
    parameters.csv
    process_data.csv
    controller_data.csv
    gsensor_data.csv
    events.csv
```

SQLite 可能同时出现 `-wal`、`-shm` 文件，这是数据库运行文件，不是额外实验。启动生成 `_system` 中的检查点、锁和队列属于正常行为。不要在服务运行时手动删除这些文件。writer 的命令执行数据库仍单独保存在 `opcua_writer_data` 卷，Telegraf 的磁盘缓冲保存在 `telegraf_data` 卷。

Telegraf 在服务运行时持续采集，不依赖实验是否开始：同一轮数据分别发送 AMQP 和直接写入 InfluxDB 的 `crystallizer_raw`。原始数据按 `device + NodeId + 纳秒采集时间` 保存，保留设备单位（温度 ℃）。Controller 数据保存实际使用的测量和控制结果，温度为 K。单次实验导出按固定设备及 `[开始时间, 最后生产者结束时间)` 筛选；后台数据不强行写入某个实验 ID，CSV 中再附本次 `run_id`。

OPC UA 输入是定时读取当前节点值。如果设备每五秒更新一次，Telegraf 每秒读取会产生多条数值相同、采集时间不同的记录；不会强迫设备产生新测量，也不会丢弃这些有效重复值。`collection_start` 只代表读取轮次，不证明设备每秒更新、设备端原子快照或测量未冻结。设备更新快于采集时，采集间隔内的变化可能读不到。该版本配置不提供完整的设备源更新时间历史，不能用采集时间检测所有设备冻结问题。

AMQP 与 InfluxDB 是独立输出：数据入库不表示 Controller 已使用它，控制算法使用记录以 `controller_measurement` 为准。断网后积压原始数据可补入数据库，但 Controller 仍拒绝超过有效期的积压测量。未实际采集的停机时间、Telegraf 已过滤的坏质量值无法补造；CSV 和 `archive.json` 会显示可检测的缺字段、间隔和质量问题。

### 五类 CSV 与结束行为

| 文件 | 内容 |
|---|---|
| `parameters.csv` | 启动参数快照、运行模式、运行期间参数变更及生效状态；未生效请求不会伪装成已生效参数 |
| `process_data.csv` | 实验时间范围内每轮五节点原始读值、NodeId、质量、完整性；温度 ℃ |
| `controller_data.csv` | 各控制周期实际测量、算法输出、设定值、写回结果和周期序号；算法温度 K |
| `gsensor_data.csv` | 实际已处理图像的测量、帧序号及有效性；未启用时只有表头 |
| `events.csv` | 实验生命周期、运行配置事件、写命令准备/发布/执行/回执/未知结果 |

结束实验后，Central 等待 Controller 和 GSensor 的结束标记、本地待发送队列清空以及原始数据流越过结束边界，再导出。数据库断线时先显示 `Waiting for data`；恢复后继续。停止通知丢失但两端有效结束标记均已落盘时，可从记录恢复结束状态。Telegraf 停止后没有越过边界的新数据时，保持等待；恢复采集后再检查，不会将旧数据冒充完整结果。

导出通过临时文件替换写入，五个文件完成后才开放下载。`Completed` 表示本次检查覆盖完整；`Incomplete` 表示文件可用但发现缺测、缺审计或旧实验信息不足；`Failed` 表示导出失败。`Exporting` 表示正在生成文件。UI 全部采用英文，用户输入的实验名称保持原文。结果未知的设备写入仍按未知保存，不会为了完成报表而重发控制命令。

`EXPERIMENT_EXPORT_ENABLED=true` 默认开启结束后自动导出；关闭后仍可手动导出。旧实验不会自动批量重导，历史缺失记录无法追溯生成。数据库、持久队列和 CSV 均未设置自动删除策略；InfluxDB 实际保留时长取决于 bucket retention。容量规划应同时包括这些记录、设备采集和图像。Telegraf 1.40.1 的磁盘缓冲为实验性功能；当前关闭逐点 fsync 以满足一秒周期，已验证进程崩溃后的补传，**不保证宿主机断电时最近缓冲零丢失**。若在实验室启用 fsync，应重新验收采集延迟，不能直接放宽控制数据有效期。

CSV 的 `time_ns` 为完整纳秒整数，`time_utc` 保留九位小数。Excel 自动识别数字可能舍入 `time_ns`，导入该列时选择文本。当前部署运行一个 Central 进程；不要为同一实验存储启动多个 Central workers，归档锁目前为进程内锁。

### 从哪里查询和下载

Central `http://localhost:8000` 展开 **Experiment History & Exports**，按实验名称、完整/部分 ID、UTC 创建日期搜索。选择历史行仅查看该次实验，不会切换活动实验。可查看运行配置、目录、导出状态和缺失原因，下载五个 CSV，或点击 **Re-export CSV**。所有 CSV 也直接保存在对应实验目录中。

接口示例（替换为真实 ID）：

```powershell
$runId = 'exp_20261014T190821_051Z_2a986e'
Invoke-RestMethod "http://localhost:8000/api/experiment-history?q=$runId"
Invoke-RestMethod "http://localhost:8000/api/experiments/$runId/archive"
Invoke-RestMethod -Method Post "http://localhost:8000/api/experiments/$runId/export"
Invoke-WebRequest "http://localhost:8000/api/experiments/$runId/exports/process_data.csv" -OutFile process_data.csv
```

**Grafana** 链接使用现有分析看板和 `var-run_id`、实验时间范围；地址通过 `GRAFANA_EXPERIMENT_URL` 配置。Controller/GSensor/事件数据可按 `run_id` 查询；连续原始数据没有实验 ID tag，在 InfluxDB 中使用 `device` 和时间范围查询，Central 导出会自动处理这种关联。本次没有新增 Grafana 原始数据面板。

### 已有机器首次升级

先在 Central 结束活动实验并确认结束。已有 `.env` 和数据库不会自动覆盖；保留 `EXPERIMENT_HOST_ROOT` 指向原实验根目录。迁移程序只整理已知系统文件，不重命名历史实验目录，不重放控制命令。

若旧 RabbitMQ 使用自动生成的数据卷，**在重建服务之前**记录旧 volume 和 hostname：

```powershell
$rabbitContainer = docker compose ps -q rabbitmq
docker inspect $rabbitContainer --format '{{.Config.Hostname}}'
docker inspect $rabbitContainer --format '{{range .Mounts}}{{if eq .Destination "/var/lib/rabbitmq"}}{{.Name}}{{end}}{{end}}'
```

将这两个结果分别填入 `.env` 的 `RABBITMQ_HOSTNAME` 和 `RABBITMQ_DATA_VOLUME`。新安装无需填写，默认数据卷按 Compose 项目隔离。保留旧 RabbitMQ 身份和旧数据卷，避免重建后得到空 broker。

在 PowerShell 从仓库根目录执行，`$experimentRoot` 必须替换为 `.env` 中实际的 `EXPERIMENT_HOST_ROOT`：

```powershell
$experimentRoot = 'D:\lab\experiments'
docker compose --profile process-io stop
.venv\Scripts\python -m crystallization_mpc.infra.storage_paths --root $experimentRoot
.venv\Scripts\python -m crystallization_mpc.infra.storage_paths --root $experimentRoot --apply
docker compose --profile process-io up -d --build
docker compose --profile process-io ps
docker compose logs --tail 100 central controller gsensor telegraf opcua-writer
```

前一条 Python 命令只预览；后一条执行并备份。先确保该虚拟环境已按 README 安装当前项目（`pip install -e .`）。如果目标文件与旧文件冲突，迁移拒绝覆盖，应查看报告中的具体文件；运行中的实验也会阻止迁移。发现旧默认系统文件时，服务会明确要求迁移，不会悄悄创建空状态绕过历史。

Controller 可读取旧版 schema 1 中 `idle`、`configured`、`stopped` 的检查点，保留参数、实验 ID 和运行配置历史，并按当前规则校验；首次读取不改写原检查点。旧版运行中或错误状态不能自动恢复，兼容读取也不会重新连接设备或继续旧控制周期。

确认 `config/influxdb.env` 中 token/org/bucket 与原数据库一致且有读写权限；Telegraf 与各服务通过 Compose 使用同一份配置。新默认参数文件为容器内 `/data/experiments/_system/params_runtime.yaml`。显式自定义参数/状态路径仍按用户配置使用。禁止使用 `docker compose down -v` 作为升级步骤。

升级后启动短实验，检查 Controller `integrations.process` 的有效读值、成功写回，以及 `integrations.influxdb.archive.pending_count` 最终归零。结束后在历史页面确认 `Completed`，核对 CSV 的 run_id、温度单位、最后周期和 `archive.json` 的覆盖检查。组员模拟器与真实硬件仍分别验收。

## 实现依据

- [固定版本 OPC UA 输入配置](https://github.com/influxdata/telegraf/blob/v1.40.1/plugins/inputs/opcua/sample.conf)：节点、认证与时间来源选项。
- [固定版本采集时间处理](https://github.com/influxdata/telegraf/blob/v1.40.1/models/running_input.go)：同一轮的 `collection_start` 覆盖规则。
- [固定版本配置解析](https://github.com/influxdata/telegraf/blob/v1.40.1/config/config.go)：`buildInput`、`missingTomlField` 与旧式输入表解析路径；用于保留官方镜像的配置兼容处理。
- [固定版本测试路径](https://github.com/influxdata/telegraf/blob/v1.40.1/agent/agent.go)：测试模式与正常采集路径的区别。
- [AMQP 输出插件](https://docs.influxdata.com/telegraf/v1/output-plugins/amqp/)：exchange、routing key 和批量序列化配置。
