from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "static" / "index.html"
ADMIN_JS = ROOT / "static" / "js" / "admin.js"


class IdCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.buttons = {}

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        element_id = attr.get("id")
        if element_id:
            self.ids.add(element_id)
            if tag == "button":
                self.buttons[element_id] = attr


def test_system_tab_contains_odysseus_maintenance_panel():
    html = INDEX.read_text(encoding="utf-8")
    parser = IdCollector()
    parser.feed(html)

    for element_id in {
        "odysseus-maintenance-card",
        "odysseus-maintenance-refresh",
        "odysseus-maintenance-status",
        "odysseus-maintenance-summary",
        "odysseus-maintenance-detail",
        "odysseus-maintenance-reset-link",
    }:
        assert element_id in parser.ids

    assert parser.buttons["odysseus-maintenance-refresh"].get("type") == "button"
    assert "Daily main-branch intake" in html
    assert "Scoped PWA reset" in html
    assert "No chat, memory, transcript, token, or credential contents are shown here" in html


def test_admin_js_fetches_and_renders_maintenance_status():
    js = ADMIN_JS.read_text(encoding="utf-8")

    assert "/api/hermes/maintenance/status" in js
    assert "loadOdysseusMaintenanceStatus" in js
    assert "renderOdysseusMaintenanceStatus" in js
    assert "odysseus-maintenance-reset-link" in js
    assert "PWA reset" in js
