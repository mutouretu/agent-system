"""Package existing Docker test evidence; never execute source or rerun tests.

This trusted packaging utility parses JUnit, verifies snapshots against current
files, hashes evidence and produces a local CI/CD handoff bundle. It is not a
remote CI attestation or a cryptographic signature.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import copy
from datetime import datetime
import gzip
import hashlib
import json
from pathlib import Path
import tarfile
import tomllib
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def inspect_junit(path):
    root = ET.parse(path).getroot()
    cases = root.findall(".//testcase")
    result = {"tests": len(cases), "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    observations = []
    for case in cases:
        status = "passed"
        for element, outcome in (("error", "errors"), ("failure", "failed"), ("skipped", "skipped")):
            if case.find(element) is not None:
                status = outcome
                break
        result[status] += 1
        # Standalone controllers flatten test module paths; retain module+test IDs.
        module = case.attrib["classname"].rsplit(".", 1)[-1]
        observations.append((f"{module}::{case.attrib['name']}", status, case))
    suites = list(root.iter("testsuite"))
    for key, attribute in (("tests", "tests"), ("failed", "failures"), ("errors", "errors"), ("skipped", "skipped")):
        if sum(int(suite.attrib[attribute]) for suite in suites) != result[key]:
            raise RuntimeError(f"inconsistent JUnit counters: {path}")
    return result, observations


def verify_sources(base, chaos, probe):
    checked = {}

    def compare(snapshot, relative):
        current = ROOT / relative
        checksum = digest(snapshot)
        if not current.is_file() or digest(current) != checksum:
            raise RuntimeError(f"validated source differs from current source: {relative}")
        checked[relative] = checksum

    for name in ("src", "tests", "scripts"):
        for path in sorted((base / "workspace" / name).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.name != ".DS_Store":
                compare(path, path.relative_to(base / "workspace").as_posix())
    compare(base / "workspace/pyproject.toml", "pyproject.toml")
    compare(chaos / "workspace/test_sandbox_chaos.py", "tests/test_sandbox_chaos.py")
    for path in sorted((probe / "workspace/src").rglob("*.py")):
        compare(path, path.relative_to(probe / "workspace").as_posix())
    for name, source in (
        ("test_probe.py", "tests/unit/test_probe.py"),
        ("test_probe_evidence.py", "tests/integration/test_probe_evidence.py"),
        ("workload.py", "tests/fixtures/probing/workload.py"),
        ("benchmark.py", "tests/fixtures/probing/benchmark.py"),
    ):
        compare(probe / "workspace" / name, source)
    current = {
        path.relative_to(ROOT).as_posix()
        for name in ("src", "tests", "scripts") for path in (ROOT / name).rglob("*.py")
        if "__pycache__" not in path.parts
    }
    additional = current - checked.keys()
    if additional != {"scripts/archive_validation.py"}:
        raise RuntimeError(f"unexpected unvalidated source additions: {sorted(additional)}")
    return {
        "validated_snapshot_matches": checked,
        "packaging_only_additions_not_part_of_prior_test_execution": {
            path: digest(ROOT / path) for path in sorted(additional)
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--chaos", type=Path, required=True)
    parser.add_argument("--probe", type=Path, required=True)
    args = parser.parse_args()
    roots = {name: getattr(args, name).resolve(strict=True) for name in ("base", "chaos", "probe")}
    for path in roots.values():
        if not path.is_relative_to(ROOT / "sandbox-validation"):
            raise RuntimeError("evidence must come from this project's sandbox-validation directory")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    sources = verify_sources(**roots)
    reports = {
        "base": roots["base"] / "output/pytest.xml",
        "chaos": roots["chaos"] / "output-pytest/pytest.xml",
        "probe": roots["probe"] / "output/pytest.xml",
    }
    counts = {}
    unique = defaultdict(list)
    for label, report in reports.items():
        counts[label], observations = inspect_junit(report)
        for key, outcome, case in observations:
            unique[key].append((label, outcome, case))
    if any(item[1] in ("failed", "errors") for values in unique.values() for item in values):
        raise RuntimeError("failure/error evidence exists; refusing PASS certification")
    if any(not any(item[1] == "passed" for item in values) for values in unique.values()):
        raise RuntimeError("some tests only skipped; refusing 100% pass certification")

    output = ROOT / "releases" / f"v{version}"
    output.mkdir(parents=True, exist_ok=False)
    timestamp = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    testsuites = ET.Element("testsuites")
    suite = ET.SubElement(testsuites, "testsuite", {
        "name": "aggregated-local-docker-validation", "tests": str(len(unique)),
        "failures": "0", "errors": "0", "skipped": "0", "timestamp": timestamp,
    })
    test_index = []
    for key, observations in sorted(unique.items()):
        selected = next(item for item in observations if item[1] == "passed")
        testcase = copy.deepcopy(selected[2])
        testcase.set("classname", key.split("::", 1)[0])
        properties = ET.SubElement(testcase, "properties")
        ET.SubElement(properties, "property", {"name": "evidence_run", "value": roots[selected[0]].name})
        ET.SubElement(properties, "property", {"name": "aggregation", "value": "deduplicated historical results; not a new test run"})
        suite.append(testcase)
        test_index.append({"id": key, "status": "passed", "observations": [
            {"suite": label, "status": outcome} for label, outcome, _ in observations
        ]})
    ET.ElementTree(testsuites).write(output / "validation.junit.xml", encoding="utf-8", xml_declaration=True)
    save(output / "test-index.json", test_index)
    save(output / "source-manifest.json", sources)

    members = []
    for label, directory in roots.items():
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise RuntimeError(f"unexpected symlink in evidence: {path}")
            if not path.is_file() or path.name == ".DS_Store":
                continue
            archive_path = f"evidence/{directory.name}/{path.relative_to(directory).as_posix()}"
            members.append((path, {
                "suite": label, "archive_path": archive_path,
                "size_bytes": path.stat().st_size, "sha256": digest(path),
            }))
    archive = output / "evidence.tar.gz"
    with archive.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as bundle:
            for path, entry in members:
                info = bundle.gettarinfo(str(path), arcname=entry["archive_path"])
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                with path.open("rb") as stream:
                    bundle.addfile(info, stream)
    expected = {entry["archive_path"]: entry for _, entry in members}
    verified = set()
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle:
            if not member.isfile() or member.name not in expected or member.name in verified:
                raise RuntimeError("unexpected or duplicate archive member")
            stream = bundle.extractfile(member)
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
            if checksum != expected[member.name]["sha256"]:
                raise RuntimeError(f"archive verification failed: {member.name}")
            verified.add(member.name)
    if verified != expected.keys():
        raise RuntimeError("archive lost evidence files")
    save(output / "artifact-manifest.json", {
        "archive_sha256": digest(archive), "verified_files": len(verified),
        "files": [entry for _, entry in members],
    })
    base_skipped = {key for key, items in unique.items() if any(label == "base" and outcome == "skipped" for label, outcome, _ in items)}
    summary = {
        "status": "PASS", "version": version, "tag": f"v{version}", "generated_at": timestamp,
        "provenance": "local Docker pytest evidence aggregation; not a signed remote CI run",
        "original_evidence_directories": {key: value.relative_to(ROOT).as_posix() for key, value in roots.items()},
        "suite_results": counts,
        "raw_testcase_observations": sum(count["tests"] for count in counts.values()),
        "raw_passed_observations": sum(count["passed"] for count in counts.values()),
        "unique_tests": len(unique), "unique_passed": len(unique), "unique_failed": 0,
        "unresolved_skipped": 0, "initial_skips_resolved": len(base_skipped), "final_pass_rate": "100%",
        "corrected_breakdown": {"base_passed": 68, "additional_chaos": 5, "additional_probe_integration": 5},
        "source_snapshot_match": True, "archive_files_verified": len(verified),
        "archive_sha256": digest(archive),
        "limitations": [
            "Requested 68+8+2 categorization does not match JUnit; actual unique breakdown is 68+5+5.",
            "Probe's 17 passes include 12 unit tests already counted in the base suite.",
            "Base controller did not persist standalone stdout/stderr; original JUnit and evidence.json are retained without fabricated logs.",
            "Packaging utility and release metadata were added after the test runs; validated source hashes remain unchanged.",
            "This aggregation does not rerun tests and does not certify unrelated online CLI runs.",
        ],
    }
    save(output / "validation-summary.json", summary)
    probe_summary = json.loads((roots["probe"] / "summary.json").read_text())
    chaos = json.loads((roots["chaos"] / "input/evidence.json").read_text())
    report = f"""# v{version} 本地 Docker 测试验证报告

生成时间：{timestamp}（Asia/Shanghai）。结论：**PASS，78/78 个唯一测试通过**。

## 数据口径

| 原始运行 | 收集数 | 通过 | 失败/错误 | 跳过 |
|---|---:|---:|---:|---:|
| 基础套件 | 78 | 68 | 0 | 10 |
| 混沌套件 | 5 | 5 | 0 | 0 |
| 探针套件 | 17 | 17 | 0 | 0 |

三份报告共有 100 条测试观测，90 次通过、10 次初始跳过。按模块名和参数化测试名去重后为 78 项：**68 项基础已通过 + 5 项混沌 + 5 项探针集成**。探针的另外 12 项为基础套件已包含的单测。基础阶段跳过的 10 项均由后续专项运行覆盖；没有把 skip 当作 pass。“68 + 8 + 2”不符合原始 JUnit，故未采用。汇总 JUnit 是历史结果聚合，不表示重新执行了一次无跳过测试。

## 实际验证结果

- 系统隔离：宿主机人工哨兵及容器内 SSH 私钥路径不可读，根目录和只读挂载写入被拒绝。
- 网络：NetworkMode=none；对 8.8.8.8 的连接返回 ENETUNREACH。
- 内存：128 MiB 测试配额触发真实 cgroup OOM，退出码 137、oom_kill 计数增加。
- CPU：无限循环约 {chaos['cpu']['elapsed_seconds']:.3f} 秒超时，整个容器被删除。
- 进程：PID 达到 256 后创建返回 EAGAIN；混沌运行的 7 个容器全部记录为已删除。
- 依赖：保留原始 ModuleNotFoundError，后续健康调用成功。
- 探针：原始日志 {probe_summary['raw_log_bytes']:,} 字节，输出上下文 {probe_summary['context_bytes']:,} 字节；四层异常栈与关键变量保留。
- 性能：基线 {probe_summary['baseline_median_seconds'] * 1000:.2f} ms，探针 {probe_summary['probed_median_seconds'] * 1000:.2f} ms，中位增幅 {probe_summary['median_overhead_ratio'] * 100:.2f}%，低于预设门槛。Token 指标是 UTF-8 字节保守上界。

## 归档与追溯

- `evidence.tar.gz`：三个指定产物目录中的全部普通文件（排除 .DS_Store），含原始 JUnit、证据 JSON、已落盘日志、源码快照和离线 Wheels。
- `artifact-manifest.json`：每个归档成员的大小和 SHA-256；已逐成员回读校验，共 {len(verified)} 个文件。
- `source-manifest.json`：当前源码/测试/控制器与验证时快照的 SHA-256 对照，全部匹配；新增归档工具单独列为包装代码。
- `test-index.json`：78 个唯一测试的来源与原始状态；`validation.junit.xml` 可供 CI/CD 收集展示。
- `SHA256SUMS`：报告、清单及压缩包的校验值。在本目录执行 `shasum -a 256 -c SHA256SUMS` 可验证完整性。

原始证据目录：

{chr(10).join('- `' + value.relative_to(ROOT).as_posix() + '`' for value in roots.values())}

## 凭证边界

本报告是本地 Docker 执行凭证，不是远端 CI 平台签名或第三方认证。基础控制器未独立保存 stdout/stderr，已完整保留其 JUnit 与 evidence.json，不补造历史日志。文件哈希用于完整性核验，不等同于数字签名。Git 发布回执另外记录实际提交和标签，以避免把 Git 状态写入尚未创建的历史记录。
"""
    (output / "validation-report.md").write_text(report, encoding="utf-8")
    files = sorted(path for path in output.iterdir() if path.is_file())
    (output / "SHA256SUMS").write_text("".join(f"{digest(path)}  {path.name}\n" for path in files), encoding="utf-8")
    print(json.dumps({"status": "PASS", "report_directory": str(output), "unique_tests": len(unique), "archive_files_verified": len(verified), "archive_sha256": digest(archive)}, indent=2))


if __name__ == "__main__":
    main()
