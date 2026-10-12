# 本机 Grafana 实时可视化

本机部署使用项目 Compose 已固定的 InfluxDB 2.7、Grafana 10.4.3 镜像及现有
`grafana/` 仪表盘/数据源配置，不安装完整 Compose 平台，不修改 GSensor。
仅绑定 127.0.0.1；请勿将这套本地演示配置暴露到公网。版本升级和远程访问另行评估。

## 打开

[实时仪表盘](http://127.0.0.1:3000/d/crystallization-mpc-analysis?from=now-15m&to=now&refresh=5s)

也可以点击 Central 顶部的 Grafana。右上角选择 Last 15 minutes 和 5s 刷新；
“有数据的实验”选择当前 run_id。默认首页可能显示更长时间范围，需要自行缩短。
本机免登录仅授予 Viewer 权限；管理账户随机密码保存在本地受限文件中，不在文档或 Git 中公开。

图表包含温度、浓度、sigma、G、目标/误差/目标函数、E_A/k_0/n 和滞后时间。
第六步将它们整理为两个可折叠分区：顶部 **Control Targets** 并排展示 sigma/G
目标图，下面 **Process Measurements & Diagnostics** 展示其余 10 张过程图。
实验和时间筛选共用。
只进行 sigma 控制时，G 目标图可以没有数据；没有真实设备时 Count 图可以没有数据。
`Samples / completed fits / failures` 仍在 Central 的拟合状态行；新采集诊断的
实验另在下方默认折叠诊断分组中显示本段样本、最低样本要求和刷新次数。
真实拟合参数变化可查看 Growth parameters。

## 自动刷新四阶段验收（2026-10-09）

只使用上述默认仪表盘，不另建、复制或导入验收专用仪表盘。选择本次唯一
run_id 和实际写入的 UTC 时间范围。默认面板查询保留原始采样点，不再使用
aggregateWindow 平均值掩盖快速运行的配置切换；缺测不补零，spanNulls 保持 false。
查询以实际 tick/frame 为锚点，缺失字段、已记录的无效周期和未激活目标输出
明确 null，避免跨这些区间连线。完全未写入数据库的周期不凭空推断；正式验收
必须独立核对完整 tick 数。

加速验收使用真实算法和原生 controller_measurement 写入，额外的唯一周期
标签避免同一毫秒的数据覆盖。数据库计算时间仍是实际 UTC，不伪造日期。
仪表盘统一使用 UTC 显示横轴、表格时间与时间筛选，不随浏览器本地时区改变。
此设置仅改变显示时区，不改数据库时间、面板查询或 URL 的 from/to 时间范围。
当前默认面板横轴仍是实际 UTC 时间；阶段时长必须查逐周期记录中的 elapsed_s
（除以 60 得仿真分钟），不可把 Grafana 上几秒钟解释为实验只有几秒钟。

参数快照、阶段/加种/刷新事件表、周期与仿真分钟对应关系及 PASS/FAIL/NOT RUN
报告由独立验收会话保留。2026-10-09 的旧实验没有采集滤波内部诊断，刷新计数
和本段样本数只在该次报告中核对；下节新增图不能反向重建这些旧数据。
截图需标明本次实验编号及“加速数值仿真／模拟生长率”，优先保存到
桌面，文件名含 run_id 且不覆盖已有文件。无法截图时打开筛选好的默认仪表盘。

该验收不使用 run_fresh_simulation.py 的旧会话清理功能，不删除任何旧实验或
数据库记录。初始参数/周期只覆盖验收会话；数值仿真不能证明真实相机预热、FBRM
计数或晶种物理溶解。论文末端曲线和拟合数值不作为必须复制的截图模板。

独立四阶段验收命令（不清理旧会话、不操作硬件）：

```bash
.venv/bin/python scripts/run_refresh_acceptance.py \
  --rabbit-env .runtime/rabbitmq-debug/runtime.env \
  --telemetry-env .runtime/grafana-local/controller.env
```

控制命令采用正常验证的 `ControllerService.on_message` 信封，在隔离服务实例
内调用；这不是 RabbitMQ 消息传输的端到端验收。正式实验报告同时保留未测项。

2026-10-09 实测 `exp_refresh_20261009T145233_1cdff427`：480 个有效周期，
120 仿真分钟；数据库只读复查得到 480 个不同 tick 和 9 条实际事件注释。
前三阶段通过，阶段四跟踪要求失败且夹套终点未达到，**整组 FAIL**，不是完成
论文全程的成功复现。1 次晶种事件（tick 41 消耗），2 次刷新（tick 41、61）。
结果位于 `.runtime/acceptance-refresh/exp_refresh_20261009T145233_1cdff427/`。
图中 UTC/本地时钟不是仿真分钟；精确映射见同目录 `timeline.csv`。
独立结论、持续失跟窗口、样本重新累计表和桌面真实截图路径见同目录
`verified-review.md`。完整本地回归：119 passed、7 subtests passed（含 30 项
只读本机 Flux 检查）。MATLAB 源文件摘要不变；无 GSensor 修改，无 Git push。

## Controller 滤波刷新与控制诊断（2026-10-11）

同一默认 UID `crystallization-mpc-analysis` 的底部追加默认折叠的
**Controller Filter & Control Diagnostics**。展开后有 11 个新增面板，原有
12 张过程/控制图、编号、查询、位置及实验/时间筛选不变。此功能只增加观察，
不调整 Q/R、初始化 P、拟合方法、控制约束或物理边界，不刷新 GSensor。

| 面板 | 主要证据与单位 |
|---|---|
| Refresh & Samples | `Refreshes`、`Fit Segment`、`Samples`、`Min Samples`：已完成刷新次数、拟合段、本段样本与最低样本要求 |
| Filter Status | `Process (T, c)`、`Growth (G)`、`E_A`、`k₀`、`n` 的真实初始化、更新、重锚、暂停及预留状态 |
| Temperature Filter | `Measured`、`Predicted`、`Estimated`：测量、预测、校正/初始化温度，°C |
| Concentration Filter | `Measured`、`Predicted`、`Estimated`：测量、预测、校正/初始化浓度，g/gH₂O |
| Growth Rate Filter | `Input`、`Predicted`、`Estimated`：有效输入、预测、校正 G，μm/s；刷新初始化 G=0 不作为校正值 |
| Relative Uncertainty | main 四分量和 G 三分量的 Pii/Pii初值，正值对数轴；实际是方差比，不是标准差比 |
| Scaled Prediction Error | `Temperature`、`Concentration`、`Growth Rate` 的实际校正前 innovation/√Sii，无未经验证的统计 PASS 阈值 |
| Reset Check | 每个量一行，周期、仿真分钟、阶段、滤波器、实际/参考值及单位 |
| Control Integrals | `Target Error`、`Temperature-rate Error`、`Temperature Error` 的积分历史与快照阶段，各行独立标明单位 |
| Temperature Change Rate | `Requested`、`Estimated`：实际 MPC/PI 请求与主 EKF 温度变化率，K/s；未求解周期留空 |
| Jacket Temperature | `Request (raw)`、`Request (limited)`、`Zero-rate (raw)`、`Zero-rate (limited)`、`Final Command`、`Actual`，°C；tooltip 显示限幅标志和最终分支 |

以上 11 个面板的标题、图例、生命周期、表格列/信号、缺测提示和说明使用英文，
属于显示层改名，Controller 字段、原始 Flux 输出别名和计算语义保持不变。
图例/状态条统一显示 `名称 · 短实验编号`，例如 `Measured · 50434261`。
同一面板所选时间范围内，实验编号末八位唯一时显示末八位；若所选实验中有相同
末八位，冲突实验显示各自完整 `run_id`。少于八位的编号原样显示。
查询始终使用完整 `run_id` 筛选和分组，`run_label` 只辅助显示，不能合并实验。
表格仍保留完整 `Run ID`；UTC 时间、`acceptance_tick` 和原始周期点不变。

两个表格的英文列为 `Signal`、`Value`、`Reference`、`Unit`、`Filter`、
`Cycle`、`Stage`、`Time (UTC)`、`Sim Time (min)`、`Run ID`、`Status`，
所有原有列均保留。`Temperature (T)` / `Temperature Rate (dT/dt)` 等名称
对应原始状态量，`Variance (T)` 等名称对应 P 对角项；不重算或改写存储数据。
普通缺测显示 `Unavailable`，相对不确定度对数图仍显示短横线 `—`。

### 如何核对一次刷新

在同一实际完成周期，`Reset Check` 的 `Stage` 为 `Reset`（原始
`snapshot_stage=refresh_init`），
`T_KF` 和 `c_KF` 的参考值是当周期测量；`dT_KF/dt` 与 `dc_KF/dt` 的参考值
均为 0，单位分别为 K/s 与 `(g/gH₂O)/s`。P 对角项实际值应等于各自构造初值，
相对协方差图应为 1，而不是 P 清零。同步核对刷新次数、本段样本和三个积分项。
下一符合更新条件的周期恢复正常更新；P 不要求一直单调下降。

相对协方差的有效数字使用科学计数，未采集/缺测以短横线表示，不能解释为
P=0；旧实验没有这些记录时不产生协方差曲线。对数轴被过滤的小刻度可能
显示短横线，但不使用会占满纵轴的长缺测文字。

积分表的 `int_e_target_dt` 单位为 `dimensionless·s`：G 模式的目标误差经
动力学关系换算为无量纲的 sigma 等效误差，不是原始 m/s 生长率的积分。
另外两项积分分别使用 K 与 K·s；不同量纲不共用曲线纵轴。

初始化是重新定位，不是一次校正：刷新周期预测、创新及 G 校正曲线留空。
边界表只保留实际刷新点及非正 P 异常点；普通校正快照不会覆盖初始化证据。
完整初始化状态与 4×4/3×3 P 深拷贝保留在验收 `ticks.jsonl` 的
`diagnostics.raw.refresh_init` 中，非对角项由回归测试和原始记录验证，图上
仅检查对角项不能独立证明完整矩阵正确。

生命周期的原始值 `refresh_init`、`updated`、`reanchored_updated`、`paused`、
`reserved`、`refresh_reserved` 分别显示为 `Reset`、`Updated`、`Re-anchored`、
`Paused`、`Unused`、`Reset (unused)`。G 缺测恢复的重锚不算完整刷新；三个预留参数
滤波器只显示已重建/未更新，不代表它们参与参数估计。输入值、拟合参数跳变
或曲线断点都不能单独作为正确重置的证明。

生命周期面板保持默认双列半宽布局，以简短英文与末八位编号减少裁切；
`Process (T, c) · 50434261` 对应主 EKF，`Growth (G) · 50434261` 对应 G
滤波器。图上的 `Estimated` 在刷新周期表示初始化状态，不表示已进行一次校正。
相对不确定度的七条线分别为 `Temperature`、`Temperature Rate`、
`Concentration`、`Concentration Rate`、`Growth Rate`、`Growth Slope`、
`Growth Curvature`，后三项对应 G、dG/dt、d²G/dt²，不是三个新的滤波器。

夹套 tooltip 使用 `Request Clipped`、`Zero-rate Clipped`、`Any Clipping`、
`Rate Limit Applied`、`Command Source`。`Any Clipping` 是两个候选物理限幅
标志的 OR，不能据此断言最终所选分支也被物理限幅。`Command Source` 的
`Request`、`Zero-rate`、`Previous Command` 分别表示请求分支、零温度变化率
分支及保持上一指令；`Zero-rate` 不等于保持夹套温度，也不保证夹套不升温。
这些标志只在 tooltip 中显示，不作为 °C 曲线绘制。

### 数据、缺口与失败语义

每周期仍只写一次原生 `controller_measurement`，附加固定类型 `diag_` 字段：

| 字段组 | 约定 |
|---|---|
| `diag_capture_ok` / `diag_refresh_applied` | bool；成功提交才可作为刷新完成证据 |
| `diag_snapshot_stage` / `diag_*_lifecycle` | string；字符串查询单独处理 |
| `diag_refresh_count`, `diag_segment_id`, `diag_segment_start_tick`, `diag_sample_count`, `diag_minimum_samples`, `diag_fit_count`, `diag_fit_failures` | int；数值查询在 group/pivot 前统一 float，避免计数与连续量碰撞 |
| `diag_main_*_measured/prior/state`, `diag_G_input/prior/state` | 实际计算位置的观测、预测和最终状态；没有计算的字段省略 |
| `diag_main_P_*`, `diag_G_P_*` 及各自 `_initial` | P 对角与对应初始化参考，原始物理单位的平方 |
| `diag_*_innovation_*` / `diag_*_S_*` | 真正校正前创新及创新协方差；不是测量减最终估计 |
| `diag_int_target/dT_dt/T`, `diag_dT_dt_request`, `diag_T_j_*` | 原始积分、实际求解请求和按顺序计算的夹套候选 |

#### 完整标量字段契约

下面逐名列出快照的 65 个固定类型字段：8 个 string、5 个 bool、7 个 int、
45 个 float。服务另写 `diag_capture_ok` 和失败时的 `diag_capture_error`，
共 67 个可能出现的诊断字段；并非每个周期都有全部字段。未执行的预测、
校正、求解或候选评估不写对应可选字段，查询显示 null，不补成 0/false。
温度标量存储单位为 K，面板显示 °C；G 存储为 m/s，面板显示 μm/s。
下表单位 `—` 表示状态/计数没有物理量纲，不表示字段值缺失。

| 字段 | 类型 | 存储单位 | 语义 |
|---|---|---|---|
| `diag_snapshot_stage` | string | — | `refresh_init` 为初始化边界；`posterior` 为普通周期最终快照，不意味着所有辅助滤波器都更新 |
| `diag_control_hold_reason` | string | — | 保持指令的实际原因，如 `controller_refresh`、`sigma_threshold` 或生长输入暂停原因；正常求解可为空字符串 |
| `diag_T_j_selected_branch` | string | — | `requested`、`zero` 或 `held`，只表示最终选中分支 |
| `diag_main_lifecycle` | string | — | 主 EKF 的 `refresh_init` 或 `updated` |
| `diag_G_lifecycle` | string | — | G 滤波器的 `refresh_init`、`updated`、`reanchored_updated` 或 `paused` |
| `diag_E_A_lifecycle` | string | — | 预留 E_A 滤波器的 `refresh_reserved` 或 `reserved`，不表示参与拟合 |
| `diag_k_0_lifecycle` | string | — | 预留 k_0 滤波器的 `refresh_reserved` 或 `reserved` |
| `diag_n_lifecycle` | string | — | 预留 n 滤波器的 `refresh_reserved` 或 `reserved` |
| `diag_refresh_applied` | bool | — | 当前成功提交的周期是否实际完成 Controller 刷新 |
| `diag_physical_limit` | bool | — | 所有实际评估候选物理限幅的 OR；无候选时兼容值 false 不作为限幅检查证据显示 |
| `diag_non_increasing_limit` | bool | — | 不升温约束是否选择零变化率分支；未评估候选周期不在图中显示该标志 |
| `diag_request_physical_limit` | bool | — | 请求分支 raw/bounded 是否不同，仅实际评估此分支时写入 |
| `diag_zero_physical_limit` | bool | — | 零变化率分支 raw/bounded 是否不同，仅实际评估此分支时写入 |
| `diag_refresh_count` | int | 次 | 当前实验累计完成刷新次数 |
| `diag_segment_id` | int | — | 当前拟合计算段编号 |
| `diag_segment_start_tick` | int | 周期 | 当前计算段起始周期，不是 UTC 时间戳 |
| `diag_sample_count` | int | 个 | 当前段中符合现有拟合条件的正 G 样本数量 |
| `diag_minimum_samples` | int | 个 | 当前配置的最低拟合样本数，不是已取得的样本数 |
| `diag_fit_count` | int | 次 | 当前实验累计成功拟合次数，刷新不清零此累计值 |
| `diag_fit_failures` | int | 次 | 当前实验累计拟合失败次数 |
| `diag_main_T_measured` | float | K | 当周期主 EKF 温度观测 |
| `diag_main_c_measured` | float | g/gH₂O | 当周期主 EKF 浓度观测 |
| `diag_main_T_prior` | float | K | 实际预测后、校正前的温度；刷新周期没有该预测则省略 |
| `diag_main_c_prior` | float | g/gH₂O | 实际预测后、校正前的浓度 |
| `diag_main_T_state` | float | K | 普通周期校正后的温度，或刷新瞬间的初始化温度 |
| `diag_main_dT_dt_state` | float | K/s | 主状态温度变化率；刷新初始化参考为 0 |
| `diag_main_c_state` | float | g/gH₂O | 普通周期校正后的浓度，或刷新瞬间的初始化浓度 |
| `diag_main_dc_dt_state` | float | (g/gH₂O)/s | 主状态浓度变化率；刷新初始化参考为 0 |
| `diag_G_input` | float | m/s | 当周期可用的 G 输入；不是初始化 G=0 |
| `diag_G_prior` | float | m/s | 本周期实际 G 预测值；没有进行 G 更新时省略 |
| `diag_G_state` | float | m/s | 本周期实际更新后的 G；刷新初始化/暂停不伪装成一次有效校正 |
| `diag_main_P_T` | float | K² | 主状态温度协方差对角项 |
| `diag_main_P_T_initial` | float | K² | 对应滤波器构造时的温度 P 初值 |
| `diag_main_P_dT_dt` | float | (K/s)² | 主状态温度变化率协方差对角项 |
| `diag_main_P_dT_dt_initial` | float | (K/s)² | 对应温度变化率 P 初值 |
| `diag_main_P_c` | float | (g/gH₂O)² | 主状态浓度协方差对角项 |
| `diag_main_P_c_initial` | float | (g/gH₂O)² | 对应浓度 P 初值 |
| `diag_main_P_dc_dt` | float | ((g/gH₂O)/s)² | 主状态浓度变化率协方差对角项 |
| `diag_main_P_dc_dt_initial` | float | ((g/gH₂O)/s)² | 对应浓度变化率 P 初值 |
| `diag_G_P_G` | float | (m/s)² | G 状态协方差对角项 |
| `diag_G_P_G_initial` | float | (m/s)² | 对应 G 状态 P 初值 |
| `diag_G_P_dG_dt` | float | (m/s²)² | G 一阶变化率状态协方差对角项 |
| `diag_G_P_dG_dt_initial` | float | (m/s²)² | 对应 G 一阶变化率 P 初值 |
| `diag_G_P_d2G_dt2` | float | (m/s³)² | G 二阶变化率状态协方差对角项 |
| `diag_G_P_d2G_dt2_initial` | float | (m/s³)² | 对应 G 二阶变化率 P 初值 |
| `diag_main_innovation_T` | float | K | 本次校正实际使用的温度创新，不是测量减最终状态 |
| `diag_main_innovation_c` | float | g/gH₂O | 本次校正实际使用的浓度创新 |
| `diag_main_S_T` | float | K² | 温度创新协方差对角项，供 innovation/√S 标准化 |
| `diag_main_S_c` | float | (g/gH₂O)² | 浓度创新协方差对角项 |
| `diag_G_innovation_G` | float | m/s | 本次 G 校正使用的首分量创新 |
| `diag_G_S_G` | float | (m/s)² | G 首分量创新协方差对角项 |
| `diag_int_target` | float | dimensionless·s | sigma 或 G 转为 sigma 等效误差后的控制积分 |
| `diag_int_dT_dt` | float | K | 温度变化率误差积分 |
| `diag_int_T` | float | K·s | 温度误差积分 |
| `diag_dT_dt_request` | float | K/s | 本周期实际 MPC/PI 求解结果；保持/刷新等未求解周期省略 |
| `diag_projected_T` | float | K | 按现有滞后补偿得到的前推温度，不是新增一次 EKF 预测 |
| `diag_projected_dT_dt` | float | K/s | 同一前推状态中的温度变化率 |
| `diag_projected_c` | float | g/gH₂O | 按现有滞后补偿得到的前推浓度 |
| `diag_projected_dc_dt` | float | (g/gH₂O)/s | 同一前推状态中的浓度变化率 |
| `diag_T_j_actual` | float | K | 当周期实际夹套温度测量 |
| `diag_T_j_selected` | float | K | 当前最终夹套指令，包括保持周期的已有指令 |
| `diag_T_j_candidate_raw` | float | K | 请求分支物理限幅前的夹套候选 |
| `diag_T_j_candidate_bounded` | float | K | 请求分支物理限幅后的夹套候选，不一定最终选中 |
| `diag_T_j_zero_raw` | float | K | 零变化率分支物理限幅前的夹套候选 |
| `diag_T_j_zero_bounded` | float | K | 零变化率分支物理限幅后的夹套候选，不一定最终选中 |

服务级捕获结果与上述快照字段分开：

| 字段 | 类型 | 存储单位 | 语义 |
|---|---|---|---|
| `diag_capture_ok` | bool | — | 本点是否附加通过身份、固定类型、有限数及原始结构检查的快照；不是独立的控制性能 PASS |
| `diag_capture_error` | string | — | 失败时写入受限原因：`unavailable`、`unsupported`、`invalid_output`、`capture_failed`、`invalid_snapshot`、`identity_mismatch` 或 `process_write_failed`；成功时不写 |

旧实验没有捕获字段不等同于一次捕获失败，页面只显示“未采集”。检查与失败标志
能够阻止已识别的无效快照作为证据发布，不保证发现所有语义错误；矩阵完整性、
数值一致性与控制输出不变仍须回归核验。`diag_projected_*` 和保持原因保存为
诊断证据但没有独立新增面板，本轮仍只有 11 个诊断面板。

夹套图显示六条温度曲线：请求分支 raw/bounded、零变化率分支 raw/bounded、
最终选中指令、实际夹套温度。多项 tooltip 中另外显示请求物理限幅、零变化率
分支物理限幅、任一已评估候选物理限幅（OR 汇总）、不升温限制及最终分支。
辅助状态值使用独立隐藏轴、零线宽和关闭点/填充，不画线、不占用 °C 纵轴，
也不在温度图例中显示。不能使用 `hideFrom.viz=true`：Grafana 10.4.3 的
tooltip 同时会排除这种系列，即使 `hideFrom.tooltip=false` 也不会显示。

`diag_T_j_zero_raw` 保存零变化率分支物理限幅前候选；
`diag_request_physical_limit` 和 `diag_zero_physical_limit` 分别对应各分支
raw/bounded 的差异。`diag_physical_limit` 是所有实际评估候选的 OR，不能
解释为最终选中分支的物理限幅标志。`diag_T_j_selected_branch` 为
`requested`、`zero` 或 `held`：分别表示请求分支、零变化率分支、保持原指令。
查询将这三个字符串映射成带名称的 tooltip 状态，不把枚举代码画成温度。
保持周期或未评估零变化率分支时，该分支 raw/bounded/限幅项留空，不能将
“没有计算”显示为“没有限幅”。旧实验缺失这些新增字段时不反向推断它们。
旧记录的 `diag_physical_limit` 曾只表示请求分支；查询仅在新增分支限幅字段
存在时展示 OR 汇总，旧实验该 tooltip 项留空，避免把历史旧语义解释为新语义。

查询保留真实 UTC、原始 tick 与 `acceptance_tick`，不聚合、不补零、不跨缺口
连线。旧实验、无效周期、捕获失败或未执行预测/校正的字段输出 null；没有
写入数据库的周期不能凭空重建。非正 P 不可对数显示，不替换成小正数，边界表
保留实际异常值。诊断失败不能使控制失败；设备/检查点失败或回滚不能发布
已完成刷新证据。数据库写失败按现有遥测处理，不撤销已提交的控制。

这些图证明“何时刷新、什么状态被重置、是否恢复计算”，不证明控制性能因此
改善。模拟 G 与模型 G 共用主滤波 T/c，曲线重合不是独立的滤波精度证据。
新仿真沿用 sigma→单次加种/E_A→同批 G/三参数→持续 G 的原阶段门槛、
原夹套下限和 120 仿真分钟上限；图表、刷新、控制跟踪及终点分别报告。
真实 GSensor、相机预热、FBRM 与硬件验证仍需另做。

只读查询回归（使用 `array.from`，不写测试点）：

```bash
MPCRYSTAL_FLUX_CHECK=1 .venv/bin/python -m pytest -q \
  tests/test_grafana_diagnostics.py tests/test_refresh_acceptance.py
```

## 当前已运行实验：只读桥接

本次运行中的 Controller 原本没有启用 InfluxDB。为避免中断实验，使用
`scripts/forward_controller_telemetry.py` 每秒读取其 `/api/status`，按原 tick 的
配置、计算时间戳和结果写入已有 `controller_measurement` 格式。它不发送
任何启动、停止、参数或设备命令，不改算法，不重放历史。

桥接只跟随启动时指定的一个数值仿真 run_id。实验变更或发现原生写入已开启时，
桥接自行退出，防止双重写入。只读轮询不能保证补齐断线期间的每个 tick；
遗漏计入 `missed_ticks`，不伪造数据。不支持用它重建图像输入字段。

状态：`.runtime/grafana-local/forwarder-status.json`；日志：`forwarder.log`。
接入之前未写入数据库的曲线不会凭空出现。此时 Controller 自身的
`influxdb.enabled` 仍为 false；它反映原进程原生写入，而不是桥接是否成功。

## 之后的新仿真：原生写入

配置成功后，原命令仍可使用：

```bash
.venv/bin/python scripts/run_fresh_simulation.py
```

脚本检测 `.runtime/grafana-local/controller.env` 后，自动为新 Controller
启用原生 InfluxDB 写入，不再需要只读桥接。OPC UA 读写仍关闭。
**该命令仍会删除以前受管理的本地仿真会话，但不删除 InfluxDB 历史数据。**
数据库卷与 `.runtime/manual-simulations/` 分开；需要删除数据库历史时必须另外明确授权。

## 服务管理

首次配置或复用现有部署（不会清空实验/数据库）：

```bash
.venv/bin/python scripts/configure_local_grafana.py
```

服务已存在且仅需重新启动时：

```bash
podman start mpcrystal-final-influxdb mpcrystal-final-grafana
```

停止可视化服务：`podman stop mpcrystal-final-grafana mpcrystal-final-influxdb`。
这不删除卷，但关闭期间新数据不能写入；先停止实验或接受数据缺口。
不要执行 `podman volume rm` / `down -v`，它们会删除持久数据。

| 服务 | 本机地址 | 持久卷 |
|---|---|---|
| InfluxDB | 127.0.0.1:8087 | mpcrystal-final-influxdb-data |
| Grafana | 127.0.0.1:3000 | mpcrystal-final-grafana-data |

凭据在被 Git 忽略的 `.runtime/grafana-local/` 中：目录 0700，文件 0600。
Controller 使用目标 bucket 的只写 token，Grafana 使用只读 token，初始化管理员
token 仅用于本机设置。容器间使用独立 `mpcrystal-observability` 网络。

配置方式参考 [Grafana provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/)
与 [InfluxDB container setup](https://docs.influxdata.com/influxdb/v2/install/use-docker-compose/)。

## 本次验证（2026-09-10）

- InfluxDB `/health`、Grafana `/api/health`、数据源 health：PASS。
- 在真实浏览器中看到当前实验的温度、浓度、sigma 曲线，刷新 5 秒：PASS。
- 当前 run `exp_20260910T143717_880Z_ff2751` 未重启，核验时已到 175 tick，
  计算错误和真实设备读写均为 0；桥接已写入 38 点，缺失 tick 0，写入错误无。
  这些数字是当时快照，运行过程中会继续增长。
- 新增配置/桥接测试、会话清理测试、Controller runtime service 测试合计
  **32 passed**。没有把历史 MATLAB 数值一致性问题计入此专项通过结果。
- 另用独立验证 run 实际运行 3 个数值 tick，Controller 原生写入成功 3 次，
  查询数据库得到 3 个 tick：PASS。随后只删除这次合成验证 run 的数据库点，
  没有删除用户实验数据。
- 本轮未运行清空会话的启动命令，未改动用户当前实验设置，未推送 GitHub。
