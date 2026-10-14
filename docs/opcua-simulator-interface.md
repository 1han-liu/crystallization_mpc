# OPC UA 模拟器五节点接口说明

请按以下接口搭建 OPC UA Server，供 Telegraf 读取过程值，并允许独立写入服务修改 `T_j_set`。第一轮目标是验证通信读写闭环；五个节点可以先保持固定假值，无需先实现结晶动力学。

模拟器只需实现 OPC UA 接口。Telegraf 负责读取并转成 RabbitMQ JSON，Controller 负责计算，`opcua-writer` 负责写入和发送结果回执。

## 1 连接地址和认证

提供**一个 endpoint，下挂五个节点**。endpoint 是服务器地址，NodeId 是服务器内变量的标识，两者不要混淆。

| 项目 | 本次模拟联调约定 |
|---|---|
| 协议 | OPC UA，`opc.tcp://` |
| TCP 端口 | 默认 `4840`；若更改，请交付实际端口 |
| 服务监听 | `opc.tcp://0.0.0.0:4840`，允许来自集成机器的连接 |
| 客户端实际连接地址 | `opc.tcp://<模拟器机器可达IP>:4840` |
| 认证 | `Anonymous` |
| SecurityPolicy / MessageSecurityMode | `None` / `None`，用于本次测试环境 |
| NamespaceIndex | 固定为 `2`，重启后保持一致 |
| Namespace URI | 可使用 `urn:crystallization:process-io-test`，保持稳定 |
| 并行客户端 | 至少同时支持 Telegraf 和 writer 两个客户端；联调工具还会另开读写连接 |

`0.0.0.0` 仅用于服务监听，不能作为交付给客户端的连接地址。运行模拟器的机器需允许测试所用 TCP 端口入站；容器部署还需发布端口或接入同一 Docker 网络。若 endpoint 包含额外路径，交付时必须提供完整路径。

| 模拟器所在位置 | Telegraf / writer 容器使用的 endpoint |
|---|---|
| 与本项目 Docker Desktop 同一台 Windows 主机，模拟器运行在宿主机 | `opc.tcp://host.docker.internal:4840` |
| 另一台实验室机器 | `opc.tcp://<该机器局域网IP>:4840` |
| 同一个 Docker 网络中的模拟器服务 | `opc.tcp://<模拟器服务名>:4840` |

容器中的 `localhost` 指向容器自身，不能用它代指组员的电脑。仓库原生产配置示例端口为 `62552`；接本模拟器时应明确把 `OPCUA_ENDPOINT` 改为上表对应地址。

## 2 五个节点

五个节点均为 OPC UA **Variable**，其 Value 类型统一为 **Double（64 位浮点数，标量）**。不要返回数字字符串、JSON 字符串、数组或自定义结构体。所有客户端应能直接用完整 NodeId 读到值，不依赖 BrowseName 搜索。

| 软件字段 | 完整 NodeId | 类型 | 节点值的单位 | 首次启动初始值 | 客户端权限 |
|---|---|---|---|---:|---|
| `T` | `ns=2;s=Local.iControl.Experiment1.Trends.Tr.Value` | Double | ℃，过程温度 | `25.0` | 读；可选允许联调工具写 |
| `T_j` | `ns=2;s=Local.iControl.Experiment1.Trends.Tj.Value` | Double | ℃，夹套温度 | `23.0` | 读 |
| `c` | `ns=2;s=Local.iControl.Experiment1.Trends.load.Value` | Double | `g/g H₂O`，溶质/水质量比 | `0.32` | 读 |
| `count_middle` | `ns=2;s=Local.iControl.Experiment1.Trends.count.Value` | Double | 测试计数标量，原值透传 | `17.0` | 读 |
| `T_j_set` | `ns=2;s=iCache.Temperature1` | Double | ℃，夹套温度设定值 | `22.0` | **读、写** |

NodeId 的大小写、点号和 identifier 必须一致。`ns=2` 是 namespace index，`s=` 表示字符串 identifier；这些点分字符串是节点标识，不要求创建同名的完整文件夹层级。若自行建立浏览目录，也必须保留上述完整 NodeId。

### 单位和数值约定

- `T`、`T_j`、`T_j_set` 都直接提供摄氏度。例如 25 ℃应返回 `25.0`，不要返回 `298.15` 或 `250`。Controller 读取时加 `273.15`；writer 写入前减 `273.15`，模拟器不再做这两次换算。
- `c = 溶解溶质质量 / 水质量`。`0.32` 表示每 1 g 水含 0.32 g 溶解溶质；与 `kg/kg H₂O` 数值相同。它不是“溶液质量分数 32%”，也不能把 `32` 当作 `0.32` 发送。当前链路对浓度不做缩放。
- `count_middle` 在通信测试中用非负计数标量 `17.0`。真实设备的计数单位、时间窗口、采样体积及累计/瞬时含义尚待确认；本接口不把它定义成颗粒总数、颗粒/mL 或 counts/s。当前链路对它不做缩放。
- 五个值均应为有限数值，不能是 NaN、Infinity 或空值。`c`、`count_middle` 不得为负；摄氏温度应高于绝对零度。

## 3 读取行为

1. 服务准备就绪时，五个节点必须全部存在且可读，返回 OPC UA `StatusCode=Good`。
2. Telegraf 默认每秒读取一次。常量节点可以连续返回同一个值；不要求每秒随机变化，也不要求五个值由服务器组成一个 JSON。
3. 节点正常提供 OPC UA DataValue 和时间信息；`Quality` 字符串以及统一的纳秒采集标签由 Telegraf 生成，模拟器不需要另建 `Quality` 或 `timestamp` 节点。
4. 支持客户端断开后重新连接，保持 NodeId、类型及 namespace index 稳定。重新连接本身不应把已经写入的设定值重置为初始值。

链路默认数据有效期为 5 秒，Controller 启动实验最多等待 10 秒取得完整数据。缺少任意节点、节点质量错误或连接中断都会影响完整状态生成。上述数值是当前通信配置，不要求模拟器实现对应的 Controller 错误处理。

## 4 T_j_set 写入行为

`T_j_set` 必须真正允许当前登录用户写 Value 属性。仅在浏览界面上显示“可写”不够，实际 OPC UA Write 请求也应成功。

收到写入后，请按以下规则处理：

1. 接受 Double 类型的摄氏度设定值，并保存为该节点的当前值。
2. 写请求成功返回后，立即读取同一个节点，应得到刚写入的设定值。读取与写入不能使用两份互不关联的存储。
3. 在下一次写入或显式重置模拟器前保持该值；不要让周期更新线程重新填回初始值 `22.0`。
4. `T_j_set` 表示目标设定值。第一轮不要求实际夹套温度 `T_j` 立即跟随，也不要求过程温度 `T` 跟随。
5. 不增加未约定的写入触发节点、方法调用、确认位或启停前置条件。当前 writer 只写这一个节点。

完整链路按每个控制周期的实际输出验收。设算法输出为 `S_K`，则 writer 应写入 `S_K-273.15 ℃`，读回换算后与 `S_K` 的差值应不超过容差。正式算法不会固定输出某个测试值；例如某周期实际输出为 `310.15 K` 时：

```text
Controller 该周期实际输出       310.15 K
writer 转换后发出的 OPC UA 值    37.0 ℃
模拟器 T_j_set 节点当前值        37.0 ℃
writer 读回并换算               310.15 K
Controller 收到的 verified      true
```

当前读回容差为 `0.05 K`，相当于温差 `0.05 ℃`；不要把设定值取整或延迟到下一个模拟周期才更新。当前 writer 默认允许范围为 `253.15–453.15 K`，对应 `−20–180 ℃`，这是可配置的通信保护范围，不代表真实设备的工作范围。模拟器应支持双方约定范围内的设定值并保留小数精度。

组员可先在 Controller 实验停止时，用 OPC UA 客户端向 `T_j_set` 写入 `37.0 ℃`，立即读取同一节点并确认仍为 `37.0 ℃`，断开重连后也应保持。之后显式恢复初始值 `22.0 ℃`，再交给集成方做五值初始读取。此操作是模拟器节点自检，尚未验证 Telegraf、RabbitMQ 和 Controller。

## 5 接入现有链路的配置

保持上述节点映射后，集成方只需按部署位置设置地址和认证。以下为同机 Windows 宿主模拟器的配置示例；将片段合并到集成方配置，不覆盖其已有 `.env`：

```dotenv
OPCUA_ENDPOINT=opc.tcp://host.docker.internal:4840
OPCUA_NAMESPACE_INDEX=2
OPCUA_T_NODE=Local.iControl.Experiment1.Trends.Tr.Value
OPCUA_T_J_NODE=Local.iControl.Experiment1.Trends.Tj.Value
OPCUA_C_NODE=Local.iControl.Experiment1.Trends.load.Value
OPCUA_COUNT_MIDDLE_NODE=Local.iControl.Experiment1.Trends.count.Value
OPCUA_T_J_SET_NODE=iCache.Temperature1

OPCUA_AUTH_METHOD=Anonymous
OPCUA_USERNAME=
OPCUA_PASSWORD=
OPCUA_SECURITY_POLICY=None
OPCUA_SECURITY_MODE=None
OPCUA_CERTIFICATE=
OPCUA_PRIVATE_KEY=
OPCUA_SERVER_CERTIFICATE=

OPCUA_VERIFY_WRITE=true
OPCUA_VERIFY_TOLERANCE_K=0.05
CONTROLLER_PROCESS_ENABLED=true
CONTROLLER_PROCESS_WRITE_ENABLED=false
```

`OPCUA_*_NODE` 环境变量填 identifier 部分，不带 `ns=2;s=` 前缀；上面的接口表才是完整 NodeId。Compose 会把共享设备配置提供给 Telegraf 和 writer，并把节点映射提供给 Controller；无需修改算法代码。

若模拟器确需使用不同的 endpoint、namespace 或节点标识，请交付准确映射，再统一调整配置；不要只改 Telegraf 而漏改 writer 或 Controller。修改通信配置前由集成方结束活动实验。上述片段先关闭写回，用于初始读取；只读联调完成并结束该实验后，再改为 `CONTROLLER_PROCESS_WRITE_ENABLED=true` 并重新应用 Compose 配置。

模拟器由组员单独启动，项目 Compose 连接这里配置的 `OPCUA_ENDPOINT`，不会自动创建模拟器。集成方还需启用正式 Controller 算法、发布有效参数并从 Central 启动实验；步骤和状态查询命令见 [过程读写接入说明](process-io.md#对接组员模拟器并验收)。

## 6 联调验收

| 检查 | 通过标准 |
|---|---|
| 连接和发现 | 从运行 Telegraf/writer 的环境能连接实际 endpoint，五个完整 NodeId 均可访问 |
| 初始数据 | OPC UA 值为 `25、23、0.32、17、22`，全部 Double、Good |
| 软件读入 | Controller 使用 `T=298.15 K`、`T_j=296.15 K`、`c=0.32`、`count_middle=17`、`T_j_set=295.15 K`；此项在第一次控制写入前检查 |
| 数据变化 | 将 `T` 从 `25.0` 改为 `26.0 ℃`，经 Telegraf 后 Controller 使用 `299.15 K` |
| 控制写回 | 对一个已完成周期，算法实际输出为 `S_K`，节点写入值为 `S_K-273.15 ℃`，读回在容差内且 Controller 收到匹配成功回执 |
| 值保持 | 停止控制后再次读取设定值，仍为最后一次写入值；读取或重连不会恢复初始值 |

“数据变化”可通过模拟器自己的管理界面或更新逻辑把 `T` 改为 `26.0 ℃`；若由 OPC UA 客户端修改，则需临时允许该客户端写 `T`。生产 writer 始终只允许写 `T_j_set`。模拟器可提供显式 reset 操作恢复初始值，但不要在运行中自动 reset。重新运行完整初始值验收前，需要显式恢复表中五个初值。

先完成只读阶段，再由集成方结束实验、启用写回并启动新实验。正式算法的输出取决于测量、参数和所需的生长率等输入；如果没有有效设定值产生，应检查 Controller 的算法状态，不能仅凭节点未变化判定模拟器写权限有问题。记录实际节点读数、实验标识及成功回执，作为本次模拟器联调证据；真实硬件后续单独验收。

## 7 交付给集成方的信息

- 从集成环境可访问的完整 endpoint，以及模拟器运行在宿主机、远程机器还是容器中。
- 启动、停止和显式重置初值的方法。
- 五个实际 NodeId、namespace index/URI、Double 类型及读写权限确认。
- 认证方式；本次按 Anonymous/None，若有变化需提前说明。
- 五值初始读取结果，以及向 `T_j_set` 写 `37.0` 后再次读到 `37.0` 的结果。
- 在联调中修改 `T` 的方法，以及是否允许 OPC UA 客户端写入；`count_middle` 如开始模拟真实计数，应补充其统计定义。

组员可使用项目已有依赖 `asyncua` 的 `Server` 或其他兼容的 OPC UA Server 实现上述接口；具体实现由组员维护。更多链路配置见 [过程读写接入说明](process-io.md)。浓度约定见 [参数元数据](../param_meta.yaml) 中的 `c_init` 和 [Controller 接口约定](controller-reference-validation.md)。
