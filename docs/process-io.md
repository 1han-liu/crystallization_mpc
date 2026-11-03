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

正常联调先提供好质量、有限的基准数值即可，不需要实现结晶动力学。模拟器需保留写入后的设定值，以便读回验证。生产节点类型通过 OPC UA 查询后按原类型写入；首次联调需核实实际类型能够表示小数温度。负值、非有限值和有限突跳的兼容测试与正常基准分开记录，按下文实际 MATLAB 算法阶段处理，不能在读取入口一律替换或拒绝。

组员模拟器可先提供固定初值 `25 ℃、23 ℃、0.32、17、22 ℃`。在尚未写回设定值时，Controller 应看到 `298.15 K、296.15 K、0.32、17、295.15 K`。正式 Controller 使用配置的算法计算设定值，输出不固定为某个测试温度；写回时应以该周期实际输出为准。

## 数据与命令契约

读取路由 `telegraf.to.process` 的消息直接采用 Telegraf 批量 JSON，不套业务 envelope：

```json
{"metrics":[
  {"name":"crystallizer_raw","tags":{"device":"crystallizer","id":"ns=2;s=Local.iControl.Experiment1.Trends.Tr.Value"},"fields":{"T":25.0,"Quality":"The operation succeeded. StatusGood (0x0)"},"timestamp":1791964800123456789}
]}
```

示例只展示一个节点，`Quality` 文本取自固定版本容器的实际输出；交付一个 `ProcessState` 必须集齐同设备、同纳秒时间戳的五个节点。完整轮次可以跨多条消息到达，一条消息也可包含多个轮次。时间戳应由实际采集生成，不能复制示例时间。不要把纳秒整数通过 JavaScript `Number` 中转，否则可能丢失精度。

采集时间使用 `time_source="collection_start"`，是同一次 Telegraf 采集的标签，不代表设备上的原子快照或设备源值更新时间。Controller 每次成功的过程读取消费一份尚未使用的完整采集轮次；数值与上一轮完全相同仍然有效。当前读取不再按 `PROCESS_SAMPLE_MAX_AGE_S` 拒绝一个成功交付的轮次，也没有 G 的 `2 × dt_G` 年龄门槛。错误 NodeId、无法归入通信中断的坏质量及消息结构错误仍按各自契约处理；成功交付的异常数值则进入其实际算法处理阶段，不能与传输读取异常混为一谈。

## MATLAB GUI 读取异常兼容行为

本节说明读取顺序、异常处理、数值表达及接口适用范围。

本节唯一 MATLAB 参考入口是只读原项目的 `source_codes/controller_for_gui.m` 及其实际调用子程序，不采用独立 `supersaturation_control.m` 或 `opcua_reader.m` 的行为。GUI 主循环的顺序是：

```text
记录周期 ii 和经过时间
→ 读取过程测量
→ 计算控制目标、滤波状态、积分和候选指令
→ 读取 G（启用 G 时）
→ 自适应拟合
→ 记录和显示
→ 写入设备指令
```

运行期间过程或 G 读取抛出异常时，只记录警告，跳过当前循环剩余步骤，下一轮按原周期再次执行。实验仍为同一个 `running` run；没有累计失败终止、额外退避、人工重新启动或读取恢复基线。读取重新成功的周期直接执行其后的正常步骤，不因此前读取失败自动重置滤波器、清零积分、清空拟合数据或整轮回滚。失败前实际完成的工作保留，尚未执行的工作不补做，不把失败周期改报为成功。

`control_tick_count`/`ControllerTickInput.tick_seq` 对应 MATLAB 的 `ii`：读取前即递增。真实实验的 `elapsed_s` 记录从实验开始经过的实际时间；数值模拟仍按仿真周期推进。过程读取失败时，周期及时间记录保留，但没有完整 `ProcessState`，不会产生伪造的正常控制测量或设备写入。G 读取失败时，前面的主 EKF、积分、历史和候选指令可能已经改变；不撤销这些修改，后续拟合和实际设备写入跳过。已实际生效的参数更新按其真实收据记账，读取失败不使同一更新再次刷新。

原版对 MATLAB 数组的按 `ii` 赋值会在以后一次赋值时隐式扩展未赋值槽。算法内部保留失败周期及成功读取的部分值，并在需要计算下一周期差分时按该索引语义解释空槽；其中的隐式零仅是数组扩展语义，不是读失败回退值，不会被封装为成功的 `ProcessState`、有效样本或 Grafana 正常测量点。

以下情况严格区分：

| 情况 | 处理 |
|---|---|
| 读取操作抛出异常 | 警告，跳过当轮剩余步骤；温度、浓度、计数和 G 均不因该异常自动取上一值 |
| 成功读取但浓度或计数为空 | 按 `safe_read_value` 取该字段上一条实际运行期历史值；初始标量、仅到达而未消费的消息均不是这份历史；尚无历史值则读取失败，不扩展到空温度 |
| 成功读取但数值未变 | 正常使用，不按年龄或数值重复拒绝 |
| G 整体读取成功但个别字段缺失 | 保留字段缺失，不改报为整体读超时，也不补为零或旧 G |

对于最后一种情况，开启自适应时，原版先执行 G 滤波器的 `predict`，再由 `correct` 的有限测量校验拒绝含 `missing`/NaN 的测量。对应实现保留已经完成的预测状态和 P，记录 `growth_filter` 阶段的警告，跳过拟合和设备写入；不会为消除缺失值新增重锚或清窗。变化率测量仍含缺失值的后续周期同样按这个顺序处理。这一处理对应原调用顺序及 `ExtendedKalmanFilter`、`validateInputSizeAndType` 的有限测量要求。

读取警告可在 Controller `/api/status` 的 `scheduler.last_read_warning` 中查看，包含 `tick_seq`、`elapsed_s`、`stage`、`message` 和记录时间；日志也输出 warning。纯读取异常不创建永久 `growth_wait`，也不进入旧的 `process_data_timeout` 恢复基线流程。Telegraf 的原始 `crystallizer_raw` 与 Controller 是否完成该次计算分别记录；没有完成的控制周期不补造成功曲线或滤波刷新证据。

MATLAB 的最初 GUI 参数/状态客户端连接和参数接收在主循环 catch 外，不能与主循环内的过程设备激活混用。`initialize_controller` 中的过程连接、五项初值读取及后续 `refresh_controller` 均在主循环 catch 内；不能把这些初值读取失败统一称为不可恢复的启动错误，也不能自行改为反复重读初值。实际标志顺序有两种不同分支：

- 激活时 `create_nodes` 或任一初值读取抛异常：历史已清空、`ii=0`、`controller_active_old=true`，但调用方尚未设置 `controller_refresh_request=true`。下一轮不再执行激活初始化，而是记录 `ii=1` 并进入普通读取。全新工作区连接失败会在后续 `uaClient.Status` 未定义处继续警告；已有客户端但没有 EKF 时，后续四项读取可以写入历史，随后因 EKF 未定义而警告。已有旧客户端、EKF 和积分的工作区则可沿用它们继续计算。异常前已成功赋值的初值标量保留，异常后的标量保持未定义或原值；没有自动补建滤波器、补做刷新或整轮回滚。
- 五项初值读取成功，但 T/c 的 NaN/Inf 使 EKF 构造失败：`controller_refresh_request` 已设为 true 且尚未清除。后续循环重复刷新缓存的初始 T/c，`ii` 仍为零，不会仅因外部数据恢复就重新读取初值。

以上是原版实际工作区和标志顺序，不是新增的恢复保护。异常测量数值按下节核对实际失败阶段；这不等于把程序错误、持久化故障或进程重启都改成忽略，也不改变控制公式、参数含义或原版已有的温度指令限幅。

新版相应区分这两个激活阶段：先由 `adapter.start()` 清理本次运行的历史前缀，再执行 `read_initial_state()`；只有初值全部读取成功后，才按原脚本顺序生成供模拟使用的晶种尺寸模板，然后构造/刷新滤波器。这个模板生成不代表设备实际加种。初始读取失败时记录 `seed_generation_completed=false`，不伪造已经生成的尺寸群。首次过程连接或读取异常记录警告并保留 `RUNNING`；初值读取异常之后不会在下一条正常测量上自动构造 EKF。全新运行后续的正常读取仍可记入历史，但缺失 EKF 时在该阶段警告并跳过余下步骤。若首次连接没有建立客户端，则保存 `activation_client_unassigned`，后续周期继续计数，在原版 `uaClient.Status` 未定义的对应位置警告，不自行重新调用初始化连接。这与“运行中已有滤波器时读取恢复后直接继续”不同。初值已经读到但构造器失败时，则保留待刷新标志及相同缓存初值。

原生适配器的初次读取顺序为 `T → T_j → T_j_set → c → count_middle`；运行期只读 `T → T_j → c → count_middle`。`T_j_set` 保存其初次实际读到的值，即使后面的 c/count 读取失败也保留；从未完成该项初次赋值时为 `None`，不伪造设定值，也不为此增加新的运行期读门槛。原生适配器在一次读取入口发现客户端已断开时才重连；一次失败读取内部不重试。Controller 进程真正重启后仍进入 `RECOVERY_REQUIRED`，不因检查点中保存了这些阶段信息而自动续跑。

### 异常数值：输入成功不等于算法一定成功

MATLAB 的 `readValue`、`read_value` 和 `safe_read_value` 没有统一的负浓度、负计数、绝对温度或突跳拒绝门槛。与此同时，实际调用的 MathWorks 滤波器具有类型及有限性要求。因此既不能把所有异常数值在读取入口拦成等待，也不能声称原版会接受任何数值并完成控制。应按实际执行位置核对：

| 数值及阶段 | 处理位置与行为 |
|---|---|
| 初始化 T 或 c 为 NaN/±Inf | `[T; 0; c; 0]` 传给 `extendedKalmanFilter` 构造器；其 `State` setter 要求实数、有限浮点向量，不能建立这个初始状态 |
| 初始化 T/c 为有限但负浓度、非正 Kelvin 温度 | `State` 的有限性检查本身不限制正负；以后模型中的除法、指数和数值积分是否成功必须按具体状态验证，不能凭符号提前推断 |
| 运行期 T、c 或其差分为 NaN/±Inf | 原始历史和差分先赋值，主 EKF 先 `predict`；若预测完成，随后测量 `C*x + Du` 的非有限结果由 `correct` 的有限性校验抛出异常。保留已完成预测的状态与 P，跳过当轮后续控制/G/拟合/写入 |
| 非有限 T/c 后紧接一条有限测量 | 差分仍可能包含上一条非有限原始值；原版用完整矩阵乘法生成测量，不能仅提取当前 T/c 后宣称已经消除所有非有限项 |
| 运行期 T_j 或 count 为负、NaN/±Inf | 原版记录这两项；它们没有进入主 `[T,dT/dt,c,dc/dt]` 测量。实际 GUI 自适应脚本也没有以 count 作为启动门槛。不能仅因此统一拒绝整个读取；显示或其他后续步骤的结果另行核对 |
| G_u/G_v 原始方向值非有限，但 G_u_KF/G_v_KF 有限 | 原版分别保存四项；拟合前平均只使用两个 KF 方向值，原始方向异常本身不等于 G 滤波测量异常 |
| G_u_KF/G_v_KF 缺失或非有限 | 平均和原始历史先保存，G 滤波先预测，再在校正测量有限性校验处失败；见上节，不能替零、取旧值、重锚或撤销预测 |
| 有限的负 G | 滤波器有限性校验没有正值要求；拟合函数随后只选取正的滤后 G 样本，不应把负号本身解释成读取超时 |
| 有限的过程数值突跳 | 原版没有突跳幅度门槛；继续通过实际滤波、动力学、优化和原有指令限幅。极大值导致的溢出、奇异状态或求解器异常不能统一认定为成功或提前替换 |

`calc_dT_dt_set` 使用 `fminbnd` 的默认选项，未启用 `FunValCheck`。优化目标中的非有限数值不应统一归类为读取异常；后续结果由滤波、动力学、优化器及原有温度指令限幅按各自执行位置处理。求解器返回也不等于控制质量合格。

拟合函数按原规则选取非 NaN、正的滤后 G 历史；有效样本少于 `min_num_adapt` 时更新实际样本数并保留参数。负或非有限的 σ、T、G 可能在目标计算或优化阶段引发异常，不能因此将此前成功的读取改报为超时，也不自动清空拟合历史。

| 拟合输入或执行位置 | 原算法处理位置与行为 |
|---|---|
| σ、T、正 G 中的非有限值，或负 σ 与非整数 n | 目标或导数可能未定义，由拟合阶段抛出异常；调用方保留旧 params 和 num_adapt |
| 负 σ 与整数 n | 负号本身不是统一拒绝条件，按实际目标函数和当前适应模式计算 |
| 三参数拟合的后续阶段抛异常 | 函数未返回时，局部先前拟合值不提交给调用方，也不替换主 EKF 捕获的参数 |
| 正、非 NaN 的滤后 G 样本不足 `min_num_adapt` | 更新实际样本数并保留参数，不作为读取异常 |
| 有限但极大的 G | 进入原参数搜索范围内的拟合计算，不在读取入口统一拒绝 |

函数输出赋值与整轮回滚必须区分：原拟合函数若抛异常，调用方没有收到该次函数输出，因此保留旧 params/num_adapt；这不是撤销此前主 EKF、控制积分或 G 校正。主 EKF 的预测/测量函数句柄只在拟合函数末尾更新；后续参数拟合阶段失败不会把局部已拟合值泄漏给调用方。`to_adapt_list(ii)`、已生成的滤后 G、候选指令等在函数调用前完成的工作仍保留，后续记录和设备写入跳过。异常值留在历史后，即使当前输入恢复，后续差分或拟合仍可能抛异常；不自动替零、重锚或清窗。

原主循环的 `add_points`、`drawnow limitrate` 位于设备写入之前。显示步骤若抛出未被局部捕获的异常，也会跳过当轮余下步骤，不能只根据候选指令已生成就认定设备写入已经发生。

### 本次修改前后与文件分组

| 项目 | 修改前 | 本次修改后 |
|---|---|---|
| 运行期过程读取失败 | 部分通信异常进入等待/恢复基线，其他异常可能结束控制 | 读取阶段警告，保留本次运行及实际完成前缀，跳过当轮余下步骤，下一正常周期再读 |
| 过程和 G 数据年龄 | 按过程年龄或 `2 × dt_G` 拒绝数据 | 成功交付的过程轮次和可读 G 邮箱不按年龄拒绝；失败读取不伪装成旧值读取成功 |
| G 读取位置 | G 可用性可在本轮控制计算前阻止执行 | 候选控制计算后才读取 G；G 读错不撤销此前滤波、积分及历史 |
| 过程初值读取 | 先读过程，再开始算法；部分失败进入待激活或 ERROR | 先清历史前缀；按原 GUI 激活阶段保留警告、标志及未定义状态，不自动补做初始化 |
| 原生 OPC UA 过程读取 | 初次和运行期共用五项读取 | 初次五项，运行期四项；初次实际读到的 T_j_set 保留 |
| 负值、NaN/Inf、有限突跳 | 读取及消息入口存在统一数值拒绝 | 保留输入，按实际主滤波、G 滤波和拟合阶段决定结果；不增加替零、限跳或清窗 |
| 非有限状态的传输与记录 | JSON 或序列化路径可能拒绝、遗漏这些值 | 显式无损编码，诊断和归档保留实际值，有限值类型保持不变 |

生产文件按职责归组如下；本地测试及验证材料不属于正式运行依赖，未随正式代码分发。

| 模块 | 本次修改文件组 | 用途 |
|---|---|---|
| Controller 调度与适配 | `apps/controller/{service,adapter,app,tick,result}.py` | 读取顺序、激活阶段、警告及周期前缀，状态接口与结果传递 |
| 过程输入 | `apps/controller/{process,rabbit_process}.py` | 初次/运行期原生读顺序、部分读取与空值语义、Rabbit 采集轮次、移除年龄拒绝 |
| 控制与自适应算法 | `apps/controller/algorithm/{controller,integration,ekf,dynamics,control,adaptation,_checkpoint,_ode45_ieee}.py` | MATLAB 索引空槽、滤波和拟合失败前缀、平方根协方差及相关 ODE/优化顺序、检查点保真 |
| 数值编码与消息 | `infra/numeric.py`、`messaging/{codecs,contracts}.py` | NaN/±Inf 的无损编码及逐项 G 数据表达 |
| 数据诊断与导出 | `apps/controller/{diagnostics,telemetry}.py`、`infra/influxdb/{durable,write}.py`、`experiments/exporter.py` | 实际非有限诊断、Influx 伴随字段、严格 JSON 和 CSV 还原 |
| Central 观测 | `apps/central/process_source.py`、`apps/central/ui/static/app.js` | 观测卡片按实际数值显示，不另加数值输入拒绝 |
| GSensor 数据出口 | `apps/gsensor/app.py` | 各方向原始/KF 数据独立保留，不因另一方向无效而整体丢失 |
| Telegraf | `config/telegraf/telegraf.conf` | 在 JSON 序列化遗漏非有限数值之前编码，并区分消息与归档输出 |
| 说明 | `docs/process-io.md` | 读取行为、对应实现、使用方式及接口适用范围 |

上表中的源码路径均相对于 `src/crystallization_mpc/`，`config/` 和 `docs/` 两项相对于项目根目录。修改范围不包括 MATLAB、设备 Writer 的执行算法或原有温度指令限幅；设备接入仍需按现场配置单独验收。

异常数值处理参考项目实际调用的函数和 MathWorks 源码：`ExtendedKalmanFilter` 的 `set.State`、`predict`、`correct`，共享 `validateInputSizeAndType`，以及 `fminbnd` 的默认选项。MathWorks 工具箱源码不属于本项目分发内容。

JSON 不支持标准裸 NaN/Infinity。传输和检查点使用明确的无损表示，例如 Python 文档中的 `{"__mpcrystal_float__":"NaN"}`、`{"__mpcrystal_float__":"+Inf"}`、`{"__mpcrystal_float__":"-Inf"}`，Telegraf 平面字段使用保留 token `@mpcrystal:float:NaN`、`@mpcrystal:float:+Inf`、`@mpcrystal:float:-Inf`。原始 InfluxDB 归档把相应非有限项保存为 `<字段>_nonfinite` 字符串伴随字段，正常有限项保持原值与类型；不把保留 token 写进原数值字段造成类型冲突。诊断快照同样保留真实状态、P、创新和候选控制量中的 NaN/Inf，通过伴随字段归档，不因为数值异常而丢弃整份快照。解码后仍是原来的数值类别，不是零、空值、旧值或预测值；缺失字段也不补为零。异常读数与成功的有限控制结果分别记录，不能为得到可画曲线而伪造正常数值。

### 传输接口与适用范围

- 原生适配器已分别实现初次五项和运行期四项读取，保留实际已读部分及初次 T_j_set；不再在每个控制周期额外读取第五项。正式 RabbitMQ 路径仍以五字段同轮采集为单位，无法从一个缺字段轮次还原 MATLAB 每项同步函数调用及部分工作区状态。五节点采集也不代表设备上的原子快照。
- 原版初始化直接使用 `readValue`，可将成功返回的空数组保留在工作区；`safe_read_value` 的上一历史值回退只用于运行期 c/count。新版 `ProcessState` 是标量结构，不能无损携带这种初始化空数组，当前对它明确报空值错误；不能把这项表示差异称为真实读取超时，也不能擅自沿用旧初值填充。
- 原版单个 MATLAB 进程的工作区可跨停用/再激活保留旧客户端、EKF 和积分；它们在后来一次激活读取失败后可能继续参与计算。新版新 run 使用独立的算法状态，不跨 run 复用上一实验的 EKF/积分，因此不与跨实验旧工作区行为等同。
- RabbitMQ 没有新完整轮次、连接中断或收到明确的通信失败质量时，应用层本次读取报错。它不证明 MATLAB 的某次 `readValue` 已抛出超时；没有新消息也不能当作成功读到了旧节点值。重连后仍使用本次连接实际收到的轮次，不从检查点恢复测量。
- Telegraf 1.40.1 的 OPC UA 插件还限制了空值等价性：收到 nil Variant 时未必清除内部已有值，空数组也可能不产生字段；下游不能据此恢复原版一次 `readValue` 返回空值的完整事实。原生适配器与隔离消息可验证明确空值的回退，但不能据此声称真实 OPC UA 空 Variant/空数组经 Telegraf 的端到端行为已与 MATLAB 完全一致。
- G 使用最后一条成功接收、符合实验与版本身份的消息作为可读邮箱，数值未变或消息年龄增长不拒绝。完全没有 G 消息时，在控制计算后的 G 读取位置报错。生产消息仍受既有结构契约约束，不能把任意损坏字典当作成功的 MATLAB map；内部 reader 的部分字段返回可单独验证缺失字段语义。
- 新版保留运行参数的测量边界应用和跨服务收据协议；过程读取失败前尚未应用的侧栏修改仍为 Pending。原版 `receive_modified` 位于周期记录和测量之前，可先修改 MATLAB 工作区。这项参数提交时序没有借本次读取异常修改而重写，不能宣称所有操作事务与原版逐语句等价。
- Writer 的执行日志、命令关联、回执未知核实及其恢复基线属于独立写入协议，见下一节；测量读取异常不会自动消除未知写入结果。
- MATLAB 与 Python 的数值求解器、浮点运算和运行时实现存在差异；相同控制流程不构成逐项数值完全一致的保证。模拟器接入、完整 GUI 生命周期及真实设备读写也应分别确认。

## 写入命令与回执

命令和回执使用现有 envelope，`ver=1`，`correlation_id=command_id`：

| 项目 | 命令 | 结果 |
|---|---|---|
| 路由 | `controller.to.opcua_writer` | `opcua_writer.to.process` |
| `src` / `dst` | `controller` / `opcua_writer` | `opcua_writer` / `process` |
| `msg_type` | `command` | `event` |
| `name` | `process.setpoint.write` | `process.setpoint.result` |
| payload | `command_id, run_id, run_started_at, tick_seq, setpoint_K, expires_at` | 相同命令及实验/周期标识，`status, error, write_result` |

时间字段采用带时区的 ISO 8601。`tick_seq` 是 Controller 周期序号。`status` 为 `success`、`failed` 或 `unknown`。成功的 `write_result` 包含 `setpoint_K, setpoint_C, written_at, verified, readback_K`。Controller 只有收到身份匹配、读回验证通过的结果才记录成功；发布到 RabbitMQ 不代表设备已写入。

默认命令有效期与等待回执时间均为五秒。等待回执超时保留当前实验，暂停新的优化、积分、拟合和设备指令，不生成新命令自动补发。Controller 保存原命令身份，通过 `process.setpoint.status` 查询 Writer 的已保存结果；查询不执行设备写入。Writer 尚未收到该命令时，查询会保存取消记录，阻止该旧命令随后到达时执行。迟到回执可核实恢复条件，但不会把原超时周期改报为成功。

`opcua-writer` 的 SQLite 日志在写前记录 `executing`，写后记录结果；重复命令返回持久化结果。重启遇到未完成记录时保留 `unknown`，禁止猜测并重新写设备。这并不承诺物理动作“恰好一次”。消息消费 ACK 必须晚于结果发布确认。

结果无法自动核实时，Central 显示 **Confirming previous command** 及 **Confirm device state and resume**。操作员必须先核实设备当前状态，并确认旧指令不会继续执行，再明确确认；按钮只转发该 run/command 的确认，不发送夹套设定值。Writer 单独保存核实记录，不覆盖历史 `unknown`。确认送达也不等于已恢复：需要 Writer 对应确认和恢复边界之后的完整有效测量，首个恢复周期只建立测量基线、不写设备，下一正常周期才恢复控制。无法核实时继续保留当前实验等待，操作员仍可 End Experiment。

运行状态与 `integrations.process.write_recovery` 可查看写入等待原因、原命令及是否需要人工确认。网页分别显示 **Reconnecting**、**Waiting for process data**、**Confirming previous command**；这段写入核实等待没有自动结束实验的时长上限。已经提交的参数和滤波刷新不重复执行；未提交更新保留原事件身份和 Pending，写入核实后的恢复基线不应用参数，后续有效周期只应用一次。上述人工确认和基线仅用于结果未知的设备写入，不用于普通测量读取超时。Controller 真正重启、状态损坏、算法故障等不属于通信重连自动续跑。

## 配置迁移与生产启动

通信恢复协议需要 Controller、`opcua-writer` 和 Central 使用同一版本；部署时在实验结束后一起更新这三个服务。旧 Writer 不支持 `process.setpoint.status` / `process.setpoint.confirm`，只更新 Controller 不能完成写入超时恢复。状态查询使用独立 `query_id` 作为消息关联标识，并回显恢复代次；原写入仍以 `command_id` 去重。

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

通信参数保留 `PROCESS_SAMPLE_MAX_AGE_S=5`、`PROCESS_STARTUP_TIMEOUT_S=10`、`PROCESS_COMMAND_TTL_S=5`、`PROCESS_WRITE_TIMEOUT_S=5`；`PROCESS_SAMPLE_MAX_AGE_S` 仅保留配置兼容，不再作为过程读取或写前测量年龄门槛。`PROCESS_STARTUP_TIMEOUT_S` 限定单次连接/等待完整轮次的调用时长，不是运行中实验的累计等待上限。命令有效期和写回执超时仍属于独立写入协议。Telegraf 的 `TELEGRAF_INTERVAL/TELEGRAF_FLUSH_INTERVAL` 默认 `1s`；这些是通信默认值，不是 MATLAB 原版设置的读取超时秒数。

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

在 Central 页面 `http://localhost:8000` 配置并启动新的 `experiment` 实验，使用已发布的有效参数及其所需的生长率/图像输入。服务启动本身不会自动创建实验；过程适配器在实验激活时连接并尝试读取完整初值。须分别检查激活读取、初始滤波器构造和首个控制周期，不能仅凭启动按钮已受理就判定控制已经执行。激活异常的原版标志顺序见上节，不把它统一改报为运行期读取超时或自动重做初始化。生产算法仍需满足其正常输入条件，五个假值只替代设备过程测量。

在 PowerShell 读取状态：

```powershell
$controllerStatus = Invoke-RestMethod 'http://localhost:8002/api/status'
$controllerStatus.integrations.process | ConvertTo-Json -Depth 8
$controllerStatus.last_control_output | ConvertTo-Json -Depth 8
```

在实验运行期间，检查 `integrations.process` 的 `connected=true`、`sample_valid=true`、`missing_fields=[]`，并结合 `collection_timestamp_ns`、`last_used_timestamp_ns` 核对本轮实际交付和消费；`sample_age_s` 仅供观察，不是年龄拒绝门槛。`last_state` 应包含五个换算后的初值；`last_control_output.process_state` 则显示最近完成计算实际使用的测量，不能把旧输出当作失败周期的新结果。模拟器把 `T` 从 `25.0` 改为 `26.0 ℃` 后，后续采集及控制周期应看到 `299.15 K`。此阶段 `write_enabled=false`，Controller 不应产生过程写入。

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

联调时记录 endpoint、节点映射、服务版本、实验 `run_id`、测量变化、匹配命令/回执和独立读回值。组员模拟器和真实硬件应分别验收，不能把模拟器上的结果当作现场设备验收。读取异常检查应确认仍为同一个 run、失败周期已经增加、异常后的步骤未执行，下一次读取成功直接继续，没有读恢复基线。新采集轮次数值不变应被接受，失败读取不能回退使用已消费缓存。不要复制历史消息或重放历史控制命令来模拟恢复。Writer 结果未知另外按写入核实流程验收，人工 End 后的迟到回执不能恢复该实验。

## Telegraf 固定版本运行约束

**Telegraf 采集轮次验收必须常驻运行。** 本配置固定 1.40.1；`--test`/`--once` 绕过采集开始时间初始化，不可作为轮次验收方式。Compose 启动正常 Telegraf 进程，通过 Controller 状态和 AMQP 输出检查完整采集轮次。

**1.40.1 配置兼容问题：** 这个版本的 `buildInput` 支持 `time_source`，但未使用字段检查的白名单漏掉了它，直接放在 `[[inputs.opcua]]` 中会启动失败。本项目保持官方 `telegraf:1.40.1` 镜像，采用该版本支持的旧式 `[opcua]` 输入表，仍创建同一个 `inputs.opcua` 插件并使用相同运行逻辑。通过两个显式 `--config` 参数，先加载 `telegraf.conf`（agent/output），最后加载 `opcua.conf`（单个输入）。**顺序不能颠倒，不能再追加配置文件或 `--config-directory`**，否则后续加载会再次触发该版本错误的未使用字段检查。升级 Telegraf 时应重新验收并考虑恢复现代配置形式；不要删掉 `time_source` 或改为按到达时间拼接五个节点。

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

AMQP 与 InfluxDB 是独立输出：数据入库不表示 Controller 已使用它，已完成控制计算的使用记录以 `controller_measurement` 为准，失败读取及已执行前缀另外结合周期警告与检查点核对。断网后积压原始数据可补入数据库；当前 Controller 不按测量年龄拒绝成功交付的完整轮次，但连接代次、收集完整性和一次消费规则仍适用，因此不能承诺每条积压记录都会进入计算。未实际采集的停机时间、Telegraf 已过滤的坏质量值无法补造；CSV 和 `archive.json` 会显示可检测的缺字段、间隔和质量问题。

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

`EXPERIMENT_EXPORT_ENABLED=true` 默认开启结束后自动导出；关闭后仍可手动导出。旧实验不会自动批量重导，历史缺失记录无法追溯生成。数据库、持久队列和 CSV 均未设置自动删除策略；InfluxDB 实际保留时长取决于 bucket retention。容量规划应同时包括这些记录、设备采集和图像。Telegraf 1.40.1 的磁盘缓冲为实验性功能；当前关闭逐点 fsync 以满足一秒周期，**不保证宿主机断电时最近缓冲零丢失**。若在实验室启用 fsync，应重新验收采集和交付延迟，并保留每个控制周期的实际输入来源记录。

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
