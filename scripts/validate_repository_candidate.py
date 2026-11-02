"""Exercise the repository overlay in Docker without writing generated code to Git."""

import asyncio
import argparse
import json
import os
from pathlib import Path
import sys
import uuid
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from rd_platform.agents.dev.schemas import CoderArtifacts
from rd_platform.runtime.test_executor import DockerTestExecutor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id')
    parser.add_argument('--coder-task')
    args = parser.parse_args()
    if bool(args.run_id) != bool(args.coder_task):
        parser.error('--run-id and --coder-task must be used together')
    directory = ROOT / 'sandbox-validation' / f'repository-overlay-{uuid.uuid4().hex}'
    directory.mkdir(parents=True)
    config = directory / 'docker-config'
    config.mkdir()
    os.environ['DOCKER_CONFIG'] = str(config)
    if args.run_id:
        if len(args.run_id) != 32 or any(char not in '0123456789abcdef' for char in args.run_id):
            parser.error('run ID must be 32 lowercase hex characters')
        opener = build_opener(ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:8000/v1/runs/{args.run_id}', timeout=10) as response:
            run = json.load(response)
        task = next((item for item in run['tasks'] if item['id'] == args.coder_task and item['capability'] == 'coding'), None)
        if task is None or task.get('status') != 'COMPLETED':
            parser.error('selected completed Coder task is missing')
        candidate = CoderArtifacts.model_validate(task['output'])
    else:
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
                   'source_run_id': args.run_id, 'source_task_id': args.coder_task,
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
