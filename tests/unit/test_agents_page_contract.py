"""Structural contract tests for the standalone Agent management page."""

from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "src" / "rd_platform" / "service" / "static"


def read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def test_page_loads_dedicated_resources_only():
    text = read("agents.html")
    assert '<link rel="stylesheet" href="/assets/app.css">' in text
    assert '/assets/agents_page.css' in text
    assert '/assets/agents_page.js' in text
    assert 'app.js' not in text
    assert 'studio.js' not in text
    assert 'agent_config.js' not in text
    assert 'onclick=' not in text


def test_page_marks_active_navigation_and_returns_to_console():
    text = read("agents.html")
    assert 'aria-current="page"' in text
    assert 'href="/agents"' in text
    assert 'href="/"' in text


def test_page_has_project_selector_and_roles_in_order():
    html = read("agents.html")
    js = read("agents_page.js")
    assert 'id="agents-project"' in html
    assert 'id="agents-save"' in html
    assert "const ROLE_ORDER = ['ARCHITECT', 'CODER', 'REVIEWER', 'TESTER']" in js
    for role in ("ARCHITECT", "CODER", "REVIEWER", "TESTER"):
        assert role in js
    assert "'sandbox'" in js


def test_page_exposes_no_secret_input():
    html = (read("agents.html") + read("agents_page.js")).lower()
    assert "api key" in html or "密钥" in html
    assert 'type="password"' not in html
    assert 'localstorage' not in html
    assert 'sessionstorage' not in html
    assert 'indexeddb' not in html


def test_page_handles_conflict_without_auto_retry():
    js = read("agents_page.js")
    assert "response.status === 409" in js
    assert "state.phase = 'conflict'" in js
    assert "'PUT'" in js
    # Only one PUT call site: no automatic retry path.
    assert js.count("method: 'PUT'") == 1


def test_reload_requires_confirmation_when_dirty_or_conflict():
    js = read("agents_page.js")
    assert "window.confirm(MESSAGES.reloadDiscard)" in js
    assert "state.dirty || state.phase === 'conflict'" in js


def test_save_requires_valid_integer_revision():
    js = read("agents_page.js")
    assert "Number.isInteger(state.revision)" in js
    assert "Number.isInteger(data.revision)" in js
