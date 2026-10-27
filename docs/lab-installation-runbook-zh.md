# 结晶控制系统实验室上机流程

适用基线为 `final` 分支的 `d561aee`。本流程按 2026 年 10 月 13 日首次上机准备，主线采用 Windows 64 位电脑、PowerShell、Docker Desktop 的 Linux 容器。实验室系统、网络、OPC UA 地址和设备允许的控制范围需要现场填写；文中的示例地址不是已确认的设备地址。如果安装的是更新提交，先核对配置和迁移说明。

**已知现场条件：实验室电脑只有 Python 3.9，Docker Desktop 是否已安装仍待确认。按本手册的 Docker 主路线，保留 Python 3.9 即可，不需要先下载新版 Python 或 Miniconda。** 项目在容器中使用 Python 3.11。只有决定用 Windows 原生 Python 运行应用时，3.9 才不够，需要按第 14 节另外准备 3.11 环境。

完整顺序是：确认设备和电脑条件 → 获取代码 → 准备配置与依赖 → 构建镜像 → 启动基础服务 → 纯仿真 → OPC UA 只读联调 → 相机与 GSensor → 经现场确认后启动真实控制 → 结束实验并归档。不要把“网页能打开”当作“设备能控制”。

当前仓库记录的 2026 年 10 月 9 日四阶段数值验收中，阶段四跟踪要求失败，整组结果为 FAIL；物理设备验证也不能由离线数值测试替代。因此安装成功之后仍需完成只读及现场控制验证，不能据此直接认定默认参数可以稳定闭环。见 [四阶段结果](local-grafana.md) 和 [设备验证边界](controller-reference-validation.md)。

## 1 上机前准备

请先填写下表，未知项留待设备负责人确认。

| 项目 | 要确认的内容 |
|---|---|
| 电脑 | Windows 版本、64 位架构、是否允许启用虚拟化和 WSL 2、管理员协助 |
| 软件安装 | 已有 Python 3.9；确认 Docker Desktop 是否可用。Git 仅 Git 下载路线需要，ZIP 路线可跳过 |
| 外网 | GitHub、Docker Hub、Python 包索引、PyTorch CPU 索引、LoFTR 权重下载是否可达 |
| 设备服务 | OPC UA Server 是本机设备软件、另一台电脑还是设备自身；服务是否启动并获授权 |
| Endpoint | 完整 `opc.tcp://主机:端口/可能存在的路径`，本仓库默认端口为 `62552` |
| 身份与加密 | 匿名是否允许；是否必须账号、证书、Sign 或 SignAndEncrypt |
| 节点 | 5 个节点的真实 NodeId、namespace、读写权限、数值类型和单位 |
| 工艺 | 实际溶剂/溶质、浓度定义、温度边界、升降温速率、设备本地保护和急停方式 |
| 图像 | 相机软件能否持续保存图片、保存目录、文件名、分辨率和标定比例 |
| 数据 | 可写且空间充足的本地目录、备份位置、是否存在旧实验存档 |

### 1.1 先决定用哪条安装路线

| 现场情况 | 下一步 | 是否需要另装 Python |
|---|---|---|
| Docker Desktop 已安装且能运行 Linux 容器 | 检查 Client/Server 后，从第 2 节开始 | 不需要，保留 3.9 |
| 没有 Docker Desktop，但允许安装 | 按 1.2 节安装并启动，然后走 Docker 主线 | 不需要，保留 3.9 |
| 只有 Miniconda，没有 Docker | 仍要单独准备 Docker；Miniconda 不提供 Docker Engine | 走 Docker 主线仍不需要 |
| 现场决定让三个应用直接运行在 Windows | 使用第 14 节，并另外安排消息队列和数据库 | 需要，3.9 不满足项目最低要求 |

Docker Desktop 是在 Windows 上运行容器的软件；Python 是执行项目代码的解释器；Miniconda 是管理 Python 环境和包的工具。这三者可以共存，功能不同。**输入 `docker version` 只是检查已安装的 Docker，不会自动下载安装 Docker。**

导师所说“Docker 自己安装环境”，可以理解为 Docker 根据项目的 Dockerfile 准备容器内部环境。当前仓库的两份 Dockerfile 实际使用 `python:3.11-slim` 和 `pip install`，没有使用 Miniconda：先把 Docker 准备好，再执行 `docker compose build`，Python 3.11 和项目依赖就装进镜像里，不会把 Windows 原来的 Python 3.9 升级成 3.11。

完整 Docker 部署不要求宿主机另外安装 Python、Node.js、MATLAB 或 Visual Studio。前端是静态 HTML/JS，没有 `npm install`、`npm run build` 这一步。相机厂商软件、驱动和 OPC UA Server 属于现场设备环境，`docker compose` 不会替你安装它们。

### 1.2 检查和安装 Docker Desktop

先在普通 PowerShell 中检查：

```powershell
docker version
docker compose version
```

若两条命令都可用，且第一条同时显示 Client 和 Server，继续后面的 Linux 容器检查。若只有 Client、Server 报连接错误，先从开始菜单打开 Docker Desktop，等待引擎启动，再试一次。

**若提示“无法将 docker 识别为命令”或电脑上根本没有 Docker Desktop：**

1. 在 Windows 设置的“系统 → 系统信息/关于”确认系统版本和处理器架构，并在任务管理器“性能 → CPU”查看虚拟化是否启用。
2. 打开 [Docker Desktop Windows 官方安装页](https://docs.docker.com/desktop/setup/install/windows-install/)，核对当前系统要求，下载匹配架构的安装程序。普通 Intel/AMD 64 位电脑选 x86_64/AMD64；ARM 电脑不要照选。本仓库依赖能否在 ARM 安装需另外验证。
3. 运行安装程序，按提示使用 WSL 2 后端。如果需要管理员权限、启用虚拟化或重启，由现场管理员协助完成。不要因安装受限而先在 Python 3.9 里尝试装项目包。
4. 若提示缺少或需要更新 WSL，由管理员依照安装页处理。新安装通常使用管理员 PowerShell 的 `wsl --install`；已经安装时按需要使用 `wsl --update`，然后完成要求的重启。这些是系统安装操作，不是本项目的 pip 命令。
5. 从开始菜单打开 Docker Desktop，等待引擎就绪，使用 Linux containers。关闭旧 PowerShell，重新打开后再检查 Docker。

安装后执行：

```powershell
wsl --version
docker version
docker compose version
docker info --format '{{.OSType}}'
```

成功标志：Compose 能输出版本；`docker version` 同时有 Client、Server；最后一项输出 `linux`。只看到 Client 不算通过，最后一项输出 `windows` 时应先切换 Linux containers。安装后仍找不到命令，先确认 Desktop 已安装并重开终端，再检查 PATH；不要安装同名 pip 包替代 Docker。系统不受支持、虚拟化被禁用或引擎无法启动时，先解决这一层，再构建项目。[Docker Windows 安装说明](https://docs.docker.com/desktop/setup/install/windows-install/)

内存和磁盘要留出图像、模型、镜像和数据库增长的余量；建议准备 16 GB 内存和至少数十 GB 空闲磁盘作为部署预算，并以实际负载观察为准。

实验室不能联网时，应提前在相同架构的电脑构建镜像并导出，见第 13 节。不要把“明天到现场再下载全部模型”作为唯一方案。

## 2 获取代码：GitHub 下载 ZIP 或 Git 克隆，二选一

### 2.1 直接从 GitHub 页面下载 ZIP

这条路线不需要安装 Git，也不需要执行 `git clone`。

1. 打开 [仓库的 final 分支](https://github.com/1han-liu/crystallization_mpc/tree/final)，确认左上角分支显示 `final`。
2. 记录这次下载对应的最新提交号和下载时间。为了固定版本，可先点击该提交进入提交详情，再点击 **Browse files**，从该提交的文件页面下载。
3. 点击绿色 **Code → Download ZIP**，等待下载完成。在文件资源管理器中右键 ZIP → **全部解压**，例如解压到 `C:\Lab`。
4. 找到真正包含 `docker-compose.yml`、`README.md`、`src`、`services` 的文件夹。按分支下载时通常名为 `crystallization_mpc-final`；按提交下载时名字可能带提交号。不要留在 ZIP 内，也不要停在多套一层的外部文件夹。
5. 打开 PowerShell，进入该文件夹。下面按分支 ZIP 的默认名称举例，名称不同时替换路径：

```powershell
Set-Location C:\Lab\crystallization_mpc-final
Test-Path -LiteralPath docker-compose.yml
Test-Path -LiteralPath .env.example
```

两个检查都应输出 `True`。然后跳到第 3 节。后面的 Docker 命令与 Git 克隆路线相同；`.env` 和数据库配置仍需现场创建。

ZIP 通常没有 `.git`，所以不能执行 `git pull` 或 `git rev-parse HEAD`。以后更新时，应在实验结束后把新 ZIP 解压到新的目录，再核对并迁移现场配置；不要直接覆盖正在运行的目录，也不要复制别人的 `.venv` 或旧运行状态。归档时使用已记录的下载提交号，见第 12 节。

### 2.2 使用 Git 克隆和更新

如果希望之后用 Git 更新，先确认 `git --version` 能输出版本；没有 Git 时可从 [Git for Windows](https://git-scm.com/downloads/win) 安装，或直接选上面的 ZIP 路线。首次克隆，在 PowerShell 执行：

```powershell
New-Item -ItemType Directory -Force C:\Lab | Out-Null
Set-Location C:\Lab
git clone --branch final --single-branch https://github.com/1han-liu/crystallization_mpc.git
Set-Location C:\Lab\crystallization_mpc
git status --short --branch
git log -1 --oneline
```

已有仓库且当前实验已结束时：

```powershell
Set-Location C:\Lab\crystallization_mpc
git status --short --branch
git fetch origin
git switch final
git merge --ff-only origin/final
git log -1 --oneline
```

本机当前基线是 `d561aee`；现场最新提交可能不同。记录真正安装的提交号，不要假定分支名代表固定版本。工作区有修改、未跟踪文件冲突或分支分叉时，先保留现场配置和改动，处理清楚后再合并；不要用 `reset --hard` 或 `clean` 排障。

若出现连接 `127.0.0.1` 代理失败，检查是不是复制了另一台电脑的本地代理设置。仅在允许直连、且不需要单位代理时，可以对单次命令临时禁用 Git 代理：

```powershell
git -c http.proxy= -c https.proxy= fetch origin
```

首次克隆失败时，对 `clone` 使用同样的 `-c` 参数。认证失败应使用你的仓库访问权限，不要把 token 写入教程、URL 或截图；TLS 证书错误应处理证书链或代理，不要关闭 SSL 验证。

## 3 创建现场配置和独立数据目录

下面及后续 Docker 命令均在项目根目录执行。

```powershell
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
if (-not (Test-Path -LiteralPath config\influxdb.env)) {
    Copy-Item -LiteralPath config\influxdb.env.example -Destination config\influxdb.env
}
$preflightRoot = 'C:\MPCData\session-20261013-preflight'
if (Test-Path -LiteralPath $preflightRoot) { throw 'Choose a new session directory; do not reuse old state' }
New-Item -ItemType Directory -Force C:\MPCData | Out-Null
New-Item -ItemType Directory -Path $preflightRoot | Out-Null
notepad .env
notepad config\influxdb.env
```

`.env` 和 `config/influxdb.env` 不随 Git 分发。尤其第二个文件缺失时，Compose 会在启动前报错；新克隆后直接执行 README 的一条启动命令并不完整。已有文件只修改所需项，不要用模板覆盖已配置的数据库凭据。

本流程中“新会话目录”都要求确实不存在。出现上述目录已存在提示时停止后续步骤，换一个新的后缀，并同步修改 `.env` 路径；不要跳过检查。旧目录里的 RUNNING 状态可能在服务启动时恢复。

在根目录 `.env` 中设置以下内容，其他配置先保留模板值：

```dotenv
UI_MODE=development
EXPERIMENT_HOST_ROOT=C:/MPCData/session-20261013-preflight
CONTROLLER_ADAPTER=crystallization_mpc.apps.controller.algorithm.integration:CrystallizationControllerAdapter
CONTROLLER_OPCUA_ENABLED=false
CONTROLLER_OPCUA_WRITE_ENABLED=false
CONTROLLER_INFLUX_ENABLED=true
CONTROLLER_INFLUX_URL=http://influxdb:8086
GSENSOR_HOUGH_DEBUG_ENABLED=false
```

含义：

- `development` 让仿真来源选项可见，供安装预检；现场正式使用再切 `production`。
- `EXPERIMENT_HOST_ROOT` 是这次会话的总目录。Windows Docker 路径使用正斜杠，避免网盘同步目录和暂时不稳定的网络盘。
- `CONTROLLER_ADAPTER` 必须填完整。默认空值会使用 NoOp，不计算真实控制结果；服务在线不代表算法已启用。
- 两个 OPC UA 开关都为 `false` 时，本阶段不接设备。
- `influxdb:8086` 是容器之间的地址，不是宿主机浏览器地址。

在 `config/influxdb.env` 中，首次初始化前设置自己的数据库管理密码和随机 token：

| 配置 | 必须保持的关系 |
|---|---|
| `DOCKER_INFLUXDB_INIT_PASSWORD` | 使用符合 InfluxDB 要求的强密码，替换 `change-me` |
| `DOCKER_INFLUXDB_INIT_ADMIN_TOKEN` 与 `INFLUX_TOKEN` | 两项填写相同的、足够长的随机 token |
| `DOCKER_INFLUXDB_INIT_ORG` 与 `INFLUX_ORG` | 一致，例如模板的 `crystallization-lab` |
| `DOCKER_INFLUXDB_INIT_BUCKET` 与 `INFLUX_BUCKET` | 一致，例如模板的 `crystallization` |

凭据保存在本机私有配置，不要提交 Git。默认 Compose 把多个端口发布到宿主机接口；应在实验室受控网络使用，并由现场网络策略限制访问，尤其不要把默认 RabbitMQ 开发账号暴露到公网。

先做配置语法检查：

```powershell
docker compose config --quiet
```

成功标志：无报错且退出码为 0。不要把不带 `--quiet` 的完整配置输出发给别人，它可能展开 token。若配置显示与文件不一致，检查当前 PowerShell 是否已有同名 `$env:` 环境变量，它们可能覆盖 `.env`。

**数据库初始化陷阱：** `DOCKER_INFLUXDB_INIT_*` 用于首次初始化空数据卷。已有数据库后只改 env 密码/token，不会自动修改数据库内已有账户，容易造成 401。应恢复一致的凭据或通过数据库的管理流程修改，不能靠删除数据卷解决有价值数据的问题。

## 4 安装依赖和构建镜像

主路线直接执行：

```powershell
docker compose pull rabbitmq influxdb grafana
docker compose build central gsensor
```

这里安装了什么：

| 镜像 | 内容 |
|---|---|
| `crystallization-mpc-app:local` | Python 3.11、基础依赖、Central 和 Controller 代码 |
| `crystallization-mpc-gsensor:local` | 基础依赖、Torch 2.14.0、Kornia 0.8.3、LoFTR 权重及 GSensor |
| RabbitMQ、InfluxDB、Grafana | 消息队列、时序数据库、曲线与诊断面板 |

基础包来自 `requirements.txt`，包含 FastAPI/Uvicorn、asyncua、NumPy/SciPy、OpenCV、ONNX Runtime、Pika、InfluxDB client 等。`central` 和 `controller` 共用镜像，构建 `central` 已为 Controller 准备镜像；不是三个应用都要单独 `build`。镜像复制了运行代码和默认参数，图像中的 ONNX 模型已跟踪在 Git 中。

GSensor 构建可能耗时更长：不仅安装 Torch/Kornia，还会下载 LoFTR outdoor 权重并校验 SHA256。即使现场暂时选 `alignment_method=none`，当前完整 GSensor Dockerfile 仍执行这一步。不要在现场随意去掉版本或哈希校验来掩盖下载失败。

常见构建错误：

| 错误 | 处理 |
|---|---|
| Docker daemon 不可达 | 先启动 Desktop，确认 Linux 后端与 `docker version` 的 Server |
| `env file ... not found` | 返回第 3 节复制并编辑 `config/influxdb.env` |
| 下载超时、DNS、代理失败 | 区分 Docker 拉镜像和镜像内 pip/权重下载；配置可用网络或使用预构建离线镜像 |
| `No matching distribution found` | 检查架构、Python 版本和可访问的包索引；优先与仓库 Python 3.11 镜像一致，不随意改固定依赖 |
| LoFTR 下载或哈希失败 | 保留原始错误，核对网络、缓存与上游资产，重新构建；不要接受不明权重 |
| 磁盘不足、内存不足、退出 137 | 释放无关文件或增加 Docker 资源；不要清理实验数据和数据库卷 |

构建成功后可检查依赖：

```powershell
docker compose run --rm --no-deps controller python --version
docker compose run --rm --no-deps gsensor python --version
docker compose run --rm --no-deps controller python -m pip check
docker compose run --rm --no-deps gsensor python -m pip check
docker compose run --rm --no-deps gsensor python -c "import torch, kornia, onnxruntime, cv2; print(torch.__version__, kornia.__version__)"
```

这些命令覆盖容器默认启动命令，不启动实验或写设备。成功标志是两个容器都显示 `Python 3.11.x`，`pip check` 不报告冲突，GSensor import 成功。此时 Windows 直接输入 `python --version` 仍显示 `3.9.x` 完全正常：前面检查的是容器，后者检查的是宿主机。

基础依赖大多是版本范围；记录镜像内实际版本有助于重现，不要认为同一 Git 提交在未来重新下载依赖一定得到相同环境。

## 5 分阶段启动服务

先启动数据库和消息队列：

```powershell
docker compose up -d rabbitmq influxdb
docker compose ps
docker compose exec rabbitmq rabbitmq-diagnostics -q ping
docker compose exec influxdb influx ping -host http://localhost:8086
```

等到 RabbitMQ 和 InfluxDB 的检查成功，再启动应用和面板：

```powershell
docker compose up -d controller gsensor central grafana
docker compose ps
docker compose logs --tail 100 controller central gsensor
```

当前 Compose 的 `depends_on` 只定义启动先后，没有要求依赖先变为健康状态。因此首次启动若应用因基础服务未就绪报连接错误，先确认基础服务已好，再重新启动失败的应用；不要不断同时重启全部服务。

| 用途 | 宿主机打开或连接 | Docker 网络内部 |
|---|---|---|
| Central 主界面 | `http://127.0.0.1:8000` | `central:8000` |
| GSensor 界面 | `http://127.0.0.1:8001` | `gsensor:8001` |
| Controller 状态 | `http://127.0.0.1:8002/api/status` | `controller:8002` |
| RabbitMQ AMQP | `127.0.0.1:5673` | `rabbitmq:5672` |
| RabbitMQ 管理界面 | `http://127.0.0.1:15673` | `rabbitmq:15672` |
| InfluxDB | `http://127.0.0.1:8087` | `influxdb:8086` |
| Grafana | `http://127.0.0.1:3000` | `grafana:3000` |

注意 `8002` 不是 GSensor，`5673` 不是 RabbitMQ 容器内端口，`8087` 不是 InfluxDB 容器内端口。

管理页面登录：这份 Compose 的全新 RabbitMQ 开发实例使用 `guest/guest`；InfluxDB 使用第 3 节配置的管理用户和密码。全新且未另设管理员凭据的 Grafana 初次使用 `admin/admin`，登录后按提示改密码；已有数据卷使用原账户，不会因重启重置。仓库另一个本地演示脚本配置的免登录 Viewer，不是完整 Compose 的默认行为。[Grafana 首次登录说明](https://grafana.com/docs/grafana/latest/setup-grafana/sign-in-to-grafana/)

检查应用状态：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/system/status | ConvertTo-Json -Depth 8
Invoke-RestMethod http://127.0.0.1:8001/api/status | ConvertTo-Json -Depth 8
Invoke-RestMethod http://127.0.0.1:8002/api/status | ConvertTo-Json -Depth 8
```

服务未创建实验时不要求 `running`。重点是没有持续报错，Controller 的 `adapter.safe_noop` 为 `false`，OPC UA 两个开关仍为 `false`。Controller 的 HTTP healthcheck 只验证状态接口可访问，不证明 RabbitMQ、设备或算法运行成功。

若出现端口占用，查看占用者而不是立即杀进程：

```powershell
Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in 8000,8001,8002,5673,15673,8087,3000 } | Select-Object LocalAddress,LocalPort,OwningProcess
# 用上一条实际得到的 PID 查询进程，例如：
Get-Process -Id 1234
```

确认是旧服务时先正常结束其工作；若改 Compose 的宿主机映射端口，后续浏览器地址、本机脚本和审计地址也要一起修改。容器内部服务互联通常仍保留原来的内部端口。

## 6 先完成纯仿真预检

保持 OPC UA 两门关闭，并在 Controller 状态中核实。**当前写入判断没有额外检查 UI 的 Run Type；仅把界面切到 Simulation 不能阻止设备读写。仿真必须把两个 OPC UA 环境开关都设为 false，并让新配置实际生效。** 在 Central 刷新页面，按下列顺序操作：

1. Run Type 选 `Simulation`。
2. Growth-rate Source 选 `Simulated`。
3. Controller Mode 选 `MPC`，Control Target 选 `sigma`，Adaptation 先关闭。
4. 填写 Experiment Label，点击 `Create Experiment`；打开左侧 `Parameters`，检查参数并 `Save Parameters`，再点击 `Start Experiment`。
5. 打开 Controller 状态，确认当前 run 相同、`status=running`、`scheduler.tick_count` 持续增加、`last_control_output` 有结果。
6. 查看 Grafana 中对应 run 的曲线及当前时间范围；有数据证明相应遥测链路可用，不代表控制性能已经验收。
7. 点击 `End Experiment`，确认 Controller 变成 `stopped`，再进行后续修改。

看不到 Simulated 时检查 `UI_MODE=development` 是否实际进入 Central 容器。修改 `.env` 后要重建容器配置，不是仅刷新浏览器：

```powershell
# 只在实验已结束的情况下执行。
docker compose up -d --force-recreate central gsensor controller
```

`docker compose restart` 不会把修改后的环境变量注入旧容器。代码变化还需要先 `docker compose build central gsensor`；挂载的图表和实验目录又是另一类文件，不要混淆。

这一步不需要相机图片。不要运行 `scripts/run_fresh_simulation.py` 作为 Windows 主流程：它使用 Linux 的 `fcntl`，且会永久清理其管理的已结束仿真会话。新克隆也没有完整 `tests/` 和 MATLAB 对照数据，不应把“现场 pytest 找不到测试”误判为安装失败。

## 7 配置 OPC UA 地址并排查端口

### 7.1 确认谁是服务端

本项目 Controller 是 OPC UA 客户端。设备软件或设备自身是服务端，必须由现场启动。Compose 没有提供设备 OPC UA Server。

| 实际拓扑 | `CONTROLLER_OPCUA_ENDPOINT` 应如何填 |
|---|---|
| Controller 在 Docker，Server 在同一台 Windows | `opc.tcp://host.docker.internal:实际端口/实际路径` |
| Controller 在 Docker，Server 在另一台实验室电脑或设备 | `opc.tcp://服务器局域网IP:实际端口/实际路径` |
| Controller 原生 Python，Server 在同一台 Windows | `opc.tcp://127.0.0.1:实际端口/实际路径` |
| Controller 原生 Python，Server 在另一台机器 | `opc.tcp://服务器局域网IP:实际端口/实际路径` |

容器内的 `localhost` 指向当前容器。Docker Desktop 提供的 `host.docker.internal` 用于访问宿主机；它不代表另一台设备。原生 Linux Docker Engine 对该名称的支持与 Desktop 不同，应配置 host gateway 或使用实际可达地址。[Docker 宿主机网络说明](https://docs.docker.com/desktop/features/networking/networking-how-tos/)

仓库默认是 `62552`，不能因为很多 OPC UA 教程用 `4840` 就照抄。端口和 endpoint 路径以现场 Server 实际公布的配置为准。Controller 是主动向外连接，不需要在它的 Compose `ports` 中添加 `62552:62552` 来解决连接问题。

### 7.2 从 Windows 测试 TCP

先填现场地址；下面 IP 仅是语法示例，不是设备地址：

```powershell
$OpcServer = '192.168.10.20'
$OpcPort = 62552
Test-NetConnection -ComputerName $OpcServer -Port $OpcPort -InformationLevel Detailed
```

`TcpTestSucceeded=True` 说明 TCP 通路建立，不证明 OPC UA 会话、节点或权限正确。Ping 失败但 TCP 成功仍可继续，ICMP 可能被限制。[Microsoft TCP 检查说明](https://learn.microsoft.com/en-us/powershell/module/nettcpip/test-netconnection?view=windowsserver2025-ps)

如果为 `False`，依次检查：网线/网卡 → IP 和掩码 → 是否跨 VLAN/网段 → 路由/VPN → Server 是否运行并监听 → Server 入站防火墙 → 客户端出站策略。不要随意更改设备固定 IP；先取得实验室网络规划。可用 `ipconfig /all`、`Get-NetIPConfiguration` 和 `route print` 检查客户端网络。

**在 OPC UA 服务器所在 Windows 上**检查监听，不是在任意客户端上执行：

```powershell
Get-NetTCPConnection -State Listen -LocalPort 62552 | Select-Object LocalAddress,LocalPort,OwningProcess
```

没有监听通常是服务未启动或端口不对。只监听 `127.0.0.1` 时，远端电脑无法连接；Docker 到宿主机是否可达也必须实测。需要由设备服务配置允许正确接口访问，而不是盲目增加防火墙规则。

若确认是防火墙拦截，由服务器管理员在认可的网络配置下添加针对实际来源的规则。以下仅适用于服务器的 Domain/Private 网络且来源确实为该 IP：

```powershell
# 在 OPC UA Server 所在 Windows 的管理员 PowerShell 执行，替换真实客户端来源 IP。
New-NetFirewallRule -DisplayName 'MPC OPC UA from lab client' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 62552 -RemoteAddress 192.168.10.30 -Profile Domain,Private
```

如果实际网络配置为 Public 或受域策略管理，交由现场管理员处理；不要关闭全部防火墙。Docker Desktop 的流量可能以 Desktop 后端/虚拟网络对应的来源出现，应依据服务器日志确定被允许的实际来源。[Microsoft 防火墙规则说明](https://learn.microsoft.com/en-us/powershell/module/netsecurity/new-netfirewallrule?view=windowsdefender)

### 7.3 从 Controller 容器测试 TCP

结束仿真实验后，编辑 `.env` 中真实 endpoint，仍保持写门 `false`。以下命令创建一次性容器，只测试 TCP，不启动 Controller 服务：

```powershell
docker compose run --rm --no-deps controller python -c "import os,socket; from urllib.parse import urlparse; u=urlparse(os.environ['CONTROLLER_OPCUA_ENDPOINT']); s=socket.create_connection((u.hostname,u.port),timeout=5); print('TCP OK'); s.close()"
```

Windows 测试成功、此处失败时，重点排查 Docker 内 DNS、endpoint 是否误填 `localhost`、Server 监听接口、Docker Desktop 后端防火墙和 VPN 路由。增加应用超时不能修复错误地址或未监听端口。

### 7.4 会话与安全策略

TCP 通了但出现 `BadSecurityPolicyRejected`、`BadSecurityChecksFailed`、证书不可信、`BadIdentityTokenRejected` 或 `BadUserAccessDenied`，进入的是 OPC UA 层的问题。用现场已有的 OPC UA 客户端查看 Server 的 endpoint、安全策略和身份要求，再与应用能力比较。

**本版本的重要边界：** 项目配置没有独立的 OPC UA 用户名、密码、客户端证书和安全策略入口；默认工厂直接构造 `asyncua.sync.Client(endpoint, timeout=...)`。如果现场强制认证或加密，需要补齐应用的安全连接支持并验证后再继续，不能把它当成单纯“开放端口”问题。不要通过关闭服务器要求来绕过，也不要把账号密码塞进 endpoint：endpoint 会出现在状态接口和诊断材料中。asyncua 本身具有认证和证书接口，但本项目目前没有完整接入它们。[asyncua 客户端接口](https://opcua-asyncio.readthedocs.io/en/latest/api/asyncua.client.html)

## 8 核对 OPC UA 节点和数据

在 `.env` 中根据现场 NodeId 核对这些项：

| 变量 | 当前默认字符串 | 作用 |
|---|---|---|
| `CONTROLLER_OPCUA_NAMESPACE_INDEX` | `2` | 所有节点共同使用的 namespace index |
| `CONTROLLER_OPCUA_T_NODE` | `Local.iControl.Experiment1.Trends.Tr.Value` | 反应器温度 |
| `CONTROLLER_OPCUA_T_J_NODE` | `Local.iControl.Experiment1.Trends.Tj.Value` | 实际夹套温度 |
| `CONTROLLER_OPCUA_C_NODE` | `Local.iControl.Experiment1.Trends.load.Value` | 浓度或 load 标量 |
| `CONTROLLER_OPCUA_COUNT_MIDDLE_NODE` | `Local.iControl.Experiment1.Trends.count.Value` | count 标量 |
| `CONTROLLER_OPCUA_T_J_SET_NODE` | `iCache.Temperature1` | 夹套设定值，读写目标 |

代码将节点拼成 `ns=2;s=上述字符串`。因此 env 中只填 `s=` 后面的字符串，不要填完整 `ns=2;s=...`，也不要填 DisplayName 或浏览路径。当前实现只支持统一 namespace 下的字符串 NodeId；若实际为数值 `i=`、GUID 或节点分属不同 namespace，需要改代码支持，单改这些字符串不够。

即使只读，应用也要求能读取设定值节点的类型和值；“温度能读，但设定值读不了”仍会失败。`T_j` 必须是实际夹套测量值，不能拿设定值节点代替来消除错误。

下面是在容器环境中执行的只读探测，逐个显示原始值、质量、时间戳和类型；它不调用任何写接口。运行前核对 `.env` 中 endpoint，并保持 `CONTROLLER_OPCUA_WRITE_ENABLED=false`。

```powershell
@'
from asyncua.sync import Client
from crystallization_mpc.apps.controller.config import ControllerSettings
from crystallization_mpc.apps.controller.process import OpcUaNodeConfig

s = ControllerSettings.from_env()
assert not s.opcua_write_enabled, "Keep the write gate disabled during preflight"
nodes = OpcUaNodeConfig(
    namespace_index=s.opcua_namespace_index,
    T=s.opcua_t_node,
    T_j=s.opcua_t_j_node,
    c=s.opcua_c_node,
    count_middle=s.opcua_count_middle_node,
    T_j_set=s.opcua_t_j_set_node,
)
with Client(s.opcua_endpoint, timeout=s.opcua_timeout_s) as client:
    print("Namespaces:", client.get_namespace_array())
    for name, node_id in nodes.node_ids().items():
        node = client.get_node(node_id)
        data = node.read_data_value()
        data.StatusCode.check()
        print(name, node_id, "value=", data.Value.Value,
              "type=", node.read_data_type_as_variant_type(),
              "quality=", data.StatusCode,
              "source=", data.SourceTimestamp,
              "server=", data.ServerTimestamp)
'@ | docker compose run --rm --no-deps -T controller python -
```

成功标志是 5 个节点均可读、质量正常、数值合理，并且随真实设备变化。多读几次并与设备面板逐项对照。

**温度单位是现场最关键的核对项。** 当前代码假定 OPC UA 原始温度是摄氏度：读入时加 `273.15` 转成 K；写回设定值时减 `273.15` 转成摄氏度。例如设备显示 25°C，原始节点应约为 25，Controller 状态中的 T 应约为 298.15。如果 Server 已提供 K、十分之一摄氏度或其他标度，必须修改并验证换算，不能直接运行。

浓度和 count 在接口层不转换单位；控制算法期望浓度符合其 g/gH2O 约定。现场 `load` 如果是百分数或其他含义，要先明确换算。`resolution` 是图像米/像素比例，不能直接使用与现场光学配置无关的标定值。

代码记录 OPC UA 源时间戳，但没有统一的“测量超过多少秒必须拒绝”的校验。数值能读且质量 Good，也可能是服务器缓存旧值；需检查时间戳、设备运行状态和变化响应，不能只看 API 是否报错。

## 9 运行真实设备的只读联调

在已完成节点核对、设备由其本地系统正常运行的条件下进行。先在 Central 结束仿真，确认 Controller `stopped`。为真实只读联调使用新的会话总目录：

```powershell
$shadowRoot = 'C:\MPCData\session-20261013-shadow'
if (Test-Path -LiteralPath $shadowRoot) { throw 'Choose a new session directory; do not reuse old state' }
New-Item -ItemType Directory -Path $shadowRoot | Out-Null
notepad .env
```

修改：

```dotenv
UI_MODE=production
EXPERIMENT_HOST_ROOT=C:/MPCData/session-20261013-shadow
CONTROLLER_OPCUA_ENABLED=true
CONTROLLER_OPCUA_WRITE_ENABLED=false
```

同时保留实际 endpoint、正确节点及算法 adapter。在 `.env` 将 `CONTROLLER_OPCUA_JACKET_MIN_K`、`CONTROLLER_OPCUA_JACKET_MAX_K` 设置为现场认可的范围，使用 K。模板的 253.15–453.15 K 即 −20–180°C，仅是宽泛默认值，不代表你的装置允许这个范围。

```powershell
docker compose up -d --force-recreate central gsensor controller
```

所有三个应用必须挂载同一新目录。刷新 Central，确认旧 run 没有自动继续。**换总目录会切换参数文件来源，之前在旧目录保存的参数不会自动带入。** 依次选 `Experiment`、`Live GSensor`、`MPC`、`sigma`、Adaptation 关闭，填写标签并创建新实验；在 Parameters 中重新核对全部现场参数，将 `T_j_min`、`T_j_max` 设为与 OPC UA 边界一致的范围，保存后再 `Start Experiment`。UI 输入单位以字段标签为准，不把 env 的 K 数值直接填入标为 °C 的字段。

普通空闲启动不一定建立 OPC UA 连接；正常新实验在 Start 时读取初始过程值，并在每个控制周期继续读取。启动失败看 Controller 日志和 `integrations.opcua.service_last_error`，不要只看网页上一个概括提示。

```powershell
$status = Invoke-RestMethod http://127.0.0.1:8002/api/status
$status.status
$status.scheduler
$status.integrations.opcua | ConvertTo-Json -Depth 8
$status.last_control_output | ConvertTo-Json -Depth 8
```

应确认：`status=running`、run 一致、`connected=true`、`shadow_mode=true`、`write_enabled=false`、`read_count` 和 tick 持续增长。`write_count=0`，输出中的 `process_write_attempted=false`。缺少有效 G 时不要切 G 目标或开启依赖它的适应；现场投晶种必须按物理操作实际记录，不能为通过检查伪造事件。

`sigma + Adaptation Disabled` 的只读联调不要求先投晶种或初始化 GSensor。如果初始工况触发算法的低过饱和度分支，最初可能没有可审计的控制输出；应在工艺允许且出现连续输出后重新采集，不能为了让审计通过而随意加种晶或改参数。

采集仓库提供的 8 个连续控制周期只读证据，保存到宿主机挂载目录：

```powershell
$stamp = Get-Date -Format yyyyMMdd-HHmmss
docker compose exec -T controller python -m crystallization_mpc.apps.controller.shadow_audit --status-url http://127.0.0.1:8002/api/status --ticks 8 --timeout 180 --output /data/experiments/artifacts/controller-shadow-$stamp.json
```

成功提示为 `shadow audit passed`。默认周期 5 秒，采集需要等未来若干周期；周期调大时相应增加 timeout。此工具不会帮你 Start，也不直接访问或写 OPC UA。失败应检查运行状态、算法是否 NoOp、连续 tick、候选值和现场温度边界，而不是取消检查。通过仅证明这一段真实只读运行符合该审计条件，不代表真实写入和全部物理安全已验证。

审计允许记录 `solver.status=invalid` 的周期，因此即使 PASS，也要检查每条记录的 `solver.status`、`failure_reason` 和候选输出；PASS 不等于每个控制周期求解成功，更不是控制性能或稳定性合格。

## 10 相机和 GSensor 现场流程

GSensor 从共享目录扫描图片，不负责安装相机驱动或直接操纵相机。先用厂商软件确认相机有正常图像，然后让它保存到 Central 当前实验显示的 `images` 目录。主机目录和容器目录不同，但必须指向同一挂载内的数据。

例如主机总目录为 `C:\MPCData\session-20261013-shadow`，Central 创建具体 run 后，通过 `Camera Save Path → Copy Path` 取得实际主机图片路径，不要自行猜测 run 名；容器内对应 `/data/experiments/.../images`。先放一张真实可解码的测试图像，确认两边都看得到。

```powershell
docker compose exec gsensor python -c "from pathlib import Path; p=Path('/data/experiments'); print([x.name for x in p.iterdir()])"
```

操作顺序：

1. Central 已 `Start Experiment`，再点击单独的 `Enable GSensor`；Start 本身不自动启用 GSensor。
2. 确认最新、已写完的图片到达正确 run 的目录。
3. 打开 GSensor 页面，先选晶体 Full mode Yes/No，依次按 Step 指示标注 u/v 边上的 t/e/o 点及对应的 opposite/w 边，再按提示标 kernel corner/outer point，选 corner A/B/C 与 3D candidate，核对预览和 Image Alignment，最后点击 `Confirm selected candidate`。具体步骤随 Full mode 分支变化，以当前提示为准。基线图片在初始化期间冻结；不要把页面显示旧基线误认为相机停止。
4. 保持相机继续产出图片，确认 GSensor 转入测量状态，并出现新的有效帧。
5. Central 和 Controller 的 GSensor 状态、run、版本及 `growth_input.available` 要相互对应，再考虑 G 目标或适应。

首日对齐方式优先使用现场已验证的配置。`none` 不需要 LoFTR，但不意味着所有图像条件都满足测量要求。确认后的对齐切换、重新标注可能重建测量段；不要在闭环首次联调时同时改变多项条件。

图像相关常见故障：目录不一致、相机写入上一场 run、文件尚未写完、文件名/序号不符合解析规则、时间或序号重置、图片无法解码、标定比例不对、晶体不完整或检测不可靠。检查 GSensor `image_scan` 和页面具体提示，再决定是否确认序号重置或重新初始化；不要只改文件名来掩盖旧帧问题。

文件放在 `images` 当前层，扫描不递归子目录。支持 PNG、JPG/JPEG、BMP、TIF/TIFF；推荐 `IMG_00001.PNG`、`IMG_00002.PNG` 单一递增序列，解析使用扩展名前最后一串数字。默认每秒扫描，大小和修改时间连续两次稳定且可完整解码后才接纳，短暂 Pending 正常。相机序号从头开始时核对批次后使用 `Confirm numbering restart`。初始化选最新 ready 帧，预放一整批旧图不会自动从第一张依次回放。

可单独验证 ONNX 模型能加载：

```powershell
docker compose exec -T gsensor python -c "from crystallization_mpc.apps.gsensor.detection.find_edge_points_yolov import OnnxRuntimeYoloV8Seg; OnnxRuntimeYoloV8Seg(); print('ONNX OK')"
```

默认 `dt=5s`，`dt_G=15s`，它们不是同一时钟。启用实时 GSensor 后，首次 G 尚未准备好或既有 G 超过 `2 × dt_G`（默认 30 秒）时，Controller 保持同一实验，持续等待新鲜有效 G，暂停控制计算、适应和设备写入；σ 和 G 目标均受此门控。网页显示 **Waiting for fresh valid G data**，可在 Controller `growth_input.growth_wait` 查看原因和等待起点。30 秒是新鲜度阈值，不是自动终止时限；新 G 通过原有身份、版本及有效性检查且过程输入也有效后，同一实验自动继续，恢复首周期只更新基线而不写设备。明确关闭 GSensor 时仍走原有非相机输入逻辑；其他真实故障或容器重启仍需按提示人工处理。`Add Seed` 是记录实际投晶种事件，不会操作投料设备，必须在真正投晶种后按实验规程使用。

相机拍摄周期应与 `dt_G` 对应；缺图不意味着软件会自动推断实际间隔并修正生长率。GSensor 重启后默认关闭，需再次 Enable；Controller 重启也会等待新的 G 状态和样本，不能依赖旧缓存。

## 11 启用真实控制

只读联调通过后，与设备负责人确认设备控制权、温度和速率边界、浓度/图像标定、通信故障处置以及物理急停。此处的确认来自实际设备投运需求：代码中的写门一旦打开，运行中的 Experiment 可能直接把夹套设定值送给真实设备。

先 `End Experiment`，确认 Controller `stopped`，并让设备按实验室规程处于可交接状态。不要仅 `docker stop`：兼容的 RUNNING checkpoint 可在容器重启后自动恢复运行。

先创建确实全新的正式控制会话总目录，保留只读证据。下面目录已存在时会停止；必须选择新名称，并将后面的 `.env` 改成同一个新路径，不能跳过这个错误：

```powershell
$controlRoot = 'C:\MPCData\session-20261013-control'
if (Test-Path -LiteralPath $controlRoot) { throw 'Choose a new empty control session directory before enabling writes' }
New-Item -ItemType Directory -Path $controlRoot | Out-Null
notepad .env
```

之后编辑 `.env`：

```dotenv
UI_MODE=production
EXPERIMENT_HOST_ROOT=C:/MPCData/session-20261013-control
CONTROLLER_OPCUA_ENABLED=true
CONTROLLER_OPCUA_WRITE_ENABLED=true
CONTROLLER_OPCUA_VERIFY_WRITE=true
```

保留已核实的节点、endpoint、现场范围，再重建三个应用容器：

```powershell
docker compose up -d --force-recreate central gsensor controller
```

重新检查状态为空闲、新 run 尚未开始。创建正式实验，**重新设置并保存新根目录中的全部现场参数，尤其是与 OPC UA 写边界一致的 `T_j_min/T_j_max`**；不要假定只读会话的参数自动继承。按已确认的工艺选择控制目标、图像和适应设置，然后由现场操作者 Start，并持续看设备实际响应、读回、错误和日志。不要为了测试写权限随意向设备写一个温度。

当前程序在两个 OPC UA 开关均打开、输出有效且有设定值时尝试写入，**写入条件没有单独排除 Simulation**。因此绝不能保持这套写入配置再切换到仿真；回到仿真前先结束实验，将两门改为 false 并重建容器。写后校验读取的是设定值节点，并不代表夹套物理温度已经达到目标。若读回不匹配，检查设备远程控制模式、其他控制程序是否覆盖、节点语义和允许的更新时序；不要一开始就关闭验证或放大容差。

写调用超时或读回失败时，命令可能已经到达设备。`write_count` 只统计成功返回的结果，因此真实写入模式下 `write_count=0` 不能证明设备没被写过；必须结合 `process_write_attempted`、错误及设备实际状态判断。

## 12 运行途中问题与结束实验

| 现象 | 最先看哪里 | 处理方向 |
|---|---|---|
| 网页打不开 | `docker compose ps` 与相应日志 | 容器退出、端口占用、路径/配置错误 |
| UI 在线但算法没有输出 | `adapter.safe_noop`、run、scheduler | adapter 为空、未 Start、模式或参数错误 |
| 参数/操作长期 pending | RabbitMQ 健康、Central 与 Controller 日志、run/version | 队列断线或确认未完成；不要反复制造新操作 |
| UA connection refused | Server 监听和实际 IP/端口 | 服务没启动、端口错误、监听接口错误 |
| UA timeout | 主机和容器 TCP 结果 | 路由、防火墙、服务卡住；不是先增大超时 |
| `BadNodeIdUnknown` | 真实 NodeId/namespace | 不能把显示名当 ID，不能重复填 `ns=...;s=...` |
| UA 认证/证书错误 | Server endpoint 安全要求 | 当前客户端缺安全配置支持时停止此阶段，补齐实现 |
| 温度差约 273.15 | 原始节点与 Controller 温度 | 检查摄氏度/开尔文转换，禁止带错单位写入 |
| read failed after reconnect | OPC UA 服务和设备状态 | 当前实现重连一次仍失败会进入 ERROR；先处理物理设备，再恢复软件 |
| write/readback failed | 设备设定值、远程模式和多客户端 | 可能已执行写入，先核实现场状态 |
| GSensor 不出 G | enable、基线确认、图片目录和图像状态 | 初始化没完成或新帧无效；按具体原因处理 |
| G 目标处于 hold | `growth_input.reason`、样本年龄 | 恢复有效测量，不伪造 G 值绕过 |
| Grafana No data | Controller Influx 状态、bucket、run、时间范围 | 可能未产出数据、凭据错误或选错 run；图表默认 UTC，注意本地时区差异 |
| Influx 401 | 初始化与运行 token/org/bucket | 修正凭据一致性，不删数据库卷 |
| 不支持 checkpoint / recovery error | Controller recovery 字段和日志 | 保留旧目录，停服务后更换会话总目录，再重建三个应用 |
| 磁盘写入失败 | 数据盘剩余空间与权限 | 可能导致历史/状态保存失败，按错误处理，不能只看内存仍有曲线 |

中断网络不保证设备进入安全温度，关闭网页也不会结束后台实验。当前 `End Experiment`/Controller stop 主要停止软件计算并断开 OPC UA，没有实现设备物理急停或自动写安全温度。发生异常时先按实验室设备规程处理，再处理容器。

正常结束顺序：

1. Central 点击 `End Experiment`，确认当前 run 已结束且 Controller `stopped`。
2. 按设备规程把装置交回本地/人工控制并确认实际状态；停止相机或修改保存目录前先确定数据落盘。
3. 导出状态、日志，保留实际配置和提交记录，再停止容器。

```powershell
New-Item -ItemType Directory -Force .runtime\lab-handoff | Out-Null
if (Test-Path -LiteralPath .git) {
    git rev-parse HEAD | Set-Content -Encoding utf8 .runtime\lab-handoff\commit.txt
} else {
    $sourceCommit = Read-Host 'Enter the GitHub commit recorded when downloading this ZIP'
    if ([string]::IsNullOrWhiteSpace($sourceCommit)) { throw 'Record the actual source commit before archiving' }
    $sourceCommit | Set-Content -Encoding utf8 .runtime\lab-handoff\commit.txt
}
docker compose logs --no-color --tail 2000 central controller gsensor | Set-Content -Encoding utf8 .runtime\lab-handoff\application.log
Invoke-RestMethod http://127.0.0.1:8002/api/status | ConvertTo-Json -Depth 30 | Set-Content -Encoding utf8 .runtime\lab-handoff\controller-status.json
docker compose run --rm --no-deps controller python -m pip freeze | Set-Content -Encoding utf8 .runtime\lab-handoff\controller-packages.txt
docker compose run --rm --no-deps gsensor python -m pip freeze | Set-Content -Encoding utf8 .runtime\lab-handoff\gsensor-packages.txt
docker compose stop
```

实验图像、checkpoint 和历史文件在 `EXPERIMENT_HOST_ROOT` 对应主机目录。InfluxDB 和 Grafana 的持久化数据在 Docker 命名卷，**只复制实验文件夹不等于备份数据库**。需要数据库备份时，使用现场认可的 InfluxDB 备份流程。诊断和日志可能包含实验数据，分享前检查内容。

不要用 `docker compose down -v`、`docker volume prune` 或删除旧 checkpoint 做常规排障。普通 `stop` 保留容器和卷；即使使用不带 `-v` 的 `down`，仍应先正常结束实验，防止下次启动按 RUNNING 状态恢复。

**关于旧版本升级的准确解释：** Controller 根目录有 `.controller_runtime_state.json`。新版遇到不兼容存档会拒绝恢复，这时仅在 UI 点击 Create Experiment 仍可能被拒绝。要保留旧根目录，在实验停止后把 `.env` 的 `EXPERIMENT_HOST_ROOT` 换为一个全新的目录，重建 Central、GSensor、Controller，再创建新实验；不要手改 schema 或删旧文件冒充兼容。

## 13 无外网时的提前准备

在有网络、相同 CPU 架构的电脑完成第 3～6 节。将代码和以下已构建镜像带到现场，避免安装时依赖临时外网：

```powershell
New-Item -ItemType Directory -Force C:\LabKit | Out-Null
docker image save -o C:\LabKit\mpc-images.tar crystallization-mpc-app:local crystallization-mpc-gsensor:local rabbitmq:3-management influxdb:2.7 grafana/grafana:10.4.3
Get-FileHash -Algorithm SHA256 C:\LabKit\mpc-images.tar
```

保存提交号和镜像归档哈希，现场核对。离线镜像不包含宿主机实验目录、数据库卷、相机驱动或 Docker 安装环境；这些要分别准备。已有代码可复制干净工作目录或用 Git bundle 搬运仓库，避免复制别人整套 `.venv`、旧 `.runtime` 和私密配置。

如果现场还没装 Docker Desktop，提前准备对应的官方安装程序，并让管理员确认 WSL 2、虚拟化和重启安排；仅有 `mpc-images.tar` 不能运行容器。完整 Docker 路线的离线包不需要另外附带新版 Windows Python 或 Miniconda。原生 Python 备用路线则需要另外准备解释器、匹配的 wheel 包和模型缓存；只带源码 ZIP 无法完成离线依赖安装。

现场安装并启动 Docker 后：

```powershell
docker image load -i C:\LabKit\mpc-images.tar
docker compose config --quiet
docker compose up -d --no-build --pull never rabbitmq influxdb
# 等两项基础服务健康后再执行：
docker compose up -d --no-build --pull never controller gsensor central grafana
```

离线路线跳过拉镜像和构建步骤，仍必须完成现场 `.env`、私有数据库配置、目录、节点、单位和只读联调。若镜像与代码提交不一致，应重新准备同一版本，不能把两者混用。

## 14 备选方案 原生 Windows Python

**完整 Docker 主路线不需要执行本节。** 本节仅用于现场决定让三个应用运行在 Windows Python、基础服务仍用 Docker 的场景。不能与主路线的三个应用容器同时占用 8000/8001/8002，也不能让两套 Controller 同时接设备。若电脑连 Docker 基础服务也不能运行，需要现场已有的 RabbitMQ/InfluxDB/Grafana，并按实际地址配置；仅安装新版 Python 并不能替代这些服务。

### 14.1 只有 Python 3.9 时，另装解释器并创建环境

仓库的 `pyproject.toml` 要求 `Python >=3.10`，所以 3.9 不能用于原生安装。这里选择 **Python 3.11 64 位**以接近 Docker 基线，保留原来的 3.9，不卸载、不覆盖实验室其他软件使用的环境。`pip install --upgrade pip` 只升级包管理器，不能把 Python 3.9 升级成 3.11。

若现场已通过其他方式安装了合适的 3.11，直接用其解释器创建下文的新环境即可。若只有 3.9，可按官方方式安装 **Python Install Manager**：从 [Python 官方下载页](https://www.python.org/downloads/windows/) 下载并打开安装文件，或使用 Microsoft Store 中的官方版本。安装后重开 PowerShell；使用 `pymanager` 可避免与旧版 `py` 启动器名称冲突。[Python Windows 安装与版本选择说明](https://docs.python.org/3/using/windows.html#installing-python)

查看可用的 3.11，再安装并检查：

```powershell
pymanager list --online 3.11
pymanager install 3.11
pymanager exec -V:3.11 --version
pymanager exec -V:3.11 -c "import sys, struct; print(sys.executable); print(struct.calcsize('P') * 8)"
```

应看到 `3.11.x`、实际解释器路径和 `64`。这些下载需要网络；如果 `pymanager` 不存在，应先解决管理器安装或命令路径，而不是继续执行后续步骤。不要直接下载官网默认的最新大版本来替换本手册基线。

**3.11 安装包版本说明：** Python 3.11 已进入仅安全修复阶段，3.11.9 是最后提供传统 Windows 二进制安装程序的完整维护版本；后续 CPython 3.11 发布为源码。安装管理器可提供的补丁版本以查询结果为准，不保证与最新源码发布一致。若只能取得 3.11.9，不要把它记为“最新版 3.11”；需与实验室确认其维护要求，优先使用 Docker 主线或团队维护的 3.11 发行版，不必临场从源码编译。[Python 官方 3.11 发布说明](https://www.python.org/downloads/release/python-31115/)

回到项目根目录，建立本项目专用的 `.venv311`。它与已有的 `.venv` 分开，避免误用以前由 3.9 创建的环境：

```powershell
if (-not (Test-Path -LiteralPath pyproject.toml)) { throw 'Open PowerShell in the project root first' }
if (Test-Path -LiteralPath .venv311) { throw 'Environment already exists; check its interpreter or use a fresh project directory' }
pymanager exec -V:3.11 -m venv .venv311
.venv311\Scripts\python.exe -c "import sys, struct; print(sys.version); print(sys.executable); assert sys.version_info[:2] == (3, 11); assert struct.calcsize('P') * 8 == 64"
.venv311\Scripts\python.exe -m pip install --upgrade pip
.venv311\Scripts\python.exe -m pip install -e .
.venv311\Scripts\python.exe -m pip check
```

若 3.11 由传统安装器提供，创建环境那一行改为调用该 **3.11 解释器的完整路径**，形式为 `& 'C:\实际安装路径\python.exe' -m venv .venv311`，先把示例路径替换成真实路径；PowerShell 的 `&` 用于执行带引号的路径。后续仍统一使用 `.venv311\Scripts\python.exe`。已有 Miniconda 时也可由现场人员准备独立的 Python 环境，但不要混用两套环境的安装与启动命令；Miniconda 不会因此安装 Docker Engine。

逐条检查命令结果；任何一步报错都先处理，不要继续启动应用。最后应不报告依赖冲突。直接用环境内的解释器不需要激活脚本，也不需要更改 PowerShell 全局执行策略。`pip install -e .` 已从 `requirements.txt` 安装基础包，不必重复安装同一份 requirements。它是可编辑安装，运行仍需要保留仓库目录和源码。

完整 GSensor/LoFTR 环境继续执行：

```powershell
.venv311\Scripts\python.exe -m pip install -r requirements-gsensor.txt
.venv311\Scripts\python.exe -c "from kornia.feature import LoFTR; import hashlib,pathlib,torch; LoFTR(pretrained='outdoor'); p=pathlib.Path(torch.hub.get_dir())/'checkpoints'/'loftr_outdoor.ckpt'; assert hashlib.sha256(p.read_bytes()).hexdigest() == '21f5bec5968178e8bc8b7633441836fe5de4f47d861dd2cd7dc38e271b0479ec'; print('LoFTR weights OK')"
.venv311\Scripts\python.exe -m pip check
```

权重需要联网预取，且保存在执行该命令的 Windows 用户缓存；换服务账号启动时可能看不到它。只用其他已支持的对齐方式可以不安装 LoFTR 专用依赖，但不能在运行中直接选缺依赖的方法。DLL/import 错误先检查 64 位 Python、对应 wheel、运行库及报错包，不要同时安装多个冲突的 OpenCV 变体。

| 现象 | 原因与处理 |
|---|---|
| 提示 `requires a different Python` 或仍显示 3.9 | 用错解释器；检查 `.venv311\Scripts\python.exe --version`，不要使用裸 `pip` 或旧 `.venv` |
| Windows 的 `python --version` 仍是 3.9 | 可以保留；关键是启动本项目的环境显示 3.11。Docker 路线看容器版本 |
| 新装 3.11 后，旧 `.venv` 仍是 3.9 | 虚拟环境不会自动升级；按上面的流程新建 `.venv311`，不要覆盖旧环境 |
| `py install 3.11` 被当成脚本文件而失败 | 可能调用了旧版启动器；安装管理器后使用 `pymanager install 3.11` |
| 下载超时或找不到匹配 wheel | 核对网络、CPU 架构、64 位解释器和包索引；不要通过删除版本约束绕过 |
| 找不到模块，但刚才明明装过包 | 包可能装进其他解释器；安装和启动始终使用同一个 `.venv311\Scripts\python.exe` |

### 14.2 配置本机应用环境

先只启动基础服务：

```powershell
docker compose up -d rabbitmq influxdb grafana
```

以下公共环境块要在启动每个应用的 PowerShell 中执行一次；`.env` 不会被所有应用自动完整读取。此函数只读取赋值，不执行配置文件里的代码：

```powershell
function Import-LabEnvFile([string]$FilePath) {
    foreach ($line in Get-Content -LiteralPath $FilePath) {
        $text = $line.Trim()
        if (-not $text -or $text.StartsWith('#')) { continue }
        $parts = $text -split '=', 2
        if ($parts.Count -ne 2) { throw "Invalid env assignment" }
        $name = $parts[0].Trim()
        $value = $parts[1].Trim()
        if ($name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { throw "Invalid env name" }
        if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }
}
Import-LabEnvFile .env
Import-LabEnvFile config\influxdb.env
$repoRoot = (Get-Location).Path
$env:RABBIT_URL = 'amqp://guest:guest@127.0.0.1:5673/%2F'
$env:RABBIT_EXCHANGE = 'idp.bus'
$env:RABBIT_HEARTBEAT_SEC = '0'
$env:EXPERIMENT_ROOT = $env:EXPERIMENT_HOST_ROOT
$env:EXPERIMENT_HOST_ROOT_DISPLAY = $env:EXPERIMENT_HOST_ROOT
$env:PARAMS_DEFAULT_FILE = Join-Path $repoRoot 'params_default.yaml'
$env:PARAM_META_FILE = Join-Path $repoRoot 'param_meta.yaml'
$env:OPERATION_META_FILE = Join-Path $repoRoot 'operation_meta.yaml'
$env:PARAMS_FILE = Join-Path $env:EXPERIMENT_ROOT '.central\params_runtime.yaml'
$env:CONTROLLER_STATUS_URL = 'http://127.0.0.1:8002/api/status'
$env:INFLUX_URL = 'http://127.0.0.1:8087'
$env:CONTROLLER_INFLUX_URL = 'http://127.0.0.1:8087'
$env:GSENSOR_STATUS_PUBLISH_ENABLED = 'true'
$env:GSENSOR_SAMPLE_PUBLISH_ENABLED = 'true'
$env:GSENSOR_INFLUX_ENABLED = 'true'
New-Item -ItemType Directory -Force (Join-Path $env:EXPERIMENT_ROOT '.central') | Out-Null
```

这个简易读取块要求 env 每行只有赋值，不支持 shell 展开或行末注释。现场自定义凭据不应打印到终端。团队 Broker 如果不是本机默认开发实例，替换 `RABBIT_URL` 为分配的连接配置，特殊字符按 URL 编码。确保三个终端的共享根目录和参数路径相同。

原生 Python 连接本机 OPC UA Server 时，把 endpoint 配置为 `opc.tcp://127.0.0.1:实际端口`；远端设备用它的真实地址，不套用 Docker hostname。先保持两门关闭完成仿真，再按前面的只读和真实控制阶段修改。环境变量变更后要重启相应 Python 进程；先结束实验，禁止运行中直接换配置。

### 14.3 启动三个应用

三个独立终端均先进入仓库根目录并执行上面的公共环境块，然后各执行对应命令。

Controller 终端：

```powershell
$env:RABBIT_QUEUE = 'controller.in'
.venv311\Scripts\python.exe -m uvicorn crystallization_mpc.apps.controller.app:web_app --host 127.0.0.1 --port 8002
```

GSensor 终端：

```powershell
.venv311\Scripts\python.exe -m uvicorn crystallization_mpc.apps.gsensor.app:web_app --host 127.0.0.1 --port 8001
```

Central 终端：

```powershell
$env:RABBIT_QUEUE = 'central.in'
.venv311\Scripts\python.exe -m uvicorn crystallization_mpc.apps.central.ui.app:web_app --host 127.0.0.1 --port 8000
```

不使用 `--reload` 或多 worker，以免产生多个设备/消息消费者和多个进程同时修改状态。此处只监听本机；若确实需要其他电脑访问 UI，需另行配置监听接口及现场访问策略。页面操作和验收标准与 Docker 主路线相同。

原生 Python 的只读审计用：

```powershell
$stamp = Get-Date -Format yyyyMMdd-HHmmss
.venv311\Scripts\python.exe -m crystallization_mpc.apps.controller.shadow_audit --status-url http://127.0.0.1:8002/api/status --ticks 8 --timeout 180 --output artifacts/controller-shadow-$stamp.json
```

正常退出仍是先在 UI End，确认停止和设备交接，再各终端 Ctrl+C。不要用关闭所有终端来代替设备结束流程。

## 15 现场完成标准

- [ ] 已选择 Docker 主路线或原生备用路线；Docker 路线无需升级宿主机 Python 3.9。
- [ ] 若采用 Docker 主路线，Client、Server 和 Compose 可用，运行 Linux 容器；应用容器内 Python 显示 3.11。
- [ ] 若采用原生备用路线，已确认专用环境为 Python 3.11 64 位，依赖安装和启动使用同一解释器。
- [ ] 记录提交、镜像或实际包版本，配置文件和数据目录都在现场电脑可用。
- [ ] RabbitMQ、InfluxDB、Central、GSensor、Controller、Grafana 逐项可用。
- [ ] 纯仿真能创建、运行、显示输出并正常结束。
- [ ] 应用实际运行的位置能到达正确 OPC UA endpoint，安全策略兼容；Docker 路线检查 Windows 主机和容器，原生路线检查 Windows。
- [ ] 5 个节点、类型、单位、权限和数据新鲜度与现场设备核对一致。
- [ ] 真实只读 Experiment 运行，8 个连续周期审计通过且证据已保存。
- [ ] 相机路径和标定正确，GSensor 初始化完成并持续产生有效新帧。
- [ ] 在真实写入前完成现场控制权、限值和故障处置确认。
- [ ] 结束流程、物理设备交接和数据备份位置已明确。

如果某阶段未通过，保留该阶段的日志和错误，在该阶段修复；不要跨过只读验证直接开启真实写入。

仓库核对入口：[README](../README.md)、[Compose](../docker-compose.yml)、[环境模板](../.env.example)、[Controller 配置](../src/crystallization_mpc/apps/controller/config.py)、[OPC UA 实现](../src/crystallization_mpc/apps/controller/process.py)、[运行与恢复逻辑](../src/crystallization_mpc/apps/controller/service.py)、[只读审计](../src/crystallization_mpc/apps/controller/shadow_audit.py)、[Controller 验证说明](controller-reference-validation.md)。其中历史验证记录不替代本台设备的现场验收。
