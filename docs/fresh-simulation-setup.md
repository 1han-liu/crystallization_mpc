# 新电脑：启动纯数值仿真

本流程用于 Linux + Python 3.12，不需要图片、GSensor 或真实设备。
`run_fresh_simulation.py` 使用 Linux 进程/文件锁；Windows 请使用 WSL/Linux，
不能直接套用本页命令到 PowerShell。不要复制别人的 `.venv` 或 `.runtime`。

## 1. 安装项目

在克隆后的项目根目录执行；已有可用 `.venv` 时不必重建：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
```

## 2. 准备 RabbitMQ

如果已有可用 Broker，直接使用它，不要重复占用端口。新电脑没有 Broker 时，
可安装 Podman，然后启动仅监听本机的开发实例（镜像与项目 Compose 一致）：

```bash
podman run -d --name mpcrystal-dev-rabbitmq \
  -p 127.0.0.1:5673:5672 -p 127.0.0.1:15673:15672 \
  docker.io/library/rabbitmq:3-management
podman exec mpcrystal-dev-rabbitmq rabbitmq-diagnostics -q ping
```

等到 `Ping succeeded` 再继续。已有同名容器时用
`podman start mpcrystal-dev-rabbitmq`，不要重复 `run`。
这是临时开发 Broker；不要把默认账号或上述配置暴露到公网。

## 3. 填写本机连接配置

模板不含密码；复制时不覆盖已有配置：

```bash
cp -n config/rabbitmq.env.example config/rabbitmq.env
chmod 600 config/rabbitmq.env
```

用编辑器打开 `config/rabbitmq.env`，填写唯一一行 `RABBIT_URL=...`。
上述新建的标准开发容器使用公开的默认开发账号，地址是：

```dotenv
RABBIT_URL=amqp://guest:guest@127.0.0.1:5673/%2F
```

如果使用团队已有 Broker，必须换成分配给你的地址、账号、密码和虚拟主机。
账号、密码或虚拟主机中的特殊字符需要 URL 编码。完成后的文件被 Git 忽略；
不要上传、截图或打印真实凭据。仓库只提交空白的 `.example` 模板。

连接配置优先级（高到低）：

1. `--rabbit-env /absolute/path/to/private.env`。
2. 当前进程环境变量 `RABBIT_URL`。
3. 项目内 `config/rabbitmq.env`。
4. 旧本机 `.runtime/rabbitmq-debug/runtime.env`，仅为兼容已有安装。

明确指定的配置无效时会报错，不会悄悄连接另一个 Broker。文件只按赋值文本读取，
不会执行 shell。密码不要作为命令行参数传入。

## 4. 可选：开启 Grafana

如果只体验 Central，可跳过。需要曲线及配置修改标记时，先在无运行实验的窗口执行：

```bash
.venv/bin/python scripts/build_runtime_dashboard.py --output grafana/dashboards/crystallization-mpc.json
.venv/bin/python scripts/configure_local_grafana.py
```

脚本使用 Podman 创建本机 InfluxDB/Grafana，并在被忽略的 `.runtime/grafana-local/`
生成专用随机凭据。之后的新仿真自动加载遥测配置。详见 [Grafana 说明](local-grafana.md)。

## 5. 启动

**警告：此命令会永久删除 `.runtime/manual-simulations/` 下已确认结束的旧脚本会话，
包括其中图片、标注和日志，不备份。其他实验目录和 InfluxDB 历史不在清理范围。
需要保留旧会话时，不要执行这个清理启动器。**

先结束旧实验，在其启动终端按 Ctrl+C，释放 8000/8002；本脚本不会强杀占用端口的进程。
接受以上清理范围后，在项目根目录执行：

```bash
.venv/bin/python scripts/run_fresh_simulation.py --rabbit-env config/rabbitmq.env
```

自定义配置文件也可以在项目外；显式指定文件能避免旧终端的 `RABBIT_URL` 覆盖它。
首次启动无需任何 `.runtime/rabbitmq-debug/` 文件。

打开 <http://127.0.0.1:8000/>，Ctrl+Shift+R 刷新，确认：
Simulation + Simulated / MPC / sigma / Adaptation Disabled。
Create Experiment 后检查参数，再 Start Experiment；Controller 应为当前 run 的 `running`。
8002 是 Controller 状态接口，不是 GSensor；本流程不用打开 8001。

结束时先点击 End Experiment，再在终端 Ctrl+C。不要把“启动成功”当作拟合或控制性能验收。

## 测试与已知限制

测试代码、MATLAB 参考数据和测试/演示脚本仅在本地保留，不随 Git 上传。
以下测试命令仅适用于已具备这些文件的开发目录，新克隆的项目不能直接执行；
上面的正常仿真启动步骤不依赖这些测试文件。

运行 Python 测试需要 pytest；完整 GSensor 测试还需要 `requirements-gsensor.txt`。
MATLAB 对照测试使用固定 Python/NumPy/SciPy 版本，参见
[Controller 基准说明](controller-reference-validation.md)。现有 13 项数值容差失败未被本次修复隐藏。

浏览器行为测试需要 Node.js、Playwright 和 Chromium/Chrome（可通过 `CHROME_PATH`
指定浏览器路径，`NODE_PATH` 指向 Playwright 所在 node_modules）。执行：

```bash
node --test tests/central/runtime_ui.test.cjs tests/gsensor/alignment_ui.test.cjs
```

GSensor 浏览器测试使用真实页面和 DOM 事件、模拟状态/网络：检查初始化时才可选择、
不可用方法禁用、草稿保持、确认时提交所选方法以及确认后锁定；不运行图像算法。
