"""Exercise the repository overlay in Docker without writing generated code to Git."""

import asyncio
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from rd_platform.agents.dev.schemas import CoderArtifacts
from rd_platform.runtime.test_executor import DockerTestExecutor


def main():
    directory = ROOT / 'sandbox-validation' / f'repository-overlay-{uuid.uuid4().hex}'
    directory.mkdir(parents=True)
    config = directory / 'docker-config'
    config.mkdir()
    os.environ['DOCKER_CONFIG'] = str(config)
    candidate = CoderArtifacts(
        files={'src/rd_platform/_promotion_probe.py': 'def probe():\n    return 7\n'},
        tests={'tests/unit/test_promotion_probe.py':
               'from rd_platform._promotion_probe import probe\n\ndef test_probe():\n    assert probe() == 7\n'},
        commit_message='verify repository overlay')
    executor = DockerTestExecutor(directory / 'sandbox', ROOT / 'sandbox-validation/wheels',
        repository_root=ROOT, image='rd-platform-service:0.3.0-dev', timeout=120)
    try:
        result = asyncio.run(executor.run_tests(candidate))
        summary = {'status': 'PASS' if result.exit_code == 0 and result.counts.passed > 0 and
                   not (result.counts.failed or result.counts.errors or result.counts.skipped) else 'FAILED',
                   'test_summary': result.model_dump(),
                   'all_containers_removed': all(job['container_removed'] for run in executor.runs for job in run['jobs'])}
    except Exception as exc:
        summary = {'status': 'FAILED', 'error': f'{type(exc).__name__}: {exc}',
                   'all_containers_removed': all(job['container_removed'] for run in executor.runs for job in run['jobs'])}
    (directory / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    print(f'Evidence directory: {directory}')
    print(json.dumps({key: value for key, value in summary.items() if key != 'test_summary'}, ensure_ascii=False))
    return 0 if summary['status'] == 'PASS' and summary['all_containers_removed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
