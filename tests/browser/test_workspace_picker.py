from __future__ import annotations

import json
import os
import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
from language_controls import rebind_language
from playwright.sync_api import Page, expect, sync_playwright

from dagoberd.config import load_config
from dagoberd.gui import create_app
from dagoberd.gui.workspaces import create_workspace_app
from dagoberd.workspaces import WorkspaceRegistry

pytestmark = pytest.mark.skipif(
    os.environ.get("DAGOBERD_BROWSER_TESTS") != "1",
    reason="Chromium is installed only in the dedicated GUI test job",
)


@pytest.fixture
def gui(tmp_path, monkeypatch, request):
    monkeypatch.delenv("DAGOBERD_DATA_ROOT", raising=False)
    monkeypatch.delenv("DAGOBERD_CLOUD_WORKSPACE", raising=False)
    mode = getattr(request, "param", "multiple")
    registry = WorkspaceRegistry(tmp_path / "settings" / "workspaces.toml")
    entries = []
    if mode not in {"empty", "legacy", "standalone"}:
        for name in ["first"] if mode == "one" else ["first", "second"]:
            path = tmp_path / name / "dagoberd.toml"
            path.parent.mkdir()
            path.write_text('[database]\npath = "ledger.db"\n', encoding="utf-8")
            label = (
                "Shared " + "workspace " * 15
                if mode == "duplicates"
                else f"{name.title()} workspace"
            )
            entries.append(registry.add(name, path, display_name=label))
    loaded = None
    if mode in {"legacy", "standalone"}:
        path = tmp_path / "dagoberd.toml"
        path.write_text('[database]\npath = "ledger.db"\n', encoding="utf-8")
        loaded = load_config(path)
    app = (
        create_app(loaded, "picker-token")
        if mode == "standalone"
        else create_workspace_app(registry, "picker-token", loaded)
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.01)
        assert server.started
        yield f"http://127.0.0.1:{port}/", entries
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def page() -> Iterator[Page]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        yield page
        browser.close()
        assert errors == []


@pytest.mark.parametrize("gui", ["empty", "one", "multiple", "legacy", "standalone"], indirect=True)
def test_initial_states_and_single_workspace_auto_selection(page, gui, request):
    base, entries = gui
    calls = []
    page.on(
        "request", lambda request: calls.append(request.url) if "/api/" in request.url else None
    )
    page.goto(base + "#t=picker-token")
    mode = request.node.callspec.params["gui"]
    if mode in {"legacy", "standalone", "one"}:
        expect(page.locator(".kpi-grid")).to_be_visible()
        expect(page.locator("#refresh")).to_be_enabled()
        if mode == "legacy":
            expect(page.locator("#workspace-switcher")).to_have_text("Local configuration⌄")
        elif mode == "standalone":
            expect(page.locator("#workspace-switcher")).to_have_count(0)
        else:
            assert f"workspace={entries[0]['id']}" in page.url
            expect(page.locator("#workspace-switcher")).to_contain_text("First workspace")
            assert all(
                url.endswith("/api/workspaces") or f"/workspaces/{entries[0]['id']}/" in url
                for url in calls
            )
    else:
        expect(page.locator("main h1")).to_have_text("Choose workspace")
        expect(page.locator("#refresh")).to_be_disabled()
        assert all(url.endswith("/api/workspaces") for url in calls)
        expect(page.locator("#workspace-switcher")).to_contain_text("Choose workspace")
        page.locator("#workspace-switcher").click()
        expect(page.get_by_role("menuitemradio")).to_have_count(len(entries))
        expect(
            page.locator("#workspace-picker").get_by_role("button", name="Manage workspaces")
        ).to_be_visible()


def test_keyboard_modal_focus_and_management(page, gui):
    base, entries = gui
    page.goto(base + f"?workspace={entries[0]['id']}#t=picker-token")
    expect(page.locator(".kpi-grid")).to_be_visible()
    switcher = page.locator("#workspace-switcher")
    expect(switcher).to_have_attribute("aria-haspopup", "dialog")
    expect(switcher).to_contain_text("First workspace")
    switcher.focus()
    page.keyboard.press("ArrowDown")
    panel = page.get_by_role("dialog", name="Choose workspace", exact=True)
    expect(panel).to_be_visible()
    first = panel.get_by_role("menuitemradio", name="First workspace", exact=True)
    second = panel.get_by_role("menuitemradio", name="Second workspace", exact=True)
    manage = panel.get_by_role("button", name="Manage workspaces", exact=True)
    close = panel.get_by_role("button", name="Close", exact=True)
    expect(first).to_be_focused()
    expect(first).to_have_attribute("aria-checked", "true")
    page.keyboard.press("ArrowDown")
    expect(second).to_be_focused()
    page.keyboard.press("ArrowDown")
    expect(first).to_be_focused()
    page.keyboard.press("End")
    expect(second).to_be_focused()
    page.keyboard.press("Home")
    expect(first).to_be_focused()
    page.keyboard.press("End")
    page.keyboard.press("Tab")
    expect(manage).to_be_focused()
    page.keyboard.press("Tab")
    expect(close).to_be_focused()
    page.keyboard.press("Tab")
    expect(first).to_be_focused()
    page.keyboard.press("Escape")
    expect(switcher).to_be_focused()
    expect(switcher).to_have_attribute("aria-expanded", "false")
    switcher.click()
    first.click()
    expect(switcher).to_have_attribute("aria-expanded", "false")
    assert f"workspace={entries[0]['id']}" in page.url
    switcher.press("Space")
    manage.click()
    dialog = page.locator("dialog.modal[open]")
    expect(dialog).to_be_visible()
    expect(switcher).to_have_attribute("aria-expanded", "false")
    dialog.get_by_role("button", name="Close", exact=True).click()
    expect(switcher).to_be_focused()
    switcher.press("Enter")
    expect(first).to_be_visible()
    close.click()
    expect(switcher).to_be_focused()
    switcher.click()
    page.mouse.click(1, 1)
    expect(switcher).to_have_attribute("aria-expanded", "false")


def test_cancelled_switch_preserves_input_then_confirmed_switch_reloads(page, gui):
    base, entries = gui
    page.goto(base + f"?workspace={entries[0]['id']}&tab=admin#t=picker-token")
    field = page.locator("main input").first
    field.fill("Unsaved synthetic input")
    previous = page.url
    page.once("dialog", lambda dialog: dialog.dismiss())
    page.locator("#workspace-switcher").click()
    page.get_by_role("menuitemradio", name="Second workspace", exact=True).click()
    assert page.url == previous
    expect(field).to_have_value("Unsaved synthetic input")
    expect(page.locator("#workspace-switcher")).to_contain_text("First workspace")
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#workspace-switcher").click()
    page.get_by_role("menuitemradio", name="Second workspace", exact=True).click()
    page.wait_for_url(f"**workspace={entries[1]['id']}")
    expect(page.locator(".kpi-grid")).to_be_visible()
    expect(page.locator("#workspace-switcher")).to_contain_text("Second workspace")


def test_unknown_workspace_gates_data_and_offers_recovery(page, gui):
    base, entries = gui
    calls = []
    page.on(
        "request", lambda request: calls.append(request.url) if "/api/" in request.url else None
    )
    page.goto(base + "?workspace=missing#t=picker-token")
    expect(page.locator("main [role=alert]")).to_contain_text("not registered")
    expect(page.locator("#workspace-switcher")).to_contain_text("Workspace unavailable")
    expect(page.locator("#refresh")).to_be_disabled()
    assert all(url.endswith("/api/workspaces") for url in calls)
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#workspace-switcher").click()
    page.get_by_role("menuitemradio", name="First workspace", exact=True).click()
    page.wait_for_url(f"**workspace={entries[0]['id']}")
    expect(page.locator(".kpi-grid")).to_be_visible()


@pytest.mark.parametrize("gui", ["duplicates"], indirect=True)
def test_long_duplicate_names_mobile_layout_and_languages(page, gui):
    base, entries = gui
    page.set_viewport_size({"width": 320, "height": 640})
    page.goto(base + f"?workspace={entries[0]['id']}#t=picker-token")
    expect(page.locator(".kpi-grid")).to_be_visible()
    switcher = page.locator("#workspace-switcher")
    expect(switcher).to_contain_text("(first)")
    assert "(first)" in switcher.get_attribute("title")
    switcher.click()
    expect(page.get_by_role("menuitemradio").first).to_contain_text("(first)")
    expect(page.get_by_role("menuitemradio").nth(1)).to_contain_text("(second)")
    # Language changes must update existing controls without changing the active workspace.
    for language, title, management in [
        ("de", "Workspace auswählen", "Workspaces verwalten"),
        ("es", "Elegir espacio de trabajo", "Gestionar espacios de trabajo"),
        ("en", "Choose workspace", "Manage workspaces"),
    ]:
        rebind_language(page, language)
        expect(page.locator("#workspace-picker-title")).to_have_text(title)
        expect(
            page.locator("#workspace-picker").get_by_role("button", name=management, exact=True)
        ).to_be_visible()
    bounds = page.locator("#workspace-picker").bounding_box()
    assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 320
    assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= 640
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert str(base) not in page.locator("#workspace-picker").inner_text()


def test_many_workspaces_scroll_and_keep_keyboard_actions_reachable(page, gui):
    base, entries = gui
    page.set_viewport_size({"width": 320, "height": 480})
    page.route(
        "**/api/workspaces",
        lambda route: route.fulfill(
            json={
                "ok": True,
                "data": {
                    "legacy": False,
                    "workspaces": entries
                    + [
                        {
                            "id": f"synthetic-{index}",
                            "name": f"synthetic-{index}",
                            "display_name": f"Synthetic workspace {index}",
                        }
                        for index in range(40)
                    ],
                },
            }
        ),
    )
    page.goto(base + "#t=picker-token")
    page.locator("#workspace-switcher").click()
    panel = page.locator("#workspace-picker")
    expect(page.get_by_role("menuitemradio")).to_have_count(42)
    page.keyboard.press("End")
    expect(page.get_by_role("menuitemradio").last).to_be_focused()
    assert panel.locator(".workspace-list").evaluate(
        "node => node.scrollHeight > node.clientHeight && node.scrollTop > 0"
    )
    bounds = panel.bounding_box()
    assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= 480
    page.keyboard.press("Home")
    expect(page.get_by_role("menuitemradio", name="First workspace", exact=True)).to_be_focused()
    page.keyboard.press("Tab")
    expect(page.locator("#workspace-switcher")).to_have_attribute("aria-expanded", "true")
    assert panel.evaluate("node => node.contains(document.activeElement)")
    panel.get_by_role("button", name="Close", exact=True).click()
    expect(page.locator("#workspace-switcher")).to_be_focused()


@pytest.mark.parametrize("gui", ["one"], indirect=True)
def test_auto_selection_preserves_view_and_uses_workspace_preferences(page, gui):
    base, entries = gui
    workspace = entries[0]["id"]
    preference_key = json.dumps(f"dagoberd.dashboard-period-v1.workspace.{workspace}")
    page.add_init_script(
        "localStorage.setItem('dagoberd.dashboard-period-v1', '3m');"
        f"localStorage.setItem({preference_key}, 'ytd');"
    )
    confirmations = []
    page.on("dialog", lambda dialog: (confirmations.append(dialog.message), dialog.dismiss()))
    page.goto(base + "?tab=transactions&scope=all#t=picker-token")
    expect(page.locator("#transaction-body")).to_be_attached()
    assert f"workspace={workspace}" in page.url
    assert "tab=transactions" in page.url and "scope=all" in page.url
    assert confirmations == []
    page.locator('[data-tab="overview"]').click()
    expect(page.locator('.period-switch button[aria-pressed="true"]')).to_have_text("This year")


@pytest.mark.parametrize("gui", ["one"], indirect=True)
def test_explicit_unknown_workspace_is_not_replaced_by_the_only_entry(page, gui):
    base, _ = gui
    page.goto(base + "?workspace=missing#t=picker-token")
    expect(page.locator("main [role=alert]")).to_contain_text("not registered")
    assert "workspace=missing" in page.url
    expect(page.locator("#refresh")).to_be_disabled()
