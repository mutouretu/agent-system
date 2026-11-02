"""UI tasks need complete source for files they may have to replace."""

from scripts.workbench_worker import repository_context


def test_ui_context_prioritizes_existing_page_and_api(tmp_path):
    files = {
        'src/rd_platform/service/static/index.html': '<html>existing sidebar and project UI</html>',
        'src/rd_platform/service/api.py': 'def create_app():\n    return app\n\napp = create_app()\n',
        'src/rd_platform/service/static/agent_config.js': 'const roleConfig = true;',
        'src/rd_platform/service/static/app.css': ':root { --bg: #080d15; }',
        'tests/service/test_workbench.py': 'agent page ' * 300,
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    context = repository_context(tmp_path, '新增 Agent 管理页面和侧边栏', max_bytes=6000)
    assert context.index('--- src/rd_platform/service/static/index.html ---') < context.index(
        '--- src/rd_platform/service/api.py ---')
    assert 'existing sidebar and project UI</html>' in context
    assert 'app = create_app()' in context
    assert '--bg: #080d15' in context
