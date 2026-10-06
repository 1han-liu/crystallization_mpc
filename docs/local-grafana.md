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
`Samples / completed fits / failures` 仍在 Central 的拟合状态行，现有 Grafana
仪表盘没有新增这三个计数面板。真实拟合参数变化可查看 Growth parameters。

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
