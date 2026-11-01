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
