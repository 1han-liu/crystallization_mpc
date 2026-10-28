# 实验室日常启动、数据状态和关闭

适用于已完成现场配置和测试的 Docker Desktop 安装。日常使用继续沿用实验室
`.env`、`config/influxdb.env`、证书和实验目录，不要用模板覆盖它们。

## 开机后是否直接打开网页

Docker Engine 和本项目的容器均已运行时，直接打开
`http://localhost:8000` 即可进入 Central；不需要另外启动 Python 或 uvicorn。
从其他电脑访问时，将 `localhost` 换成实验室电脑的地址。

本项目容器配置了 `restart: unless-stopped`。如果上次容器保持运行，Docker
Engine 重启后通常会恢复这些容器；如果上次手动执行了 Stop，不能只靠打开
Docker Desktop 恢复。首次部署尚未创建的容器也不会凭空出现。

确认设备软件的 OPC UA Server 已启动且可达。如果 Docker 引擎启动后各容器已
自动恢复，可以直接打开 Central，不需要每次执行启动命令。若容器未运行，可以
在 Docker Desktop 中启动本项目，或在**原部署目录**运行以下检查和启动命令：

```powershell
docker info
docker compose --profile process-io up -d --no-build --pull never
docker compose --profile process-io ps
```

`docker info` 成功表示引擎已就绪；Docker Desktop 窗口已打开并不等于引擎已就绪。
这条启动命令使用本机已有镜像，不下载或构建镜像；缺少镜像时应按安装流程
处理。`process-io` profile 包含 `telegraf` 和 `opcua-writer`，普通
`docker compose up -d` 不会新建这两个 profile 服务。
本项目不会代为启动设备软件、OPC UA Server 或相机拍摄程序。

命令可以在 Windows PowerShell、Windows Terminal 的 PowerShell 或 VS Code 的
PowerShell 终端输入，VS Code 不是必需的。先进入包含 `docker-compose.yml` 的
实际部署目录；不要从另一个项目目录运行。

## 网页中的操作顺序和开关

1. 打开 Central，观察顶部 **OPC UA Data Source** 卡片。显示
   **Receiving normally** 表示 Central 已从 Telegraf → RabbitMQ 收到同一设备、
   同一纳秒采集轮次的五项有效数据，节点映射匹配、质量 Good、数值通过基本范围
   检查且未过期。默认新鲜度上限为 `PROCESS_SAMPLE_MAX_AGE_S=5` 秒。
   数据可以保持恒定；只要每次采集时间更新，就不是数据过期。
2. 核对卡片底部的 Controller 过程输入/写入模式。`CONTROLLER_PROCESS_ENABLED`
   控制 Controller 的过程输入；`CONTROLLER_PROCESS_WRITE_ENABLED` 控制真实
   Experiment 的设备写入。两者是部署环境配置，修改后需要结束实验并重新创建
   Controller 容器才能生效。卡片本身始终只读，不改变这两个开关。
3. 选择运行配置，点击 **Create Experiment**，核对参数与相机保存目录。
4. 点击 **Start Experiment**，Controller 才开始本次实验。网页可访问、数据卡片
   正常和 Controller 正在控制是三个不同状态。卡片证明 Central 收到了源数据，
   实际控制仍以 Controller 对本 run 的输入校验和运行状态为准。
5. 使用实时图像时，确认相机拍摄程序持续向本次实验的图片目录保存文件，
   再点击 **Enable GSensor**，按现有界面完成初始化/标记并确认生长率就绪。
   Start Experiment 不会自动启用 GSensor；GSensor 重启后也需要重新 Enable。
6. 真正投晶种后，按实验规程使用 **Add Seed** 记录事件。

卡片的 Waiting、Invalid、Data expired、Disconnected 都不会显示绿色正常状态。
负浓度也不会被显示为正常或补成零。停止更新、Central/RabbitMQ 断开、浏览器
状态请求失败时，正常提示会失效，数值清空。新的完整有效采集可恢复卡片状态；
这不解除 Controller 自己的故障或重启确认要求。

## 管理页面入口

Central 顶部提供 Grafana、Gsensor UI、RabbitMQ、InfluxDB 链接。
默认采用浏览器当前访问 Central 的主机名，端口分别为
`3000`、`8001`、`15673`、`8087`，打开后使用现场原有账号登录。
如果使用不同端口或反向代理，可在 `.env` 配置
`CENTRAL_GRAFANA_UI_URL`、`CENTRAL_GSENSOR_UI_URL`、
`CENTRAL_RABBITMQ_UI_URL`、`CENTRAL_INFLUXDB_UI_URL`，重新创建 Central 容器后生效。
链接本身不表示对应服务已经健康或数据库已经写入成功。

## 日常关闭

1. 在 Central 点击 **End Experiment**，确认 Controller 已停止，并按实验室规程
   完成设备交接。软件结束实验不等于设备物理急停或自动切换到安全温度。
2. 等待本次 CSV 导出完成，即可正常关闭 Windows，不需要额外暂停 Docker。
   若保持容器运行，Docker 引擎下次启动时可按上述重启策略恢复服务。
3. 如果希望单独停止本项目、继续使用电脑，可以在原部署目录运行：

   ```powershell
   docker compose --profile process-io stop
   ```

   这是可选操作；手动停止后，下次需重新启动项目容器。

关闭网页不会停止后台实验。正常关机前仍应先完成 End Experiment 和导出。

`stop` 保留容器、卷和实验文件；日常关闭不使用 `down -v`。
当前 Controller 若在活动实验中被重启，恢复后进入 `recovery_required`，
需要先 End 原实验再创建新实验，不能把重启当作实验暂停/继续。

## 是否需要双击启停脚本

建议为实验员提供 Windows 启停入口，减少忘记 `process-io` profile 或进入错误
目录的情况。脚本只是封装上述步骤，不是新的软件功能开关。启动脚本应固定原
部署目录、检查 Docker Engine、启动已有镜像、检查状态后打开 Central；不应
自动 Start Experiment、Enable GSensor 或改写现场配置。关闭脚本应先确认实验
和导出结束，无法确认状态时不要直接停止容器。本次先明确日常流程，未添加自动
启停脚本，也未修改 Docker Desktop 的自启动设置。

## 将本次 UI 更新部署到实验室电脑

在实验结束、导出完成后，拉取本次代码并使用原现场配置：

```powershell
docker compose build central
docker compose --profile process-io up -d --no-build --pull never
```

Central、Controller、writer 共用 application 镜像；上面的 `up` 可能重新创建
使用该镜像的容器，因此务必在实验已结束后更新。GSensor 的独立镜像本次未修改。
再打开 Central 检查卡片、现场五项数值和管理链接。
