"""Paths accepted for automatic repository delivery; this module never runs code."""

from pathlib import Path

from rd_platform.agents.dev.schemas import CoderArtifacts, safe_path


SOURCE_PREFIX = 'src/rd_platform/'
TEST_PREFIXES = ('tests/unit/', 'tests/service/')
SOURCE_SUFFIXES = ('.py', '.js', '.html', '.css')


def validate_repository_candidate(code: CoderArtifacts, root: Path) -> CoderArtifacts:
    """Reject escapes, protected runtime edits and changes to existing tests."""
    code = CoderArtifacts.model_validate(code.model_dump())
    root = Path(root).resolve(strict=True)
    if not (root / '.git').exists():
        raise ValueError('repository delivery requires a Git worktree')
    for path in code.files:
        safe_path(path)
        if not path.startswith(SOURCE_PREFIX) or not path.endswith(SOURCE_SUFFIXES):
            raise ValueError(f'not an allowed source path: {path}')
        if path.startswith('src/rd_platform/runtime/'):
            raise ValueError('sandbox runtime cannot be delivered automatically')
    for path in code.tests:
        safe_path(path)
        if not path.startswith(TEST_PREFIXES) or not path.endswith('.py'):
            raise ValueError(f'not an allowed test path: {path}')
        if (root / path).exists():
            raise ValueError(f'existing test cannot be overwritten automatically: {path}')
    for path in (*code.files, *code.tests):
        target = root / path
        if not target.resolve(strict=False).is_relative_to(root):
            raise ValueError(f'path escapes repository: {path}')
        ancestors = []
        for parent in target.parents:
            if parent == root:
                break
            ancestors.append(parent)
        if target.is_symlink() or any(parent.is_symlink() for parent in ancestors):
            raise ValueError(f'symlink path cannot be delivered: {path}')
        if target.exists() and not target.is_file():
            raise ValueError(f'target is not a regular file: {path}')
    return code
