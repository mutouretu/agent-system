"""Version-one software R&D artifact contracts. Validation never executes code."""

import hashlib
import json
from graphlib import CycleError, TopologicalSorter
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator
from rd_platform.domain.task import json_object

Text = Annotated[StrictStr, Field(min_length=1, max_length=65536, pattern=r"\S")]
Digest = Annotated[StrictStr, Field(pattern=r"^[a-f0-9]{64}$")]
Count = Annotated[StrictInt, Field(ge=0)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False, hide_input_in_errors=True)


def safe_path(path: str) -> str:
    if not isinstance(path, str) or not path or len(path) > 240:
        raise ValueError("file path must be a nonempty relative POSIX path, at most 240 characters")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts) or "\\" in path or ":" in path:
        raise ValueError("absolute, traversal and noncanonical file paths are forbidden")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise ValueError("file paths must not contain control characters")
    if any(part.casefold() in (".git", ".ssh", ".aws", ".codex") for part in parts):
        raise ValueError("reserved metadata/authentication directories are forbidden")
    return path


class ModuleSpec(Contract):
    name: Text
    responsibility: Text


class ArchitectureSpec(Contract):
    modules: list[ModuleSpec] = Field(min_length=1, max_length=100)
    data_flow: list[Text] = Field(min_length=1, max_length=100)
    decisions: list[Text] = Field(default_factory=list, max_length=100)


class Subtask(Contract):
    id: Text
    description: Text
    acceptance_criteria: list[Text] = Field(min_length=1, max_length=100)
    depends_on: list[Text] = Field(default_factory=list, max_length=100)


class ArchitectArtifacts(Contract):
    architecture_spec: ArchitectureSpec
    api_contracts: dict[str, dict[str, Any]] = Field(min_length=1, max_length=100)
    subtasks: list[Subtask] = Field(min_length=1, max_length=100)

    @field_validator("api_contracts", mode="before")
    @classmethod
    def json_contracts(cls, value):
        # Structural JSON validation; full JSON Schema/OpenAPI semantics are not claimed.
        return json_object(value)

    @model_validator(mode="after")
    def valid_subtask_graph(self):
        ids = [task.id for task in self.subtasks]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate subtask ID")
        for task in self.subtasks:
            if len(set(task.depends_on)) != len(task.depends_on) or not set(task.depends_on) <= set(ids):
                raise ValueError("subtasks contain duplicate or unknown dependencies")
        try:
            tuple(TopologicalSorter({task.id: task.depends_on for task in self.subtasks}).static_order())
        except CycleError:
            raise ValueError("subtask graph contains a cycle") from None
        return self


class CoderArtifacts(Contract):
    files: dict[StrictStr, StrictStr] = Field(min_length=1, max_length=100)
    tests: dict[StrictStr, StrictStr] = Field(min_length=1, max_length=100)
    commit_message: Annotated[Text, Field(max_length=256)]

    @model_validator(mode="before")
    @classmethod
    def recover_unambiguous_formatting(cls, value):
        """Repair only metadata and misplaced test paths; never invent source/tests."""
        if not isinstance(value, dict):
            return value
        result = dict(value)
        if "commit_message" not in result:
            result["commit_message"] = "Implement requested changes"
        misplaced = {}
        for key in tuple(result):
            if not isinstance(key, str):
                continue
            path = key[1:-1] if key.startswith("`") and key.endswith("`") else key
            if path.startswith("tests/") and path.endswith(".py"):
                safe_path(path)
                misplaced[path] = result.pop(key)
        if misplaced:
            tests = result.get("tests", {})
            if not isinstance(tests, dict) or set(tests) & set(misplaced):
                raise ValueError("misplaced test path conflicts with tests mapping")
            result["tests"] = {**tests, **misplaced}
        return result

    @model_validator(mode="after")
    def valid_files(self):
        seen = set()
        size = 0
        for mapping in (self.files, self.tests):
            for path, content in mapping.items():
                safe_path(path)
                canonical = path.casefold()
                if canonical in seen:
                    raise ValueError("source/test paths overlap or differ only by case")
                seen.add(canonical)
                encoded = content.encode("utf-8")
                if len(encoded) > 256 * 1024 or "\x00" in content:
                    raise ValueError("file content must be text of at most 256 KiB without NUL")
                size += len(encoded)
        if size > 1024 * 1024:
            raise ValueError("source and tests exceed 1 MiB")
        for path in seen:
            parts = path.split("/")
            if any("/".join(parts[:index]) in seen for index in range(1, len(parts))):
                raise ValueError("a file path is also used as a directory")
        if not any(content.strip() for content in self.tests.values()):
            raise ValueError("at least one nonempty test file is required")
        return self


def code_digest(code: CoderArtifacts) -> str:
    payload = {"files": code.files, "tests": code.tests}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ReviewIssue(Contract):
    file: StrictStr | None = None
    line: Annotated[StrictInt, Field(ge=1)] | None = None
    severity: Literal["INFO", "WARNING", "ERROR", "CRITICAL"]
    message: Text
    suggestion: Text

    @model_validator(mode="after")
    def location(self):
        if self.file is not None:
            safe_path(self.file)
        if self.line is not None and self.file is None:
            raise ValueError("a line number requires a file")
        return self


class CorrectionDirective(Contract):
    file: StrictStr | None = None
    instruction: Text

    @field_validator("file")
    @classmethod
    def valid_path(cls, value):
        return safe_path(value) if value is not None else None


class ReviewDecision(Contract):
    review_status: Literal["APPROVED", "REJECTED"]
    issues: list[ReviewIssue] = Field(default_factory=list, max_length=100)
    correction_directives: list[CorrectionDirective] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def consistent_decision(self):
        if self.review_status == "REJECTED" and (not self.issues or not self.correction_directives):
            raise ValueError("REJECTED requires issues and correction directives")
        if self.review_status == "APPROVED" and (self.correction_directives or any(
            issue.severity in ("ERROR", "CRITICAL") for issue in self.issues
        )):
            raise ValueError("APPROVED cannot contain blocking issues or correction directives")
        return self


class ReviewerArtifacts(ReviewDecision):
    code_digest: Digest


class TestCounts(Contract):
    passed: Count
    failed: Count
    errors: Count
    skipped: Count

    @property
    def total(self):
        return self.passed + self.failed + self.errors + self.skipped


class SandboxTestResult(Contract):
    """Trusted executor receipt. Fake executors must explicitly mark their results."""
    code_digest: Digest
    execution_mode: Literal["docker", "fake"]
    executed: StrictBool
    run_id: Text | None = None
    exit_code: Annotated[StrictInt, Field(ge=0, le=255)] | None = None
    counts: TestCounts | None = None
    coverage: Annotated[float, Field(strict=True, ge=0, le=100)] | None = None
    logs: StrictStr = Field(default="", max_length=65536)

    @model_validator(mode="after")
    def execution_receipt(self):
        if not self.executed:
            if any(value is not None for value in (self.exit_code, self.counts, self.coverage, self.run_id)):
                raise ValueError("unexecuted tests cannot include measured results")
        elif self.exit_code is None or self.run_id is None:
            raise ValueError("executed tests require run_id and exit_code")
        if self.executed and self.exit_code == 0 and (self.counts is None or self.counts.failed or self.counts.errors):
            raise ValueError("exit code zero requires a consistent test-count report")
        return self


class TestSummary(Contract):
    outcome: Literal["NOT_RUN", "PASSED", "FAILED", "ERROR"]
    total: Count | None = None
    counts: TestCounts | None = None
    pass_rate: Annotated[float, Field(strict=True, ge=0, le=100)] | None = None
    coverage: Annotated[float, Field(strict=True, ge=0, le=100)] | None = None

    @model_validator(mode="after")
    def measured_rates(self):
        if self.outcome == "NOT_RUN" and any(value is not None for value in (self.total, self.counts, self.pass_rate, self.coverage)):
            raise ValueError("NOT_RUN cannot report test metrics")
        if self.counts is None:
            if self.total is not None or self.pass_rate is not None or self.outcome == "PASSED":
                raise ValueError("metrics require actual counts")
        else:
            expected = 100 * self.counts.passed / self.counts.total if self.counts.total else None
            if self.total != self.counts.total or self.pass_rate != expected:
                raise ValueError("test metrics do not match counts")
            if self.outcome == "PASSED" and (not self.counts.passed or self.counts.failed or self.counts.errors or self.counts.skipped):
                raise ValueError("PASSED requires at least one test and no failed/error/skipped tests")
        return self


class EvidenceLogs(Contract):
    code_digest: Digest
    execution_mode: Literal["not_run", "docker", "fake"]
    run_id: Text | None = None
    exit_code: Annotated[StrictInt, Field(ge=0, le=255)] | None = None
    logs: StrictStr = Field(default="", max_length=65536)


class TesterArtifacts(Contract):
    test_summary: TestSummary
    evidence_logs: EvidenceLogs

    @model_validator(mode="after")
    def consistent_evidence(self):
        if self.test_summary.outcome == "NOT_RUN":
            if self.evidence_logs.run_id is not None or self.evidence_logs.exit_code is not None:
                raise ValueError("NOT_RUN cannot include execution evidence")
        elif self.evidence_logs.execution_mode == "not_run" or self.evidence_logs.run_id is None or self.evidence_logs.exit_code is None:
            raise ValueError("test outcome requires executor evidence")
        if self.test_summary.outcome == "PASSED" and self.evidence_logs.exit_code != 0:
            raise ValueError("PASSED requires exit code zero")
        return self
