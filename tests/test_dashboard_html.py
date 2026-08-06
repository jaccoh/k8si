"""Smoke checks for k8si/ui/dashboard.html's inline <style> block."""

import re
from pathlib import Path

DASHBOARD_HTML = Path(__file__).parent.parent / "k8si" / "ui" / "dashboard.html"


def _extract_style_block() -> str:
    html = DASHBOARD_HTML.read_text()
    match = re.search(r"<style>(.*?)</style>", html, re.DOTALL)
    assert match, "dashboard.html has no <style> block"
    return match.group(1)


def test_style_block_braces_are_balanced():
    css = _extract_style_block()
    opens = css.count("{")
    closes = css.count("}")
    assert opens == closes, (
        f"unbalanced braces in dashboard.html <style> block: {opens} '{{' vs {closes} '}}' "
        "— a missing '}' silently nests the following rules inside the previous selector"
    )
