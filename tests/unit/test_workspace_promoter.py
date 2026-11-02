"""Promotion tests use a fake Git transport; no candidate code is executed."""

from pathlib import Path
from types import SimpleNamespace
import stat

import pytest

from rd_platform.agents.dev.schemas import CoderArtifacts, code_digest
from rd_platform.runtime.workspace_contract import validate_repository_candidate
from rd_platform.runtime.workspace_promoter import PromotionError, WorkspacePromoter


class FakeGit:
    def __init__(self, root):
        self.root = root
        self.head = 'a' * 40
        self.staged = []
        self.commands = []
        self.fail_commit = False
        self.dirty = False

    def __call__(self, command, **kwargs):
        assert command[:2] == ['git', '-c']
        assert 'core.hooksPath=/dev/null' in command
        assert kwargs['cwd'] == self.root and kwargs['timeout'] == 30
        args = command[7:]
        self.commands.append(args)
        operation = args[0]
        if operation == 'rev-parse':
            output = str(self.root) if args[1] == '--show-toplevel' else self.head
        elif operation == 'status':
            output = ' M unrelated.py' if self.dirty else ''
        elif operation == 'add':
            self.staged = args[2:]
            output = ''
        elif operation == 'diff':
            output = '\n'.join(self.staged) if '--name-only' in args else ''
        elif operation == 'commit':
            if self.fail_commit:
                return SimpleNamespace(returncode=1, stdout='', stderr='commit rejected')
            self.head = 'b' * 40
            output = ''
        elif operation == 'reset':
            self.staged = []
            output = ''
        else:
            raise AssertionError(args)
        return SimpleNamespace(returncode=0, stdout=output, stderr='')


@pytest.fixture
def repository(tmp_path):
    (tmp_path / '.git').mkdir()
    source = tmp_path / 'src/rd_platform/service/feature.py'
    source.parent.mkdir(parents=True)
    source.write_text('OLD = True\n')
    (tmp_path / 'data').mkdir()
    return tmp_path


def candidate():
    return CoderArtifacts(files={'src/rd_platform/service/feature.py': 'NEW = True\n'},
                          tests={'tests/service/test_new_feature.py': 'def test_new(): assert True\n'},
                          commit_message='feature')


def receipt(code):
    return {'base_head': 'a' * 40, 'tested_digest': code_digest(code),
            'test_summary': {'outcome': 'PASSED', 'total': 1,
                             'counts': {'passed': 1, 'failed': 0, 'errors': 0, 'skipped': 0}},
            'execution_mode': 'docker'}


def test_promoter_commits_only_verified_candidate(repository):
    transport = FakeGit(repository)
    promoter = WorkspacePromoter(repository, git_runner=transport)
    code = candidate()
    assert promoter.snapshot() == 'a' * 40
    result = promoter.promote('1' * 32, code, **receipt(code))
    assert result['status'] == 'COMMITTED' and result['commit'] == 'b' * 40
    assert (repository / 'src/rd_platform/service/feature.py').read_text() == 'NEW = True\n'
    assert (repository / 'tests/service/test_new_feature.py').read_text().startswith('def test_new')
    assert stat.S_IMODE((repository / 'tests/service/test_new_feature.py').stat().st_mode) == 0o644
    assert set(transport.staged) == set(result['files'])
    assert next(args for args in transport.commands if args[0] == 'commit')[2].startswith('feat: Auto-delivered')


def test_promoter_rejects_unverified_or_dirty_candidate(repository):
    transport = FakeGit(repository)
    promoter = WorkspacePromoter(repository, git_runner=transport)
    code = candidate()
    with pytest.raises(PromotionError, match='Docker test receipt'):
        promoter.promote('1' * 32, code, **{**receipt(code), 'tested_digest': '0' * 64})
    transport.dirty = True
    with pytest.raises(PromotionError, match='clean'):
        promoter.promote('1' * 32, code, **receipt(code))
    assert (repository / 'src/rd_platform/service/feature.py').read_text() == 'OLD = True\n'


def test_promoter_restores_files_if_git_commit_fails(repository):
    transport = FakeGit(repository)
    transport.fail_commit = True
    code = candidate()
    with pytest.raises(PromotionError, match='Git commit failed'):
        WorkspacePromoter(repository, git_runner=transport).promote('1' * 32, code, **receipt(code))
    assert (repository / 'src/rd_platform/service/feature.py').read_text() == 'OLD = True\n'
    assert not (repository / 'tests/service/test_new_feature.py').exists()
    assert any(args[0] == 'reset' for args in transport.commands)


def test_repository_contract_blocks_escape_and_existing_test(repository):
    code = candidate()
    existing = repository / 'tests/service/test_new_feature.py'
    existing.parent.mkdir(parents=True)
    existing.write_text('def test_old(): assert True\n')
    with pytest.raises(ValueError, match='existing test'):
        validate_repository_candidate(code, repository)
    bad = CoderArtifacts(files={'src/other.py': 'x = 1\n'}, tests={'tests/service/test_extra.py': 'def test_x(): assert True\n'},
                         commit_message='bad')
    with pytest.raises(ValueError, match='allowed source'):
        validate_repository_candidate(bad, repository)
