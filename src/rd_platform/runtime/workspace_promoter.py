"""Commit a Docker-verified candidate into a clean, unchanged Git worktree."""

import fcntl
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import tempfile

from rd_platform.agents.dev.schemas import CoderArtifacts, code_digest
from .workspace_contract import validate_repository_candidate


class PromotionError(RuntimeError):
    pass


class WorkspacePromoter:
    def __init__(self, root, *, git_runner=None):
        self.root = Path(root).resolve(strict=True)
        self.git_runner = git_runner or subprocess.run

    def git(self, *args):
        command = ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                   '-c', 'commit.gpgsign=false', *args]
        result = self.git_runner(command, cwd=self.root, capture_output=True, text=True, timeout=30,
                                 check=False)
        if result.returncode:
            raise PromotionError(f'Git {args[0]} failed: {result.stderr[:400]}')
        return result.stdout.strip()

    def snapshot(self):
        if (self.root / '.gitattributes').exists() or (self.root / '.git/info/attributes').exists():
            raise PromotionError('repository attributes must be reviewed before automatic Git operations')
        if self.git('rev-parse', '--show-toplevel') != str(self.root):
            raise PromotionError('unexpected Git worktree root')
        if self.git('status', '--porcelain', '--untracked-files=normal'):
            raise PromotionError('Git worktree must be clean before an Agent run')
        return self.git('rev-parse', 'HEAD')

    def promote(self, run_id, candidate: CoderArtifacts, *, base_head, tested_digest,
                test_summary, execution_mode):
        if not isinstance(run_id, str) or len(run_id) != 32 or any(char not in '0123456789abcdef' for char in run_id):
            raise PromotionError('invalid run ID')
        candidate = validate_repository_candidate(candidate, self.root)
        counts = test_summary.get('counts') or {}
        if (execution_mode != 'docker' or tested_digest != code_digest(candidate) or
                test_summary.get('outcome') != 'PASSED' or not test_summary.get('total') or
                counts.get('passed') != test_summary['total'] or
                any(counts.get(key) for key in ('failed', 'errors', 'skipped'))):
            raise PromotionError('candidate does not match a complete passing Docker test receipt')
        lock_path = self.root / 'data' / '.workspace-promotion.lock'
        lock_path.parent.mkdir(exist_ok=True)
        with lock_path.open('a+b') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if self.snapshot() != base_head:
                raise PromotionError('Git HEAD changed during the Agent run')
            validate_repository_candidate(candidate, self.root)
            changes = {**candidate.files, **candidate.tests}
            paths = sorted(changes)
            previous = {}
            committed = False
            try:
                for path in paths:
                    target = self.root / path
                    previous[path] = target.read_bytes() if target.exists() else None
                    target.parent.mkdir(parents=True, exist_ok=True)
                    descriptor, temporary = tempfile.mkstemp(prefix='.agent-candidate-', dir=target.parent)
                    try:
                        with os.fdopen(descriptor, 'wb') as stream:
                            stream.write(changes[path].encode('utf-8'))
                            stream.flush()
                            os.fsync(stream.fileno())
                        if previous[path] is not None:
                            os.chmod(temporary, stat.S_IMODE(target.stat().st_mode))
                        else:
                            # mkstemp defaults to 0600; the service container reads
                            # promoted source as a different, unprivileged UID.
                            os.chmod(temporary, 0o644)
                        os.replace(temporary, target)
                    finally:
                        if os.path.exists(temporary):
                            os.unlink(temporary)
                self.git('add', '--', *paths)
                staged = set(self.git('diff', '--cached', '--name-only').splitlines())
                if not staged or staged != set(paths):
                    raise PromotionError('staged changes do not match the tested candidate')
                self.git('diff', '--cached', '--check', '--', *paths)
                self.git('commit', '-m', f'feat: Auto-delivered by Agent Squad [Run {run_id}]', '--', *paths)
                committed = True
                commit = self.git('rev-parse', 'HEAD')
                clean = not self.git('status', '--porcelain', '--untracked-files=normal')
            except Exception:
                if not committed:
                    for path, content in previous.items():
                        target = self.root / path
                        if content is None:
                            target.unlink(missing_ok=True)
                        else:
                            target.write_bytes(content)
                    try:
                        self.git('reset', '--quiet', 'HEAD', '--', *paths)
                    except PromotionError:
                        pass
                raise
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return {'status': 'COMMITTED' if clean else 'COMMITTED_DIRTY', 'commit': commit, 'base_head': base_head,
                'run_id': run_id, 'files': {path: hashlib.sha256(content.encode('utf-8')).hexdigest()
                                                for path, content in changes.items()}}
