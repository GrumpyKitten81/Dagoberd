from __future__ import annotations

import json
import re

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from dagoberd.gui.app import _static_asset_version, create_app
from dagoberd.reporting.analysis_html import render_analysis_html
from dagoberd.ui import report_styles, styles_directory


def _color(name: str) -> list[float]:
    css = (styles_directory() / "tokens.css").read_text(encoding="utf-8")
    value = re.search(rf"--{name}: ([^;]+);", css).group(1)
    if value.startswith("var("):
        return _color(value[6:-1])
    return [int(value[index : index + 2], 16) / 255 for index in (1, 3, 5)]


def _luminance(name: str) -> float:
    channels = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in _color(name)
    ]
    return sum(
        value * weight for value, weight in zip(channels, (0.2126, 0.7152, 0.0722), strict=True)
    )


@pytest.mark.parametrize(
    "foreground,background,minimum",
    [
        ("text", "canvas", 4.5),
        ("text", "surface", 4.5),
        ("muted", "surface-muted", 4.5),
        ("on-accent", "accent", 4.5),
        ("on-accent", "accent-strong", 4.5),
        ("positive", "positive-soft", 4.5),
        ("negative", "negative-soft", 4.5),
        ("warning", "warning-soft", 4.5),
        ("accent", "surface", 3),
        ("border-strong", "surface", 3),
    ],
)
def test_design_palette_contrast(foreground: str, background: str, minimum: float):
    light, dark = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    assert (light + 0.05) / (dark + 0.05) >= minimum


def test_design_assets_are_versioned_and_loaded_before_view_styles(tmp_path):
    client = TestClient(create_app(None, "synthetic-token"))
    index = client.get("/")
    links = re.findall(r'href="([^"]+\.css)"', index.text)
    assert [path.rsplit("/", 1)[1] for path in links[:3]] == [
        "tokens.css",
        "components.css",
        "app.css",
    ]
    for name, url in zip(("tokens.css", "components.css"), links[:2], strict=True):
        response = client.get(url)
        assert response.status_code == 200
        assert response.text == (styles_directory() / name).read_text(encoding="utf-8")
        assert "immutable" in response.headers["cache-control"]
    gui = tmp_path / "gui"
    design = tmp_path / "design"
    gui.mkdir()
    design.mkdir()
    (gui / "app.css").write_text(".app {}", encoding="utf-8")
    token_file = design / "tokens.css"
    token_file.write_text(":root {--accent: #0b6b63;}", encoding="utf-8")
    before = _static_asset_version(gui, design)
    token_file.write_text(":root {--accent: #08554f;}", encoding="utf-8")
    assert _static_asset_version(gui, design) != before


def test_analysis_html_embeds_shared_styles_and_preserves_safe_json():
    malicious = '</script><img src=x onerror="alert(1)">'
    result = {"synthetic": malicious}
    content = render_analysis_html(result, {"Synthetic table": pd.DataFrame({"Text": [malicious]})})
    assert report_styles() in content
    assert '<script id="dagoberd-analysis" type="application/json">' in content
    embedded = re.search(r'type="application/json">(.*?)</script>', content).group(1)
    assert json.loads(embedded.replace("<\\/", "</")) == result
    assert "&lt;img" in content
    assert malicious not in content
    assert "<link" not in content
    assert "@import" not in content
