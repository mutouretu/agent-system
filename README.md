# rd-platform · 模块一

本阶段实现 `src/rd_platform/runtime/sandbox.py`。调度器、PostgreSQL 队列和 Agents SDK 适配器留待后续阶段。

## 沙箱接口

```python
from rd_platform.runtime.sandbox import DockerSandbox

# 这段代码是可信控制面：只调用 Docker API，不在宿主机执行任务。
with DockerSandbox(
    image="rd-worker:approved",
    workspace_dir="/staging/job/workspace",
    input_dir="/staging/job/input",
    output_dir="/staging/job/output",
    timeout_seconds=300,
) as sandbox:
    exit_code, stdout, stderr = sandbox.exec_run(
        ["python", "-m", "pytest", "-p", "no:cacheprovider", "--junitxml=/output/tests.xml"]
    )
```

`start()` 返回当前实例；`exec_run(cmd)` 返回 `(int, str, str)`，保留非零退出码，分别解码 stdout/stderr（UTF-8，非法字节替换）。字符串按 argv 分词，不隐式调用 shell；需要 shell 时显式传入 `["/bin/sh", "-c", script]`。

`cleanup()` 强制删除整个容器及其匿名卷，可重复调用。启动失败、执行超时、输出超限、Docker 通信异常都会触发清理；正常执行后可继续调用 `exec_run`，由 `with` 或调用方 `finally` 完成清理。清理失败最多尝试三次，保留容器标识并抛出 `SandboxError`，调用方必须重试；不会把失败伪装成回收成功。实例清理成功后不可重启。单实例串行执行命令，其他线程可调用 `cleanup()` 取消。

## 隔离配置与约束

- Linux 容器：2 CPU、6 GiB 内存、内存加 swap 总额 6 GiB、256 PID；禁用网络。
- 非 root UID/GID `65532:65532`，删除全部 capabilities，启用 `no-new-privileges`，保留 Docker 默认 seccomp。
- 根文件系统、`/workspace`、`/input` 只读；`/output` 可写；`/tmp` 为 512 MiB tmpfs，`/dev/shm` 为 64 MiB。
- 镜像启动命令及健康检查被覆盖；启用 init 回收容器子进程，不重启容器，退出后由 Docker 自动删除。闲置主进程最多运行一小时；显式清理仍是主要回收路径。
- 默认命令期限 300 秒，stdout/stderr 合计最多 8 MiB。静默阻塞和输出洪泛也受限；超限销毁整个容器，包括子进程。Docker API 请求/清理耗时可额外增加至返回错误的时间。
- 不挂载 Docker socket、认证目录或宿主机环境变量；不执行宿主机 subprocess，不在运行时自动拉取镜像。

镜像必须由控制面预先配置且可信，包含 `/bin/sleep` 及任务所需工具；拒绝非 Linux 镜像和声明隐式 `VOLUME` 的镜像。依赖需预装或从只读输入离线安装。工作区写入、构建或会产生缓存的测试应先在容器内复制到 `/tmp`，最终报告写入 `/output`。

三个挂载目录必须事先存在、互不嵌套，并且路径位于 Docker daemon 所在主机。输出目录需预先对 UID/GID 65532 可写；驱动不会修改宿主机权限。仅传递经过整理、不含秘密和特殊设备的任务目录；输出目录应是任务专属目录。输出磁盘总量配额及 daemon/控制器宕机后的遗留容器巡检需由部署层及后续调度器处理，不能仅靠 Python `finally` 保证。

## 本地验证（无模型调用）

要求运行中的 Linux Docker Engine / Docker Desktop，以及宿主机 Python 3.11+。宿主机虚拟环境仅安装可信 Docker 生命周期控制器依赖。下列依赖下载不执行任务代码；所有测试、动态脚本及 pip 的沙箱依赖安装都在禁网容器内完成。

```sh
python3 -m venv .venv-control
.venv-control/bin/python -m pip install --only-binary=:all: docker==7.1.0
mkdir -p sandbox-validation/wheels
.venv-control/bin/python -m pip download --only-binary=:all: \
  --platform any --implementation py --abi none --python-version 3.12 \
  --dest sandbox-validation/wheels docker==7.1.0 pytest==8.3.5
PYTHONDONTWRITEBYTECODE=1 .venv-control/bin/python scripts/validate_sandbox.py
```

不要在宿主机直接运行 pytest。验证控制器会：

1. 使用项目内空 Docker 配置目录，避免读取用户认证配置。通过 SDK 使用本地 `python:3.12-slim`；缺失时从公共仓库拉取，也可用 `SANDBOX_IMAGE` 指定已配置镜像。
2. 只整理 `src/`、`tests/` 和包配置到一次性工作区。仅将新建任务产物目录设置为可写，不改动任何系统权限。
3. 通过实际沙箱执行探针，收集只读写入拒绝、网络不可达、stdout/stderr、非零退出码、SHA-256、超时及输出洪泛销毁证据，并检查容器是否已删除。
4. 将证据只读挂载到另一隔离容器，离线安装依赖并运行 Fake-only 单元测试和真实证据集成断言。测试容器没有 Docker socket 或外网访问权。
5. 在 `sandbox-validation/run-*/` 保留 `input/evidence.json`、`output/result.txt` 和 `output/pytest.xml`，回传容器内 pytest 的退出码，最终清理所有本次创建的容器。

单元测试覆盖 API 配置、字节流分帧、输入校验、创建响应丢失恢复、通信失败、超时、输出限制和清理重试；集成测试验证真实 Docker 行为。未提供 `SANDBOX_EVIDENCE` 时集成测试明确跳过，标准控制器流程会提供证据并运行全部断言。

Docker 生命周期和硬限制参数依据 [Docker SDK 容器 API](https://docker-py.readthedocs.io/en/stable/containers.html)。

## Single-Agent 沙箱反馈闭环

`scripts/validate_self_correction.py` 提供可恢复的反馈控制器。当前会话 Agent 读取真实失败输出后提交代码修订；控制器不模拟模型、不预设修复顺序，也尚未接入独立模型 API 自动调用。任务及初始缺陷代码位于 `tests/fixtures/self_correction/`。

```sh
# 第一次运行故意错误的版本；退出码 1 是预期测试失败，输出包含 RUN_DIR。
PYTHONDONTWRITEBYTECODE=1 .venv-control/bin/python scripts/validate_self_correction.py init

# Agent 阅读 RUN_DIR/iteration-00/feedback.json 后编写新的候选代码和 diagnosis.json。
# diagnosis.json 必须含 reason 和上一轮 feedback.json 的 SHA-256（feedback_sha256）。
PYTHONDONTWRITEBYTECODE=1 .venv-control/bin/python scripts/validate_self_correction.py \
  retry RUN_DIR path/to/new_candidate.py --diagnosis path/to/diagnosis.json

PYTHONDONTWRITEBYTECODE=1 .venv-control/bin/python scripts/validate_self_correction.py status RUN_DIR
```

初始运行计为 iteration 0，最多允许 3 次修订；成功后拒绝继续重试。每轮使用新的禁网 Docker 容器，离线安装 pytest，工作区和测试均只读。首次运行冻结测试文件 SHA-256 和镜像 ID，后续重试检查测试未改变。完整 stdout/stderr、长 traceback、JUnit、候选代码、修改理由和容器删除记录均保存在每轮目录。pytest 的断言 traceback 通常出现在 stdout，不能只回传 stderr。

`summary.json` 记录修订次数、执行次数、固定 24 项测试的逐轮通过率、单调性和最终状态。初次实测为 `21/24 → 24/24`，1 次修订收敛（共执行 2 次），两个容器均删除。`revision_01.py` 是本次读取失败反馈后产生的修复快照，之后直接重放此文件属于结果复现，不能算新的 Agent 自我纠错能力测量。这是同一会话构造任务并修复的闭环示例，不是独立盲测或跨任务成功率评估。

## Runtime Probe 完整性与开销验证

```sh
PYTHONDONTWRITEBYTECODE=1 .venv-control/bin/python scripts/validate_probe.py
```

`src/rd_platform/runtime/probe.py` 是仅在沙箱中运行的 Python 上下文探针。压测控制器准备独立工作区，通过 Docker SDK 执行探针、基准测试和 pytest，最后确认容器删除。证据位于 `sandbox-validation/probe-*/`：`summary.json` 为控制摘要，`output/context.json` 为实际输出上下文，`output/benchmark.json` 包含每一对原始计时，`output/pytest.xml` 为断言报告，`lifecycle.json` 记录隔离配置与回收结果。失败运行也保留证据。

探针按指定函数的调用次数采样：前 8 次全部采集、第 9–256 次每 16 次采集一次，之后每 512 次采集一次。调用状态标记为 `running`，结束状态为 `ok` 或 `error`。环形缓冲最多保留 48 个调用样本和每流 24 个日志样本；大对象使用限长摘要，重复日志和丢弃数量分别计数。异常栈由最终 traceback 单独提取，优先保留函数入参和局部变量，日志/普通样本先让出预算。发生任何进一步裁剪均记录计数。

Python 3.12+ 优先使用 [sys.monitoring 的局部 PY_START 事件](https://docs.python.org/3.12/library/sys.monitoring.html)，只监听指定代码对象；退出时关闭事件、注销回调并释放工具槽。旧版本或槽位不可用时回退为 `sys.setprofile`，恢复原有 profiler；本次性能结论只覆盖实际测试的 Python 3.12 局部事件后端。

默认上下文上限 64 KiB，同时施加 16,384 的 UTF-8 字节保守 Token 上界，因此本配置实际输出不超过 16 KiB。这个值适用于字节分词器的保守预算，不是某个模型的实际 Token 数。预算计入完整 JSON 及结尾换行，按 UTF-8 字节验证，不能简单按字符数截断。

固定负载包含 10,000 次迭代、约 1.62 MiB 输出、四级函数调用和最后一条记录的除零异常。性能对照在同一沙箱进程内进行：先各预热一次，再交替执行 7 对基线/探针运行。两组执行相同计算与打印，基线使用计数字节的丢弃流，探针额外执行采样、摘要、裁剪及序列化；计时不包含容器启动和安装依赖。预先固定的验收门槛为中位耗时增幅不超过 100%，且额外耗时不超过 150 ms。

最终实测 17 项测试通过：输出 15,502 字节（较原始日志少约 99.09%），每个热函数 10,000 次调用采集 43 次，最终预算保留 7 个调用样本及四层异常帧；错误位置的 `iteration=9999`、`record_id=record-09999`、`denominator=0` 等局部变量未裁剪。基线中位耗时约 56.17 ms，探针约 71.48 ms，增幅 27.3%。这是该固定负载的实测结果，不代表所有程序的开销上限。

探针是协作式观测工具，不是新的安全边界：只采样显式选择的函数及调用线程，不逐行记录所有循环状态；Python 流重定向不覆盖原生文件描述符写入或子进程输出。无法协作的输出、原生代码或恶意绕过仍由 Docker 的硬输出、资源和期限限制处理。

## 订阅 CLI 无人值守自愈

```sh
PYTHONDONTWRITEBYTECODE=1 .venv-control/bin/python scripts/validate_cli_loop.py
```

该控制器自动执行初始缺陷版本，把任务契约、当前代码、完整 `feedback.json`（含 stdout 的 pytest traceback 和 stderr）通过 stdin 发给本地 Codex CLI。CLI 按 JSON Schema 返回完整候选代码、修复理由和反馈 SHA-256；控制器校验响应后生成 `diagnosis.json`，调用原有沙箱控制器的 `retry`，最多修订 3 次。没有预置修复或人工填入候选代码；测试文件哈希与 Docker 镜像在整个闭环中保持固定。最终 pytest 全通过、通过率严格递增且容器全部删除时，总状态才为 `pass`，不会隐藏中途回退或把跳过用例算作通过。

使用已登录的 ChatGPT 订阅，不要求 API Key。脚本只调用 CLI 的 `login status`，不读取、复制或输出认证文件；子进程移除 API Key 环境变量并设置 `forced_login_method=chatgpt`。它优先检查 PATH 中的 Codex 是否支持需要的非交互参数，不满足时尝试 ChatGPT/Codex 应用内置 CLI；也可用 `--codex /absolute/path/to/codex` 指定。不会自动安装或升级系统 CLI。`--model` 可选，未指定时使用 CLI 默认模型。

宿主机的 subprocess 仅执行可信 CLI 和 Docker 生命周期控制器，使用 argv 数组、`shell=False`、独立进程组和超时回收。模型侧关闭 shell/unified exec、Code Mode host、插件、应用、hooks、多 Agent 和浏览器等工具，以只读模式输出 JSON；候选代码不在宿主机导入、编译或执行。CLI 事件审计拒绝命令执行、文件修改、MCP 调用及未知工具活动。CLI 的可恢复重连提示、工具不可用提示和 HTTPS 回退也可能使用 `error` 事件；这些事件保留为诊断，只有最终 `turn.completed`、成功退出、合法响应且无工具活动时才继续。`turn.failed` 始终终止流程。启用 CLI 对系统代理的支持，不修改系统代理配置。

结果位于 `sandbox-validation/cli-loop-*/`：

- `summary.json`：订阅认证方式、CLI 路径/版本、每轮命令耗时、Token usage（CLI 报告值）、收敛次数、通过率及总状态。
- `cli-NN/`：完整 prompt、原始 JSONL 事件、stderr、命令参数与耗时、模型响应、候选代码和 diagnosis。
- `sandbox/iteration-NN/`：真实 pytest 输出、JUnit、固定测试、代码快照和容器删除记录。

默认每次 CLI 请求最多等待 180 秒，可通过 `--timeout` 调整到至多 600 秒；`--max-retries` 可设为 1–3。认证失效、超时、模型输出不合法或 Docker 异常会明确记录失败，不会自动读取认证文件补救。相关参数依据 [OpenAI Docs 非交互执行说明](https://learn.chatgpt.com/docs/non-interactive-mode)及[配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)，并通过本机 CLI `--help` 确认可用性。

## v0.2.0 开发服务启动指南

前置条件：Docker Desktop 已启动并使用 Linux 容器、Docker Compose v2 支持 `up --wait`，以及安装了 `docker==7.1.0` 的 `.venv-control`（Python 3.11+）。可以用 `RD_CONTROL_PYTHON` 指向已有控制面虚拟环境。首次构建需要联网下载 Python 基础镜像和二进制 Wheels；FastAPI、Uvicorn 只安装在服务镜像中。

```sh
./scripts/deploy.sh start
.venv-control/bin/python examples/client_demo.py
./scripts/deploy.sh status
./scripts/deploy.sh logs       # 持续查看日志，Ctrl-C 退出日志查看
./scripts/deploy.sh stop
```

`start` 检查 Docker、虚拟环境和端口，自动创建 `data/`、`logs/`、`models/`，通过 Python Docker SDK 构建镜像，再等待两个服务健康并验证宿主机端口、打印日志。Docker 构建阶段使用离线 Wheels 且禁网；宿主机不运行 pytest、算法任务或构建依赖源码。重复启动会检查并更新本项目的容器，其他进程占用端口时明确报错，不会结束该进程。启动失败可运行 `logs` 排查，`stop` 回收容器和网络，保留本地数据。

| 服务 | 默认地址 | 用途 |
| --- | --- | --- |
| API | `http://127.0.0.1:8000/health` | 存储目录与应用就绪检查 |
| OpenAPI | `http://127.0.0.1:8000/docs` | 请求格式及交互调用 |
| 探针 | `http://127.0.0.1:8001/health` | 执行现有 RuntimeProbe 的固定自检 |
| 探针报告 | `http://127.0.0.1:8001/probe` | 有预算限制的上下文采样报告，缓存 5 秒 |

`examples/client_demo.py` 仅依赖 Python 标准库，检查两个健康接口、执行记录过滤、写入并读回 `demo` 数据集、列出模型文件，成功输出 `"status": "PASS"`。每次运行会覆盖 `data/demo.json`。

开发 API 提供以下固定能力：

- `POST /v1/algorithms/filter`：输入 `records`（每条含字符串 `id`、整数 `score`、可选布尔值 `active`），可选 `min_score`、`limit`。保留活跃且分数大于等于阈值的记录，同 ID 取最高分，按分数降序、ID 升序排序；`limit=0` 返回空列表。
- `PUT /v1/data/{name}`、`GET /v1/data/{name}`：原子写入及读取 JSON 数据集，写入返回 SHA-256。名称限 1–64 位字母、数字、下划线、连字符；请求最多 1 MiB、记录最多 1,000 条。
- `GET /v1/models`：列出 `models/` 下最多 100 个普通文件，不加载或执行模型文件。

API 挂载 `./data`、`./logs` 为可写目录，`./models` 为只读目录；日志同时提供容器 stdout 和轮转的 `logs/service.log`。两个服务均使用非 root 用户、只读根文件系统、资源限制及独立桥接网络，端口仅发布到本机回环地址。服务桥接网络允许出网；执行动态任务的原有 DockerSandbox 仍使用 `NetworkMode=none`。探针独立运行于单进程容器，避免其 Python 流重定向影响 API 请求。服务没有挂载 Docker socket，也没有动态代码、Shell 或上传执行接口。此阶段提供本地开发调用入口；任务队列、模型推理加载、订阅 Agent 调度仍需后续接入，无需 API Key 即可运行示例。

可调整端口和运行用户（后续操作应沿用相同环境变量）：

```sh
API_PORT=8080 PROBE_PORT=8081 ./scripts/deploy.sh start
.venv-control/bin/python examples/client_demo.py --api-url http://127.0.0.1:8080 --probe-url http://127.0.0.1:8081
```

`SERVICE_UID` / `SERVICE_GID` 默认使用调用用户的 UID/GID，Linux 上须确保该用户可写数据和日志目录；脚本不修改系统权限。`RD_SERVICE_IMAGE` 可覆盖默认镜像名 `rd-platform-service:0.2.0-dev`。Compose 文件使用已构建的镜像，如需直接调用 Compose，请先运行 `scripts/build_service_image.py` 并创建挂载目录。

容器内回归与实际服务验收（先启动服务）：

```sh
PYTHONDONTWRITEBYTECODE=1 .venv-control/bin/python scripts/validate_service.py
```

该控制器通过 Docker SDK 在禁网、只读工作区的临时沙箱执行基础单元测试、服务测试和脚本语法检查，再在 API 容器中运行客户端验证独立探针和数据服务，检查宿主机发布端口。证据输出到 `sandbox-validation/service-*/`，含 JUnit、stdout/stderr、服务日志及 `summary.json`；临时测试容器自动销毁，Compose 服务由 `deploy.sh stop` 停止。
