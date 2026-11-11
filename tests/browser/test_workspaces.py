from __future__ import annotations

import os
import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import sync_playwright

from dagoberd.gui.workspaces import create_workspace_app
from dagoberd.ledger.database import LedgerDatabase
from dagoberd.ledger.organization import OrganizationService
from dagoberd.ledger.repository import LedgerRepository
from dagoberd.workspaces import WorkspaceRegistry, workspace_operation

pytestmark = pytest.mark.skipif(
    os.environ.get("DAGOBERD_BROWSER_TESTS") != "1",
    reason="Chromium is installed only in the dedicated GUI test job",
)


def test_workspace_selection_parallel_tabs_and_repository_initialization(tmp_path, monkeypatch):
    monkeypatch.delenv("DAGOBERD_DATA_ROOT", raising=False)
    monkeypatch.delenv("DAGOBERD_CLOUD_WORKSPACE", raising=False)
    registry = WorkspaceRegistry(tmp_path / "settings" / "workspaces.toml")
    entries = []
    for name in ("private", "business"):
        entries.append(registry.initialize(name, tmp_path / name))
        loaded = registry.load(name)
        with workspace_operation(loaded, registry):
            OrganizationService(
                LedgerRepository(LedgerDatabase(loaded.config.database.path))
            ).create_manual_account(
                f"{name.title()} Cash", account_kind="cash", idempotency_key="same-key"
            )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_workspace_app(registry, "browser-token"),
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.01)
    assert server.started
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context()
            first = context.new_page()
            second = context.new_page()
            errors = []
            first.on("pageerror", lambda error: errors.append(error.stack))
            second.on("pageerror", lambda error: errors.append(error.stack))
            first.on("dialog", lambda dialog: dialog.accept())
            base = f"http://127.0.0.1:{port}/"
            dashboard_requests = []
            first.on(
                "request",
                lambda request: (
                    dashboard_requests.append(request.url)
                    if "/api/" in request.url and "/dashboard" in request.url
                    else None
                ),
            )
            first.goto(base + "#t=browser-token")
            first.get_by_role("heading", name="Choose workspace").wait_for()
            assert dashboard_requests == []
            assert first.locator("#refresh").is_disabled()
            first.locator("#workspace-switcher").click()
            first.get_by_role("menuitemradio", name=entries[0]["display_name"], exact=True).click()
            first.wait_for_url(f"**workspace={entries[0]['id']}")
            first.locator("#account-scope").click()
            first.get_by_text("Private Cash", exact=True).wait_for()
            assert first.get_by_text("Business Cash", exact=True).count() == 0
            first.keyboard.press("Escape")
            second.goto(base + f"?workspace={entries[1]['id']}#t=browser-token")
            second.locator("#account-scope").click()
            second.get_by_text("Business Cash", exact=True).wait_for()
            assert second.get_by_text("Private Cash", exact=True).count() == 0
            second.keyboard.press("Escape")
            first.locator("#workspace-switcher").click()
            first.get_by_role("menuitemradio", name=entries[1]["display_name"], exact=True).click()
            first.wait_for_url(f"**workspace={entries[1]['id']}")
            first.locator("#account-scope").click()
            first.get_by_text("Business Cash", exact=True).wait_for()
            first.keyboard.press("Escape")
            first.locator("#workspace-switcher").click()
            first.get_by_role("button", name="Manage workspaces", exact=True).click()
            dialog = first.locator("dialog[open]")
            for language, local_init, snapshot_status in (
                ("de", "Lokalen Ordner initialisieren", "Status der Snapshot-Synchronisierung"),
                ("es", "Inicializar carpeta local", "Estado de sincronización de instantáneas"),
                ("en", "Initialize local folder", "Snapshot synchronization status"),
            ):
                first.locator("#language").select_option(language)
                assert dialog.locator('option[value="init"]').text_content() == local_init
                assert (
                    dialog.locator('option[value="sync-status"]').text_content() == snapshot_status
                )
                assert dialog.locator('option[value="git-init"]').count() == 1
            dialog.locator("select").select_option("status")
            dialog.get_by_role("button", name="Apply / preview", exact=True).click()
            dialog.get_by_text('"storage": "local"', exact=False).wait_for()
            dialog.locator("select").select_option("init")
            dialog.get_by_label("Name", exact=True).fill("association")
            dialog.get_by_label("Configuration file / target directory", exact=True).fill(
                str(tmp_path / "provided")
            )
            dialog.get_by_role("button", name="Apply / preview", exact=True).click()
            first.locator("#workspace-switcher").click()
            first.get_by_role("menuitemradio", name="association", exact=True).wait_for()
            assert (tmp_path / "provided" / "dagoberd.toml").is_file()
            assert not (tmp_path / "provided" / ".git").exists()
            assert not errors
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
