"""Bounded Architect -> Coder -> Reviewer -> Tester workflow, with revision evidence."""

import json
from pathlib import Path
import time

from rd_platform.agents.dev import ArchitectAgent, CoderAgent, ReviewerAgent, TesterAgent
from rd_platform.domain import Project, Task, TaskInput, TaskOutput, TaskStatus
from rd_platform.agents.dev.schemas import ArchitectArtifacts, CoderArtifacts, ReviewerArtifacts, TesterArtifacts
from .agent_registry import AgentRegistry
from .dispatcher import Dispatcher


def save(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def runtime_blocker_downgrade(review, test_summary, execution_mode):
    """A full Docker pass rebuts only build/runtime claims, never security or API defects."""
    blockers = [issue for issue in review.get("issues", []) if issue.get("severity") == "BLOCKER"]
    return (bool(blockers) and all(issue.get("blocker_type") == "BUILD_RUNTIME" for issue in blockers)
            and execution_mode == "docker" and test_summary.get("outcome") == "PASSED"
            and test_summary.get("pass_rate") == 100)


class SoftwareTeamWorkflow:
    def __init__(self, generator, executor, evidence_dir, *, max_retries=3, task_timeout=600, on_event=None):
        if type(max_retries) is not int or not 0 <= max_retries <= 3:
            raise ValueError("max_retries must be an integer in [0, 3]")
        self.directory = Path(evidence_dir)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_retries = max_retries
        self.on_event = on_event
        registry = AgentRegistry()
        for agent in (ArchitectAgent(generator), CoderAgent(generator), ReviewerAgent(generator), TesterAgent(executor)):
            registry.register(agent)
        self.dispatcher = Dispatcher(registry, task_timeout=task_timeout)

    async def run(self, requirements, *, resume_seed=None, operator_guidance=None):
        project = Project("Software R&D team", context={"requirements": requirements})
        summary = {"status": "FAILED", "max_retries": self.max_retries, "iteration_count": 0,
                   "attempts": [], "evidence_dir": str(self.directory), "project_id": project.id}
        started = time.monotonic()

        async def execute(task):
            checkpoint = next((saved for saved in (resume_seed or {}).get('tasks', []) if saved['id'] == task.id), None)
            if checkpoint is not None and task.id != resume_seed['target_task_id']:
                validators = {'architecture': ArchitectArtifacts, 'coding': CoderArtifacts,
                              'review': ReviewerArtifacts, 'testing': TesterArtifacts}
                output = checkpoint.get('output')
                if output is not None:
                    output = validators[task.capability].model_validate(output).model_dump(mode='json')
                restored = Task(task.name, task.capability, id=task.id,
                                input=TaskInput(checkpoint.get('input') or task.input.data),
                                depends_on=task.depends_on, status=TaskStatus(checkpoint['status']),
                                output=TaskOutput(output) if output is not None else None,
                                error=checkpoint.get('error'), agent_id=checkpoint.get('agent_id'),
                                logs=checkpoint.get('logs'))
                project.add_task(restored)
                save(self.directory / f"{task.id}.json", {**restored.to_dict(), 'restored': True})
                if self.on_event is not None:
                    await self.on_event('task_finished', {**restored.to_dict(), 'restored': True})
                return restored
            project.add_task(task)
            print(f"Role {task.capability}: {task.id}", flush=True)
            if self.on_event is not None:
                await self.on_event("task_started", {**task.to_dict(), "status": "RUNNING"})
            before = time.monotonic()
            await self.dispatcher.dispatch_task(task, project.context, project=project)
            save(self.directory / f"{task.id}.json", {**task.to_dict(), "elapsed_seconds": time.monotonic() - before})
            if self.on_event is not None:
                await self.on_event("task_finished", {**task.to_dict(), "elapsed_seconds": time.monotonic() - before})
            print(f"Role result: {task.id} {task.status.value}", flush=True)
            return task

        try:
            if resume_seed is not None:
                summary['resumed_from'] = resume_seed.get('parent_run_id')
                summary['resume_from_task'] = resume_seed['target_task_id']
            architect = await execute(Task("Architecture", "architecture", id="architect", input=TaskInput({"requirements": requirements})))
            if architect.status != TaskStatus.COMPLETED:
                summary["error"] = architect.error
                return summary
            previous_code = previous_review = None
            feedback = None
            for iteration in range(self.max_retries + 1):
                summary["iteration_count"] = iteration
                payload = {"instructions": requirements}
                if operator_guidance:
                    payload["operator_guidance"] = operator_guidance
                dependencies = [architect.id]
                if previous_code is not None:
                    dependencies.extend([previous_code.id, previous_review.id])
                    payload["artifact_sources"] = {"architecture": architect.id, "previous_code": previous_code.id, "review": previous_review.id}
                    if feedback is not None:
                        payload["test_evidence"] = feedback
                coder = await execute(Task("Implementation", "coding", id=f"coder-{iteration:02d}",
                                           input=TaskInput(payload), depends_on=dependencies))
                if coder.status != TaskStatus.COMPLETED:
                    summary["error"] = coder.error
                    break
                reviewer = await execute(Task("Code review", "review", id=f"reviewer-{iteration:02d}",
                                               depends_on=[architect.id, coder.id]))
                if reviewer.status != TaskStatus.COMPLETED:
                    summary["error"] = reviewer.error
                    break
                tester = await execute(Task("Sandbox tests", "testing", id=f"tester-{iteration:02d}",
                                             input=TaskInput({"diagnostic": reviewer.output.data["review_status"] == "REJECTED"}),
                                             depends_on=[coder.id, reviewer.id]))
                attempt = {"iteration": iteration, "coder": coder.id, "reviewer": reviewer.id, "tester": tester.id,
                           "review_status": reviewer.output.data["review_status"], "tester_status": tester.status.value,
                           "test_summary": tester.output.data.get("test_summary") if tester.output else None}
                summary["attempts"].append(attempt)
                runtime_claim_disproved = tester.output is not None and runtime_blocker_downgrade(
                    reviewer.output.data, tester.output.data["test_summary"],
                    tester.output.data["evidence_logs"]["execution_mode"])
                if tester.status == TaskStatus.COMPLETED and (
                    reviewer.output.data["review_status"] == "APPROVED" or runtime_claim_disproved
                ):
                    summary.update(status="PASS", final_artifacts=coder.output.data,
                                   final_test_summary=tester.output.data["test_summary"],
                                   execution_mode=tester.output.data["evidence_logs"]["execution_mode"])
                    if runtime_claim_disproved:
                        summary["review_adjudication"] = "runtime blocker downgraded after complete sandbox test pass"
                    break
                if iteration == self.max_retries:
                    summary["error"] = "revision limit reached"
                    break
                feedback = None
                if tester.status == TaskStatus.FAILED and tester.output and (
                    tester.output.data.get("test_summary", {}).get("outcome") in ("FAILED", "ERROR") and
                    tester.output.data.get("evidence_logs", {}).get("execution_mode") in ("docker", "fake")
                ):
                    feedback = tester.output.data
                elif reviewer.output.data["review_status"] == "APPROVED":
                    summary["error"] = tester.error or "test execution did not produce evidence"
                    break
                previous_code, previous_review = coder, reviewer
            return summary
        finally:
            summary["elapsed_seconds"] = time.monotonic() - started
            rates = [item["test_summary"]["pass_rate"] for item in summary["attempts"]
                     if item.get("test_summary") and item["test_summary"]["pass_rate"] is not None]
            summary["pass_rates"] = rates
            summary["monotonic_nondecreasing"] = all(b >= a for a, b in zip(rates, rates[1:])) if rates else None
            save(self.directory / "project.json", project.to_dict())
            save(self.directory / "summary.json", summary)
