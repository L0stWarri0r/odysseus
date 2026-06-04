from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "static" / "index.html"
ADMIN_JS = ROOT / "static" / "js" / "admin.js"
SW_RESET_JS = ROOT / "static" / "js" / "swReset.js"


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
        "odysseus-maintenance-reset-button",
        "odysseus-maintenance-reset-status",
    }:
        assert element_id in parser.ids

    assert parser.buttons["odysseus-maintenance-refresh"].get("type") == "button"
    assert parser.buttons["odysseus-maintenance-reset-button"].get("type") == "button"
    assert "Daily main-branch intake" in html
    assert "Scoped PWA reset" in html
    assert "Reset Odysseus now" in html
    assert "No chat, memory, transcript, token, or credential contents are shown here" in html


def test_admin_js_fetches_and_renders_maintenance_status():
    js = ADMIN_JS.read_text(encoding="utf-8")

    assert "/api/hermes/maintenance/status" in js
    assert "loadOdysseusMaintenanceStatus" in js
    assert "renderOdysseusMaintenanceStatus" in js
    assert "odysseus-maintenance-reset-link" in js
    assert "PWA reset" in js


def test_admin_js_reset_button_runs_scoped_reset_helper():
    js = ADMIN_JS.read_text(encoding="utf-8")

    assert "odysseus-maintenance-reset-button" in js
    assert "odysseus-maintenance-reset-status" in js
    assert "import('/static/js/swReset.js')" in js
    assert "resetOdysseusLocalCache" in js
    assert "cookies" in js
    assert "localStorage" in js


def test_sw_reset_helper_is_reusable_and_scoped():
    js = SW_RESET_JS.read_text(encoding="utf-8")

    assert "export async function resetOdysseusLocalCache" in js
    assert "const cachePrefix = options.cachePrefix || 'odysseus-'" in js
    assert "key.startsWith(cachePrefix)" in js
    assert "new URL(url).pathname === swPath" in js
    assert "localStorage.clear" not in js
    assert "document.cookie" not in js
