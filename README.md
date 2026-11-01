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
# Python 3.12 Linux ARM64 框架依赖；AMD64 将 aarch64 改成 x86_64。
.venv-control/bin/python -m pip download --only-binary=:all: \
  --platform manylinux2014_aarch64 --platform manylinux_2_28_aarch64 \
  --implementation cp --abi cp312 --python-version 3.12 \
  --dest sandbox-validation/wheels pydantic==2.14.2
PYTHONDONTWRITEBYTECODE=1 .venv-control/bin/python scripts/validate_sandbox.py
```

不要在宿主机直接运行 pytest。验证控制器会：

1. 使用项目内空 Docker 配置目录，避免读取用户认证配置。通过 SDK 使用本地 `python:3.12-slim`；缺失时从公共仓库拉取，也可用 `SANDBOX_IMAGE` 指定基础镜像。控制器从离线 Wheels 通过 SDK 禁网构建验证镜像，将 Pydantic 原生扩展预装在镜像只读层，保留沙箱 `/tmp` 的 noexec 限制。
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

## 项目管理与 Agent 调度框架

`domain/` 提供无 Web/模型 SDK 依赖的 `Project`、`Task`、`TaskInput`、`TaskOutput` 和 `AgentMetadata`。`Project.add_task()` 支持先添加下游任务，`build_dag()` 校验重复 ID、缺失依赖和环，`get_executable_tasks()` 返回所有依赖已完成的 PENDING 任务。

`AgentRegistry.register()` 注册可信 `BaseAgent` 实现，按 capability 匹配，同能力有多个 Agent 时选择最先注册的实现；支持 `unregister()` 动态移除。`Dispatcher.dispatch(project_or_task)` 是异步入口，项目按拓扑顺序串行执行，更新 `PENDING → RUNNING → COMPLETED / FAILED`。找不到 Agent 或上游失败时标记 `BLOCKED`，独立分支继续执行。已结束任务不会因重复 dispatch 而重跑；Python 调用方可先显式 `task.reset()` 再重试，依赖它的 BLOCKED 任务也需 reset。

Agent 接收 `AgentInput`，其中 payload 为任务输入，context 是独立的项目上下文，加上保留字段 `project_id` 和 `dependencies`（直接依赖任务 ID 到产物字典的映射）。Agent 返回 `AgentOutput`，产物必须是有限、可 JSON 序列化的数据。调度器记录 Agent ID、产物、日志和错误；同一项目重复并发调度会被拒绝，取消请求不会残留 RUNNING 状态。默认每个任务的协作式异步超时为 30 秒，可用 `Dispatcher(registry, task_timeout=...)` 调整；它不能强杀阻塞的同步代码，执行不可信代码仍必须使用 DockerSandbox 的硬超时及隔离能力。

默认注册两个无模型调用的 Agent：

| Agent | Capability | 固定行为 |
| --- | --- | --- |
| DataPipelineAgent | `data_processing` | 数值列表排序、去重，返回 `values` 和 `count` |
| AlgorithmRunnerAgent | `algorithm_execution` | 返回 count、sum、min、max、mean，空列表的 min/max/mean 为 null |

输入使用 `{"values": [3, 1, 3, 2]}`，最多 10,000 个有限数值，不接受布尔值。没有显式 values 时读取唯一上游的 values；多个上游时使用 `{"source_task": "上游任务ID"}` 选择来源。

新增 HTTP 接口：

- `GET /v1/agents`：返回已注册 Agent 元数据。
- `POST /v1/projects`：接收 `name`、可选 `context`、`tasks`，返回 201 和项目 ID。每个任务含 `name`、`capability`、可选 `id`、`input`、`depends_on`；引用依赖时应显式指定任务 ID。
- `POST /v1/projects/{id}/dispatch`：等待本次调度完成并返回全部任务状态与输出。任务失败通过任务的 FAILED/BLOCKED 和 error 字段表达；项目不存在返回 404，并发调度返回 409，非法项目 DAG 返回 422。

```sh
./scripts/deploy.sh start
.venv-control/bin/python examples/project_demo.py
PYTHONDONTWRITEBYTECODE=1 .venv-control/bin/python scripts/validate_service.py
```

示例创建“排序去重 → 统计”任务图，成功输出 `PASS`，下游 mean 为 2。验证控制器在容器中执行所有单元与服务测试，并分别调用原服务示例和项目示例，保存 `project-client.json` 作为真实 HTTP 调度证据。

当前项目存储是进程内字典：每个应用最多 100 个项目、每个项目最多 100 个任务，服务重启后丢失；须保持 Compose 中的单 worker 配置。该实现适用于本地开发，不提供 PostgreSQL 持久化队列、分布式租约或跨进程调度。自定义 Agent 通过 Python 注册（或注入 `create_app(..., registry=...)`），HTTP 请求不能注册代码或修改调度状态。

`create_default_registry()` 从 `AGENT_REGISTRY` 类目录创建独立的 Agent 实例。两个通用示例通过装饰器注册，也可通过 `create_app(..., registry=...)` 注入自定义注册表。

## 通用 Agent 契约与动态注册

`agents/base.py` 导出 Pydantic 模型 `AgentInput`、`AgentOutput`，以及 `AgentRole` 枚举和 `ARCHITECT`、`CODER`、`REVIEWER`、`TESTER` 常量。角色可设置在 `AgentMetadata.role` 中，并通过 `/v1/agents` 返回。此处定义协议和角色标识，具体架构师、开发者、测试者、审查者行为由后续实现提供。

- `AgentInput`：必填 task_id，以及 task_name、capability、payload、context、depends_on。payload/context 会复制并检查为有限 JSON 数据；未知字段被拒绝。
- `AgentOutput`：status 为 COMPLETED（默认）、FAILED 或 BLOCKED；artifacts 为字典，logs 可选（最多 65,536 字符），error 可选（最多 4,096 字符）。可使用 `model_dump_json()`、`model_validate_json()` 和 `model_json_schema()` 序列化、校验和导出 Schema。
- 调度器调用 `async run(request: AgentInput) -> AgentOutput`，再次校验返回值并映射到 Task.status、Task.output、Task.logs 和 Task.error。下游 dependencies 中直接取得上游 artifacts，保留原 API 的 output 数据布局；失败/阻塞产物保留为证据但不作为成功依赖。异常和超时继续按 FAILED 处理。

注册示例（可信应用初始化代码；任务执行仍放在 Docker 中）：

```python
from rd_platform.agents import AgentInput, AgentOutput, BaseAgent, CODER
from rd_platform.domain import AgentMetadata
from rd_platform.manager import register_agent

@register_agent("echo")
class EchoAgent(BaseAgent):
    def __init__(self):
        super().__init__(AgentMetadata("echo", "Echo", {"echo"}, role=CODER))

    async def run(self, request: AgentInput) -> AgentOutput:
        return AgentOutput(artifacts={"received": request.payload}, logs="echo complete")
```

也可以设置 `AGENT_REGISTRY["echo"] = EchoAgent`，再用 `AgentRegistry.from_catalog()` 构建实例注册表。装饰器拒绝重复名称、抽象类和非异步 run；目录实例化时会再次校验。目录中的类需要无参构造器；有依赖注入需求时用已有的 `registry.register(ConfiguredAgent(...))`。目录更新只影响之后创建的注册表，运行中的服务可通过实例注册接口更新。服务启动前须显式导入可信插件模块，不扫描任意文件或从 HTTP 请求加载 Python 类。

旧的 `run(task, context) -> dict` 实现应显式包装：`registry.register(LegacyAgentAdapter(old_agent))`。适配器创建独立 Task 副本并把字典结果转成 AgentOutput，不通过捕获 TypeError 猜测签名或重试执行。已有 client_demo/project_demo 保持原调用方式。

`tests/unit/test_base_agent.py` 使用 EchoAgent/DummyAgent 验证动态注册、角色、Schema、DAG 产物传递、日志、失败传播、取消/超时以及旧接口适配。`scripts/validate_service.py` 在 Docker 内运行全部单元和服务测试，包括动态注册 Agent 的 HTTP 验证；原有示例继续进行真实服务调用验收。

## 软件研发角色骨架（方案 A）

`src/rd_platform/agents/dev/` 实现四个可注入的角色骨架，沿用 `AgentInput → AgentOutput`。没有默认模型调用、自动自纠循环或生产 SandboxExecutor。导入该包会通过装饰器注册四个类；随后 `create_default_registry()` 可创建无配置实例。实际应用应显式导入可信模块，并将带后端的实例加入 `AgentRegistry`，通过 `create_app(..., registry=...)` 注入服务。默认服务启动入口仍只加载原有两个示例。

| 角色 / capability | 必需输入 | 产物 Schema |
| --- | --- | --- |
| ArchitectAgent / architecture | requirements，可选 constraints | ArchitectArtifacts：architecture_spec（modules/data_flow/decisions）、api_contracts、subtasks |
| CoderAgent / coding | architecture，可选 instructions | CoderArtifacts：files、tests、commit_message |
| ReviewerAgent / review | architecture、code | ReviewerArtifacts：review_status、issues、correction_directives、code_digest |
| TesterAgent / testing | code、review | TesterArtifacts：test_summary、evidence_logs |

模型见 `dev/schemas.py`，均可通过 `model_json_schema()` 导出 JSON Schema。API contracts 是命名契约到 JSON 对象的映射；本阶段验证结构和 JSON 合法性，不声称完成 OpenAPI/JSON Schema 的全部语义校验。架构 subtasks 会校验重复 ID、悬空依赖及循环，不自动创建调度任务。

输入可直接放在 payload 中，也可以从 `context.dependencies` 取符合 Schema 的唯一产物。存在多个候选时须显式指定，例如 `{"artifact_sources": {"architecture": "design_task", "code": "code_task"}}`。所有来源必须是直接依赖；Reviewer 需要同时依赖 Architect 与 Coder，Tester 需要同时依赖 Coder 与 Reviewer。不会隐式读取全局项目或宿主机文件。

Architect/Coder/Reviewer 接收 `ArtifactGenerator`，统一调用异步 `generate(GenerationRequest)`。请求包括 role、固定 system_prompt、独立的 input_data 和 output_schema；后端必须返回单个 JSON 对象。`dev/prompts.py` 提供结构化指令，禁止把候选源码当作上级指令、执行代码或声称调用了未执行的工具。缺少生成后端时返回 BLOCKED；无效输入、非法模型输出或后端异常返回 FAILED，不自动重试。Reviewer 当前是生成后端提供的文本审查，不会在宿主机调用 AST、编译器或静态分析工具。

Coder 文件路径必须是规范的相对 POSIX 路径：拒绝绝对路径、`..`、反斜杠、控制字符及元数据/认证目录；拒绝源码与测试重名、大小写冲突及文件/目录冲突。每个文件最多 256 KiB，源码加测试总量最多 1 MiB，至少一个测试文件非空。产物只作为文本校验和传递，不写入工作区或执行；后续实际落盘执行仍须由沙箱执行器处理。

Reviewer 的 REJECTED 是成功完成审查，因此外层 AgentOutput.status 为 COMPLETED，并保留问题和修改指令。REJECTED 必须有 issues 和 correction_directives；APPROVED 不得包含 ERROR/CRITICAL 问题或修改指令。文件引用和行号会对照候选文件校验。`code_digest` 由 Agent 对源码和测试的规范 JSON 计算 SHA-256，模型不能指定它；这是版本一致性检查，不是签名或安全认证。Coder 可接收显式 previous_code 与 REJECTED review，摘要必须匹配；本阶段只生成一次修订，不自动构建重试循环。

Tester 仅调用注入的 `SandboxExecutor.run_tests(CoderArtifacts)`，不会把 shell 命令或模型客户端作为执行器。缺少 APPROVED 审查、摘要过期或未配置执行器时返回 BLOCKED 和 NOT_RUN，所有测试指标为 null。方案 B 提供下面的 Python/pytest 执行器，保留现有 DockerSandbox 隔离边界；其他语言和依赖环境仍需独立实现。

执行器回执 `SandboxTestResult` 必须包含代码摘要、execution_mode（docker/fake）、executed、run_id、退出码以及实际测试计数；未运行不能填入测量值。Tester 从计数计算 total/pass_rate（0–100，分母包含 skipped），未采集覆盖率时 coverage 为 null。只有退出码为 0、至少一项通过且没有 failed/errors/skipped 才输出 PASSED；失败、空测试集或跳过用例不会得到 100% 全通过结论。Fake-only 单元测试的证据始终标明 `execution_mode=fake`，不代表生成的候选代码真的被测试过。

验证新增 `tests/unit/test_dev_agents.py`，覆盖 Schema、Prompt、路径约束、审查拒绝语义、修订输入、执行器证据及一个不执行候选代码的 Fake DAG 契约测试。服务测试覆盖后端已配置/未配置时的 API 行为；使用现有 `scripts/validate_service.py` 在 Docker 中运行全部测试，并验证原有 client_demo/project_demo。

## 端到端软件研发团队（方案 B）

`examples/software_team_demo.py` 是可信的本地控制器，使用已登录的 Codex 订阅 CLI，真实执行 Architect → Coder → Reviewer → Tester。默认需求是普通 LRU Cache；契约位于 `tests/fixtures/software_team/requirements.txt`，固定验收测试为同目录 `acceptance.py`。架构、代码、生成测试与审查由模型产生，没有预置的实现代码或自动 Fake 回退。

前置条件为 Linux Docker、兼容的已登录 Codex CLI、已缓存的 `python:3.12-slim` 和本文“本地验证”所述 pytest Wheels。控制面虚拟环境额外安装 Pydantic，仅用于固定协议和编排逻辑：

```sh
.venv-control/bin/python -m pip install --only-binary=:all: docker==7.1.0 pydantic==2.14.2
PYTHONDONTWRITEBYTECODE=1 .venv-control/bin/python examples/software_team_demo.py
# 可选：--codex /absolute/path/to/codex --model MODEL --timeout 180 --max-retries 3
```

该命令不依赖 Compose API，也不向 API 容器挂载 Docker socket。宿主机仅执行可信控制代码、订阅 CLI 以及 Docker SDK 生命周期调用；生成代码作为文本暂存于本次证据目录，从不在宿主机导入、编译或测试。CLI 沿用已验证的禁工具调用配置，移除 API Key 环境变量、使用 ChatGPT 登录，并审计事件以拒绝命令、文件修改及 MCP 活动。复杂产物 Schema 通过 `artifact_json` 字符串传输，再由各角色 Pydantic Schema 严格校验。

`SoftwareTeamWorkflow` 复用 Project/Task/Dispatcher，记录各角色产物。审查 REJECTED 时 Tester 不运行；下一轮 Coder 获得前一版本及修改指令。若真实测试失败，Reviewer 先分析匹配代码摘要的 `test_evidence`，产生 REJECTED 和修正指令后再进入 Coder。最多 3 次修订（初始版本另计）；后端或基础设施错误不伪装成可修复代码失败。所有尝试均保留，最终失败不会被改写为成功。

`DockerTestExecutor` 目前只接受 `src/` 下的 Python 源码及 `tests/` 下的 Python 测试，不安装候选代码指定的依赖。它固定镜像 ID，在两个独立的禁网容器中分别执行生成测试和固定验收测试；固定测试不会混入生成测试的 conftest 或配置。两次运行均使用只读源码、2 CPU、6 GiB、256 PID、每条命令默认 30 秒及 24 KiB 输出上限，用完强制删除容器。pytest 从离线 Wheels 安装；可信入口关闭自动插件和 conftest，使用固定 pytest 配置。SDK 操作为同步控制调用，硬超时由 DockerSandbox 执行。

JUnit 使用实际 testcase 统计，拒绝实体声明、超大报告和符号链接。两个测试集都须有真实测试且无失败/错误/跳过，才能通过最终验收；覆盖率尚未采集时保持 null。测试报告是执行证据，不是对恶意候选代码的防篡改认证；固定验收也不构成全部行为或性能的形式化证明。

产物保存在 `sandbox-validation/software-team-*/`：

- `summary.json`：最终状态、修订次数、各轮结果、CLI 耗时、工具活动审计与容器回收状态。
- `contract.json`：需求和固定验收测试 SHA-256。
- `cli/`：每个角色的 Prompt、原始事件、stderr、响应及解析产物。
- `workflow/`：完整 Project DAG、各角色结果与耗时、最终代码文件映射。
- `sandbox/tests-*/`：候选代码、每个测试集的文件清单/哈希、安装日志、pytest stdout/stderr、JUnit、隔离配置及删除记录。

`tests/unit/test_software_team.py` 用显式 Fake 验证成功、拒绝、失败反馈修复及最大轮数，并验证 CLI JSON 传输和报告读取约束。真实 CLI/Docker 演示通过上述命令单独执行，常规单元测试不消耗模型调用。
