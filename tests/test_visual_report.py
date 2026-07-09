from bs4 import BeautifulSoup

from src.visual_report import generate_visual_report


def test_visual_report_toc_links_match_rendered_heading_ids():
    report = """
# Automated Crypto Trading Bot Strategies

### **1.0 Introduction & Research Scope**

Intro body.

### **2.0 Determining the "Best" Configuration**

Configuration body.
"""

    html = generate_visual_report(
        "crypto bot strategies",
        report,
        sources=[],
        stats={},
        session_id="rp-test",
    )
    soup = BeautifulSoup(html, "html.parser")

    links = soup.select(".toc-sidebar nav a")
    assert [link.get_text(strip=True) for link in links] == [
        "1.0 Introduction & Research Scope",
        '2.0 Determining the "Best" Configuration',
    ]

    for link in links:
        target_id = link["href"].removeprefix("#")
        target = soup.find(id=target_id)
        assert target is not None
        assert target.name in {"h2", "h3"}


def test_visual_report_sanitizes_raw_markdown_html_and_source_urls():
    html = generate_visual_report(
        "unsafe report",
        """
## Findings

<script>alert(1)</script>
<iframe srcdoc="<script>alert(2)</script>"></iframe>
<svg><script>alert(3)</script></svg>
<img src="javascript:alert(4)" onerror="alert(5)">
<a href="javascript:alert(6)" onclick="alert(7)">bad link</a>
""",
        sources=[
            {"url": "javascript:alert(8)", "title": "Bad Source"},
            {"url": "https://example.com/path", "title": "Good Source"},
        ],
        stats={},
        session_id="rp-test",
    )
    soup = BeautifulSoup(html, "html.parser")
    content = soup.select_one("main.content")

    assert content is not None
    assert content.find("script") is None
    assert content.find("iframe") is None
    assert content.find("svg") is None

    bad_img = content.find("img")
    assert bad_img is not None
    assert "onerror" not in bad_img.attrs
    assert "src" not in bad_img.attrs

    bad_link = content.find("a", string="bad link")
    assert bad_link is not None
    assert "href" not in bad_link.attrs
    assert "onclick" not in bad_link.attrs

    assert not content.select('.sources-list a[href^="javascript:"]')
    good_source = content.select_one('.sources-list a[href="https://example.com/path"]')
    assert good_source is not None
