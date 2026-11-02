import re
from html.parser import HTMLParser
from pathlib import Path


AGENTS_HTML = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "rd_platform"
    / "service"
    / "static"
    / "agents.html"
)


class _SidebarParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._in_sidebar = False
        self._sidebar_depth = 0
        self.sidebar_anchors = []
        self._current_anchor = None
        self._current_text = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "aside" and "sidebar" in (attributes.get("class") or "").split():
            self._in_sidebar = True
            self._sidebar_depth = 1
            return
        if self._in_sidebar and tag == "aside":
            self._sidebar_depth += 1
        if self._in_sidebar and tag == "a":
            self._current_anchor = {
                "href": attributes.get("href"),
                "class": attributes.get("class") or "",
                "text": "",
            }
            self._current_text = []

    def handle_endtag(self, tag):
        if self._in_sidebar and tag == "a" and self._current_anchor is not None:
            self._current_anchor["text"] = "".join(self._current_text).strip()
            self.sidebar_anchors.append(self._current_anchor)
            self._current_anchor = None
            self._current_text = []
        elif self._in_sidebar and tag == "aside":
            self._sidebar_depth -= 1
            if self._sidebar_depth <= 0:
                self._in_sidebar = False

    def handle_data(self, data):
        if self._in_sidebar and self._current_anchor is not None:
            self._current_text.append(data)


def _sidebar_anchors():
    parser = _SidebarParser()
    parser.feed(AGENTS_HTML.read_text(encoding="utf-8"))
    parser.close()
    return parser.sidebar_anchors


def test_project_development_anchor_targets_development_route():
    anchors = [a for a in _sidebar_anchors() if "项目开发" in a["text"]]
    assert anchors, "侧边栏须保留项目开发入口"
    assert [a["href"] for a in anchors] == ["/#development"]


def test_sidebar_has_no_generic_requirements_or_workspace_anchors():
    hrefs = [a["href"] for a in _sidebar_anchors()]
    assert "/#workspace" not in hrefs
    assert "/#requirements" not in hrefs
    assert "#requirements" not in hrefs


def test_sidebar_has_no_generic_requirements_navigation_item():
    texts = [a["text"] for a in _sidebar_anchors()]
    assert not any("需求管理" in text for text in texts)


def test_project_list_anchor_preserved():
    anchors = [a for a in _sidebar_anchors() if "项目列表" in a["text"]]
    assert [a["href"] for a in anchors] == ["/"]


def test_agent_page_structure_and_ids_preserved():
    html = AGENTS_HTML.read_text(encoding="utf-8")
    for required_id in (
        "agents-project",
        "agents-refresh",
        "agents-status",
        "agents-error",
        "agents-form",
        "agents-release",
        "agents-cards",
        "agents-save",
    ):
        assert re.search(rf'id="{required_id}"', html), required_id
    assert "/assets/app.css" in html
    assert "/assets/agents_page.css" in html
    assert "/assets/agents_page.js" in html
