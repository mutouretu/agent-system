# 面向资产管理的多 Agent 研发系统：技术方案与验证设计文档

**文档版本：** v0.2-Verification  
**适用对象：** Codex 自动化代码验证、架构设计评审  
**核心目标：** 在不陷入宿主机权限泥潭的前提下，建立一套基于 FastAPI + PostgreSQL (SKIP LOCKED) + OpenAI Agents SDK + Docker 沙箱隔离的轻量级、可验证的多 Agent 研发系统原型。

---

## 一、 核心设计原则（避坑指南）

1. **绝对隔离（Isolation First）：** 所有由 Agent 动态生成的代码、测试运行和未知脚本，**严禁在宿主机直接执行**。必须通过 Python `docker` SDK 丢入隔离的 Linux 容器（`--network none`）中运行。
2. **控制面与执行面解耦（Control Plane vs. Execution Plane）：**
   * **控制面（大脑）：** FastAPI + PostgreSQL 状态机，负责任务队列、租约（Lease）、版本与审批流。
   * **执行面（手）：** Docker 沙箱容器，负责提供干净的 Linux 运行环境，执行完后销毁，绝不留下孤儿进程。
3. **保持极简：** 放弃复杂的微服务架构和第三方消息中间件（如 Redis/Celery），首期直接利用 PostgreSQL 17 的 `SKIP LOCKED` 实现可靠的持久化任务队列。

---

## 二、 系统架构设计

```text
[ 开发者 / 人类负责人 ] 
       │
       ▼ (提交意图与约束)
┌─────────────────────────────────────────────────────────┐
│ FastAPI 控制面 (Host / Docker Compose)                  │
│  - 任务状态机 (Draft -> Ready -> Running -> Succeeded)  │
│  - 租约与并发控制器 (SKIP LOCKED 队列)                   │
└──────────────────────────┬──────────────────────────────┘
                           │ 
                           ▼ (派发任务/传递代码 Commit)
┌─────────────────────────────────────────────────────────┐
│ 隔离执行 Worker (Docker Sandbox Manager)                │
│  - 动态拉起安全容器 (Read-only rootfs, tmpfs /tmp)      │
│  - 运行 OpenAI Agents SDK 循环                          │
└──────────────────────────┬──────────────────────────────┘
                           │
                           ▼ (回传结构化结果、测试日志、Git Diff)
                     [ PostgreSQL ]
```

---

## 三、 数据库与核心数据模型 (PostgreSQL 17)

系统依赖 PostgreSQL 存储任务状态，确保服务重启后任务不丢失。核心表设计如下：

```sql
-- 1. 项目与版本表
CREATE TABLE projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- 2. 任务队列主表（带租约与锁机制）
CREATE TABLE tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID REFERENCES projects(id),
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready', -- draft, ready, running, succeeded, failed, cancelled
    priority INT DEFAULT 0,
    available_at TIMESTAMPTZ DEFAULT now(),
    
    -- 租约机制字段 (防止多 Worker 冲突)
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    lease_generation INT DEFAULT 0,
    
    -- 版本与输入
    git_commit TEXT NOT NULL,
    input_payload JSONB,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- 3. 运行实例与产物表
CREATE TABLE task_attempts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID REFERENCES tasks(id),
    attempt_no INT NOT NULL,
    status TEXT NOT NULL,
    exit_code INT,
    artifact_refs JSONB,
    created_at TIMESTAMPTZ DEFAULT now()
);
```

---

## 四、 沙箱隔离执行器设计 (`DockerSandbox`)

为了彻底解决宿主机权限和孤儿进程问题，Worker 通过以下 Python 代码片段来管理沙箱生命周期：

```python
import docker
from typing import Tuple

class DockerSandbox:
    def __init__(self, image_name: str = "python:3.12-slim"):
        self.client = docker.from_env()
        self.image_name = image_name

    def run_command_in_sandbox(self, code_dir: str, command: str, timeout_sec: int = 300) -> Tuple[int, str, str]:
        """
        在高度隔离的 Docker 容器中运行命令
        - 禁用网络 (--network none)
        - 限制内存与 CPU
        - 自动清理容器，杜绝宿主机孤儿进程
        """
        container = None
        try:
            container = self.client.containers.run(
                image=self.image_name,
                command=f"bash -c '{command}'",
                volumes={code_dir: {'bind': '/workspace', 'mode': 'rw'}},
                working_dir='/workspace',
                network_mode='none',
                mem_limit='4g',
                cpu_quota=200000, # 限制 2 核
                detach=True,
                remove=False
            )
            
            result = container.wait(timeout=timeout_sec)
            exit_code = result.get('StatusCode', -1)
            
            logs = container.logs(stdout=True, stderr=True).decode('utf-8')
            return exit_code, logs, ""
            
        except Exception as e:
            return -1, "", str(e)
        finally:
            if container:
                try:
                    container.remove(force=True)
                except:
                    pass
```

---

## 五、 Codex 验证任务清单 (Milestone 验证步骤)

请将此文档交由 Codex 执行以下分步验证。每一步通过后方可进入下一步：

### 阶段 1：基础设施与数据库连通性验证
* [ ] 验证 `docker-compose.yaml` 是否能成功拉起 PostgreSQL 17。
* [ ] 验证 Alembic 迁移脚本是否能成功建立 `projects`, `tasks`, `task_attempts` 表。

### 阶段 2：持久化队列与并发锁（SKIP LOCKED）验证
* [ ] 编写单元测试模拟多线程/多 Worker 并发从 `tasks` 表领取任务。
* [ ] 验证 `FOR UPDATE SKIP LOCKED` 逻辑是否能保证同一个任务绝不会被两个 Worker 同时领走。

### 阶段 3：Docker 沙箱隔离执行验证
* [ ] 编写单元测试验证 `DockerSandbox` 类：
  * 传入一段合法的 Python 测试代码，验证能否在容器内正确执行并返回 `exit_code = 0`。
  * 传入一段死循环代码或恶意破坏脚本，验证设置超时或销毁容器（`force=True`）后，宿主机是否毫发无伤且无孤儿进程残留。

### 阶段 4：OpenAI Agents SDK 基础接线验证
* [ ] 在安全配置下，验证 SDK 的初始化 (`initialize`) 和基础 Tool Calling 流程是否能在不污染宿主机认证目录的前提下跑通。

---