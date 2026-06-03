from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "static" / "index.html"
MEMORY_JS = ROOT / "static" / "js" / "memory.js"


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


def test_memory_settings_contains_read_only_hermes_continuity_panel():
    html = INDEX.read_text(encoding="utf-8")
    parser = IdCollector()
    parser.feed(html)

    for element_id in {
        "hermes-continuity-card",
        "hermes-continuity-refresh",
        "hermes-continuity-status",
        "hermes-continuity-summary",
        "hermes-continuity-detail",
    }:
        assert element_id in parser.ids

    assert parser.buttons["hermes-continuity-refresh"].get("type") == "button"
    assert "No transcript or memory contents are shown here" in html
    assert "hermes-continuity-import" not in html


def test_memory_js_fetches_inventory_and_marks_contents_hidden():
    js = MEMORY_JS.read_text(encoding="utf-8")

    assert "/api/hermes/continuity/inventory" in js
    assert "content_returned" in js
    assert "read-only scan" in js
    assert "loadHermesContinuityInventory(true)" in js
    assert "Hermes continuity inventory failed" in js
