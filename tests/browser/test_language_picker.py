from __future__ import annotations

import os
import re

import pytest
from language_controls import choose_language
from playwright.sync_api import expect
from test_workspace_picker import gui as gui
from test_workspace_picker import page as page

pytestmark = pytest.mark.skipif(
    os.environ.get("DAGOBERD_BROWSER_TESTS") != "1",
    reason="Chromium is installed only in the dedicated GUI test job",
)

NAMES = {"en": "English", "de": "Deutsch", "es": "Español"}


def open_gui(page, gui):
    base, entries = gui
    page.goto(base + f"?workspace={entries[0]['id']}#t=picker-token")
    expect(page.locator(".kpi-grid")).to_be_visible()
    return page.locator("#language-switcher"), page.locator("#language-menu")


@pytest.mark.parametrize("gui", ["duplicates"], indirect=True)
@pytest.mark.parametrize("width", [320, 800, 1280])
@pytest.mark.parametrize("language", ["en", "de", "es"])
def test_language_flags_menu_layout_and_accessible_names(page, gui, tmp_path, width, language):
    page.set_viewport_size({"width": width, "height": 700})
    page.add_init_script(f"localStorage.setItem('dagoberd.gui-language', '{language}')")
    button, menu = open_gui(page, gui)
    expect(button).to_have_text(language.upper() + "⌄")
    expect(button).to_have_accessible_name(re.compile(NAMES[language]))
    expect(button).to_have_attribute("aria-haspopup", "menu")
    expect(button.locator("svg")).to_have_attribute("aria-hidden", "true")
    assert page.locator("select#language").count() == 0
    button.click()
    expect(button).to_have_attribute("aria-expanded", "true")
    expect(menu.get_by_role("menuitemradio", checked=True)).to_have_text(NAMES[language] + "✓")
    for code, name in NAMES.items():
        item = menu.locator(f'[data-language="{code}"]')
        expect(item).to_have_accessible_name(name)
        expect(item).to_have_attribute("lang", code)
        expect(item.locator("svg")).to_have_attribute("viewBox", "0 0 24 16")
        expect(item.locator("svg")).to_have_attribute("aria-hidden", "true")
        assert item.bounding_box()["height"] >= 44
    assert menu.locator('[data-language="en"] svg path').count() == 4
    assert menu.locator('[data-language="de"] rect').count() == 3
    assert menu.locator('[data-language="es"] rect').count() == 2
    bounds = menu.bounding_box()
    assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width
    assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= 700
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / "language-menu.png"))


def test_keyboard_selection_dismissal_and_focus(page, gui):
    button, menu = open_gui(page, gui)
    button.focus()
    page.keyboard.press("Enter")
    expect(menu.locator('[data-language="en"]')).to_be_focused()
    page.keyboard.press("End")
    expect(menu.locator('[data-language="es"]')).to_be_focused()
    page.keyboard.press("ArrowDown")
    expect(menu.locator('[data-language="en"]')).to_be_focused()
    page.keyboard.press("ArrowUp")
    expect(menu.locator('[data-language="es"]')).to_be_focused()
    page.keyboard.press("Home")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Space")
    expect(page.locator("html")).to_have_attribute("lang", "de")
    expect(menu).not_to_be_visible()
    expect(button).to_be_focused()
    expect(button).to_have_attribute("aria-expanded", "false")
    page.keyboard.press("ArrowUp")
    expect(menu.locator('[data-language="es"]')).to_be_focused()
    page.keyboard.press("Escape")
    expect(button).to_be_focused()
    expect(menu).not_to_be_visible()
    page.keyboard.press("Space")
    expect(menu.locator('[data-language="de"]')).to_be_focused()
    page.keyboard.press("Shift+Tab")
    expect(menu).not_to_be_visible()
    expect(page.locator('[data-tab="classification"]')).to_be_focused()
    button.focus()
    page.keyboard.press("ArrowDown")
    expect(menu.locator('[data-language="en"]')).to_be_focused()
    page.keyboard.press("Tab")
    expect(menu).not_to_be_visible()
    expect(button).not_to_be_focused()


def test_language_changes_keep_drafts_url_and_requests_and_persist(page, gui):
    button, menu = open_gui(page, gui)
    page.locator('[data-tab="admin"]').click()
    draft = page.locator(".toolbar input[placeholder]").first
    draft.fill("Synthetic unsaved tag")
    original_url = page.url
    navigations, calls = [], []
    page.on("framenavigated", lambda frame: navigations.append(frame.url))
    page.on("request", lambda request: calls.append(request.url))
    for language in ["de", "es", "en", "es"]:
        choose_language(page, language)
        expect(page.locator("html")).to_have_attribute("lang", language)
        expect(draft).to_have_value("Synthetic unsaved tag")
        assert page.url == original_url
    assert navigations == [] and calls == []
    assert page.evaluate("localStorage.getItem('dagoberd.gui-language')") == "es"
    page.reload()
    expect(button).to_contain_text("ES")
    expect(page.locator("html")).to_have_attribute("lang", "es")
    button.click()
    page.locator(".brand").click()
    expect(menu).not_to_be_visible()
    expect(button).to_have_attribute("aria-expanded", "false")


@pytest.mark.parametrize("stored", [None, "invalid"])
def test_english_default_and_unavailable_browser_storage(page, gui, stored):
    if stored:
        page.add_init_script(f"localStorage.setItem('dagoberd.gui-language', '{stored}')")
    else:
        page.add_init_script("""Object.defineProperty(window, 'localStorage', {
            get() { throw new Error('Storage unavailable'); }
        });""")
    button, _ = open_gui(page, gui)
    expect(button).to_contain_text("EN")
    choose_language(page, "de")
    expect(button).to_contain_text("DE")
    expect(page.locator("html")).to_have_attribute("lang", "de")


@pytest.mark.parametrize("gui", ["empty"], indirect=True)
def test_language_menu_works_before_workspace_selection(page, gui):
    base, _ = gui
    page.goto(base + "#t=picker-token")
    expect(page.locator("main h1")).to_have_text("Choose workspace")
    choose_language(page, "es")
    expect(page.locator("main h1")).to_have_text("Elegir espacio de trabajo")
    expect(page.locator("#account-scope")).to_be_disabled()
