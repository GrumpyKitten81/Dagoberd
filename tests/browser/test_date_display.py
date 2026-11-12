from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from language_controls import choose_language
from playwright.sync_api import expect, sync_playwright
from test_design_system import design_workspace as design_workspace
from test_workspace_picker import gui as gui

pytestmark = pytest.mark.skipif(
    os.environ.get("DAGOBERD_BROWSER_TESTS") != "1",
    reason="Chromium is installed only in the dedicated GUI test job",
)


@pytest.fixture
def page(request):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(locale="en-US", timezone_id=getattr(request, "param", "UTC"))
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        yield page
        browser.close()
        assert errors == []


def configure_metadata(page, settings):
    def respond(route):
        payload = route.fetch().json()
        payload["data"]["display"] = settings
        route.fulfill(json=payload)

    page.route("**/api/meta", respond)


def open_transactions(page, design_workspace):
    base, _, _, account = design_workspace
    page.goto(base + f"?tab=transactions&account={account}#t=design-token")
    expect(page.locator("#transaction-body tr")).to_have_count(2)


def evaluate_dates(page, values, settings=None, options=None):
    return page.evaluate(
        """async ({values, settings, options}) => {
            const base = document.querySelector('link[href$="/app.css"]').href;
            const {configureDateDisplay} = await import(new URL('date-display.js', base));
            const {date} = await import(new URL('i18n.js', base));
            if (settings) configureDateDisplay(settings);
            return values.map(value => String(date(value, options || {})));
        }""",
        {"values": values, "settings": settings, "options": options},
    )


@pytest.mark.parametrize("browser_locale", ["de-DE", "es-ES", "en-US", "en-GB"])
@pytest.mark.parametrize("language", ["en", "de", "es"])
def test_browser_locale_is_independent_of_ui_language(
    page, design_workspace, browser_locale, language
):
    page.add_init_script(f"""localStorage.setItem('dagoberd.gui-language', '{language}');
        Object.defineProperty(navigator, 'languages', {{get: () => ['{browser_locale}']}});
        Object.defineProperty(navigator, 'language', {{get: () => '{browser_locale}'}});""")
    open_transactions(page, design_workspace)
    expected = page.evaluate(
        "locale => new Intl.DateTimeFormat(locale, {dateStyle:'short', calendar:'gregory', numberingSystem:'latn',timeZone:'UTC'}).format(new Date('2026-07-02T00:00:00Z'))",
        browser_locale,
    )
    assert evaluate_dates(page, ["2026-07-02"])[0] == expected
    expect(page.locator('#transaction-body td[data-column="date"]').last).to_have_text(expected)


@pytest.mark.parametrize(
    "settings,expected",
    [
        ({"locale": "de-DE", "date_format": "%Y-%m-%d"}, "2026-07-02"),
        ({"locale": "en-US", "date_format": "%d.%m.%Y"}, "02.07.2026"),
        ({"locale": "en-GB", "date_format": "%m/%d/%Y"}, "07/02/2026"),
        ({"locale": "de-DE", "date_format": "locale"}, "02.07.26"),
    ],
)
def test_configuration_overrides_browser_preference(page, design_workspace, settings, expected):
    configure_metadata(page, settings)
    open_transactions(page, design_workspace)
    assert evaluate_dates(page, ["2026-07-02"]) == [expected]


@pytest.mark.parametrize("fallback", ["missing", "invalid", "next_language"])
def test_missing_invalid_and_fallback_browser_locales(page, design_workspace, fallback):
    languages, language = {
        "missing": ([], ""),
        "invalid": (["not_a_locale"], "not_a_locale"),
        "next_language": (["not_a_locale", "de-DE"], "es-ES"),
    }[fallback]
    page.add_init_script(
        f"Object.defineProperty(navigator, 'languages', {{get: () => {json.dumps(languages)}}}); Object.defineProperty(navigator, 'language', {{get: () => {json.dumps(language)}}});"
    )
    open_transactions(page, design_workspace)
    expected = "02.07.26" if fallback == "next_language" else "02.07.2026"
    assert evaluate_dates(page, ["2026-07-02"]) == [expected]


def test_metadata_refresh_rebinds_existing_dates_and_keeps_drafts(page, design_workspace):
    open_transactions(page, design_workspace)
    page.evaluate("""async () => {
        const base = document.querySelector('link[href$="/app.css"]').href;
        const {date} = await import(new URL('i18n.js', base));
        const {el} = await import(new URL('dom.js', base));
        const node = el('p', {id:'synthetic-date'}, date('2026-07-02'));
        document.querySelector('main').append(node, el('input', {id:'synthetic-draft', value:'Keep this draft'}));
        window.dateNode = node;
    }""")
    configure_metadata(page, {"locale": None, "date_format": "%d.%m.%Y"})
    page.evaluate("""async () => {
        const base = document.querySelector('link[href$="/app.css"]').href;
        const {loadMetadata} = await import(new URL('store.js', base));
        await loadMetadata();
    }""")
    expect(page.locator("#synthetic-date")).to_have_text("02.07.2026")
    assert page.evaluate("window.dateNode === document.querySelector('#synthetic-date')")
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    choose_language(page, "es")
    expect(page.locator("#synthetic-date")).to_have_text("02.07.2026")
    expect(page.locator("#synthetic-draft")).to_have_value("Keep this draft")
    assert requests == []


@pytest.mark.parametrize(
    "page", ["Pacific/Kiritimati", "America/Adak", "Europe/Madrid"], indirect=True
)
def test_calendar_dates_and_local_timestamp_date_parts(page, design_workspace):
    open_transactions(page, design_workspace)
    settings = {"locale": "en-US", "date_format": "%d.%m.%Y"}
    values = ["2024-02-29", "2026-12-31", "0001-01-01", "2026-02-30", "bad date", None]
    assert evaluate_dates(page, values, settings) == [
        "29.02.2024",
        "31.12.2026",
        "01.01.0001",
        "2026-02-30",
        "bad date",
        "—",
    ]
    stamps = [
        "2026-07-01T00:15:00+14:00",
        "2026-12-31T23:30:00-12:00",
        "2026-03-29T01:30:00Z",
        "2026-10-25T01:30:00Z",
    ]
    expected = page.evaluate(
        """values => values.map(value => {
        const parts = new Intl.DateTimeFormat('en-GB', {year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date(value));
        const fields = Object.fromEntries(parts.map(part => [part.type,part.value]));
        return `${fields.day}.${fields.month}.${fields.year} ` + new Intl.DateTimeFormat('en', {timeStyle:'short'}).format(new Date(value));
    })""",
        stamps,
    )
    assert (
        evaluate_dates(page, stamps, settings, {"dateStyle": "medium", "timeStyle": "short"})
        == expected
    )


def test_workspace_metadata_uses_its_own_display_config_without_changing_dates(page, gui):
    base, entries = gui
    for entry, pattern in zip(entries, ["dmy-dot", "iso"], strict=True):
        path = Path(entry["config_path"])
        path.write_text(
            path.read_text() + f'\n[display]\ndate_format="{pattern}"\n', encoding="utf-8"
        )
    for entry, expected in zip(entries, ["02.07.2026", "2026-07-02"], strict=True):
        page.goto(base + f"?workspace={entry['id']}#t=picker-token")
        expect(page.locator(".kpi-grid")).to_be_visible()
        assert evaluate_dates(page, ["2026-07-02"]) == [expected]
        metadata = page.request.get(
            base + f"api/workspaces/{entry['id']}/meta",
            headers={"X-Dagoberd-Token": "picker-token"},
        ).json()["data"]
        assert metadata["display"]["locale"] is None


@pytest.mark.parametrize("width", [320, 800, 1280])
@pytest.mark.parametrize("language", ["en", "de", "es"])
def test_dates_fit_grid_in_each_language(page, design_workspace, tmp_path, width, language):
    page.set_viewport_size({"width": width, "height": 800})
    page.add_init_script(f"localStorage.setItem('dagoberd.gui-language', '{language}')")
    configure_metadata(page, {"locale": None, "date_format": "%d.%m.%Y"})
    open_transactions(page, design_workspace)
    expect(page.locator('#transaction-body td[data-column="date"]').last).to_have_text("02.07.2026")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / "date-display.png"))


def test_offline_html_does_not_adopt_browser_locale(page, design_workspace):
    _, profile, analysis, _ = design_workspace
    for path in [profile, analysis]:
        page.goto(path.as_uri())
        before = page.locator("body").inner_text()
        page.add_init_script(
            "Object.defineProperty(navigator,'languages',{configurable:true,get:()=>['ja-JP']});"
        )
        page.reload()
        assert page.locator("body").inner_text() == before
