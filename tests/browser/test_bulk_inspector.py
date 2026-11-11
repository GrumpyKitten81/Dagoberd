from __future__ import annotations

import os
import socket
import threading
import time
from datetime import date
from decimal import Decimal

import pytest
import uvicorn
from language_controls import choose_language
from playwright.sync_api import sync_playwright

from dagoberd.config import load_config
from dagoberd.gui import create_app
from dagoberd.ledger.database import LedgerDatabase
from dagoberd.ledger.organization import OrganizationService
from dagoberd.ledger.repository import LedgerRepository
from dagoberd.profiles import install_builtin_profiles

pytestmark = pytest.mark.skipif(
    os.environ.get("DAGOBERD_BROWSER_TESTS") != "1",
    reason="Chromium is installed only in the dedicated GUI test job",
)


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_bulk_inspector_preview_apply_and_responsive_discard_warning(tmp_path):
    config_path = tmp_path / "dagoberd.toml"
    config_path.write_text(
        """[database]
path = "ledger.db"
[documents]
storage_dir = "documents"
""",
        encoding="utf-8",
    )
    loaded = load_config(config_path)
    repository = LedgerRepository(LedgerDatabase(loaded.config.database.path))
    install_builtin_profiles(repository)
    service = OrganizationService(repository)
    account = service.create_manual_account(
        "Browser", account_kind="cash", idempotency_key="browser-bulk-account"
    )
    transaction_ids = [
        service.create_manual_transaction(
            account["id"],
            booking_date=date(2026, 8, day),
            amount=Decimal("-10"),
            purpose=f"Booking {day}",
            idempotency_key=f"browser-bulk-booking-{day}",
        )["id"]
        for day in (1, 2)
    ]
    first = repository.get_transaction(transaction_ids[0])
    service.set_transaction_note(transaction_ids[0], "Existing", expected_version=first["version"])

    port = _available_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(loaded, "browser-token"),
            host="127.0.0.1",
            port=port,
            log_level="critical",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.02)
    assert server.started

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page_errors = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{port}/?tab=transactions#t=browser-token")
            rows = page.locator("#transaction-body tr")
            rows.first.wait_for()
            assert rows.count() == 2
            rows.nth(0).locator(".selection-column input").check()
            rows.nth(1).locator(".selection-column input").check()
            page.get_by_role("button", name="Edit selected").click()

            note = page.locator(".bulk-field").filter(has_text="Note")
            note.get_by_text("Mixed").wait_for()
            checkbox = note.locator('input[type="checkbox"]')
            checkbox.focus()
            page.keyboard.press("Space")
            assert checkbox.is_checked()
            note.locator("textarea").fill("Together")
            requests = []
            page.on("request", lambda request: requests.append(request.method))
            page.evaluate("window.draftNode = document.querySelector('.bulk-field textarea')")
            for language, label in [("de", "Notiz"), ("es", "Nota"), ("en", "Note")]:
                choose_language(page, language)
                assert page.locator("html").get_attribute("lang") == language
                field = page.locator(".bulk-field").filter(has_text=label)
                assert field.locator("textarea").input_value() == "Together"
                assert field.locator('input[type="checkbox"]').is_checked()
                assert page.evaluate(
                    "window.draftNode === document.querySelector('.bulk-field textarea')"
                )
                assert rows.locator(".selection-column input:checked").count() == 2
                assert rows.first.locator('td[data-column="amount"]').inner_text() == page.evaluate(
                    "language => new Intl.NumberFormat(language, {style:'currency', currency:'EUR', minimumFractionDigits:2, maximumFractionDigits:2}).format(-10)",
                    language,
                )
            assert requests == []
            page.get_by_role("button", name="Preview", exact=True).click()
            page.get_by_text("2 of 2 transactions will change.").wait_for()
            page.get_by_role("button", name="Apply changes").click()
            page.get_by_text("2 transactions updated.").wait_for()
            assert {repository.get_transaction(item)["note"] for item in transaction_ids} == {
                "Together"
            }

            rows.nth(0).locator(".selection-column input").check()
            page.get_by_role("button", name="Edit selected").click()
            note = page.locator(".bulk-field").filter(has_text="Note")
            note.locator('input[type="checkbox"]').check()
            note.locator("textarea").fill("Unsaved")
            page.set_viewport_size({"width": 800, "height": 700})
            assert (
                page.locator("#detail").evaluate("node => getComputedStyle(node).position")
                == "fixed"
            )
            page.once("dialog", lambda dialog: dialog.dismiss())
            page.get_by_title("Close inspector").click()
            assert page.locator("#detail").is_visible()
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_title("Close inspector").click()
            assert page.locator("#detail").is_hidden()
            choose_language(page, "es")
            page.reload()
            rows.first.wait_for()
            assert page.locator("html").get_attribute("lang") == "es"
            page.evaluate("localStorage.setItem('dagoberd.gui-language', 'invalid')")
            page.reload()
            rows.first.wait_for()
            assert page.locator("html").get_attribute("lang") == "en"
            page.get_by_role("button", name="Data", exact=True).click()
            page.get_by_text("File imports (CSV", exact=False).first.wait_for()
            for language, title, fragment in [
                ("de", "Dateiimport", "Dateiimporte (CSV"),
                ("es", "Importación de archivos", "Se recomienda importar archivos"),
                ("en", "File import", "File imports (CSV"),
            ]:
                choose_language(page, language)
                page.get_by_role("heading", name=title, exact=True).wait_for()
                page.get_by_text(fragment, exact=False).first.wait_for()
            page.goto(f"http://127.0.0.1:{port}/?tab=admin&account={account['id']}#t=browser-token")
            try:
                page.get_by_role("heading", name="Columns for this account").wait_for(timeout=5000)
            except Exception:
                print("ADMIN ERRORS", page_errors, page.locator("body").inner_text())
                raise
            page.locator("#admin-add-tag > summary").click()
            admin_draft = page.locator("#admin-add-tag-name")
            admin_draft.fill("Unsubmitted tag")
            for language, heading in [
                ("de", "Spalten dieses Kontos"),
                ("es", "Columnas de esta cuenta"),
                ("en", "Columns for this account"),
            ]:
                choose_language(page, language)
                page.get_by_role("heading", name=heading, exact=True).wait_for()
                assert admin_draft.input_value() == "Unsubmitted tag"
            page.get_by_role("button", name="Classification", exact=True).click()
            page.get_by_role("heading", name="Profiles for this account").wait_for()
            for language, heading in [
                ("de", "Profile dieses Kontos"),
                ("es", "Perfiles de esta cuenta"),
                ("en", "Profiles for this account"),
            ]:
                choose_language(page, language)
                page.get_by_role("heading", name=heading, exact=True).wait_for()
            page.get_by_role("button", name="Data", exact=True).click()
            preview_file = tmp_path / "preview.csv"
            preview_file.write_text(
                "booking_date;amount;currency;purpose\n2026-08-01;-1.25;EUR;Synthetic import\n",
                encoding="utf-8",
            )
            page.locator('input[type="file"]').first.set_input_files(preview_file)
            page.get_by_role("button", name="Upload and preview", exact=True).click()
            page.get_by_role("button", name="Run import", exact=True).wait_for()
            for language, button, label in [
                ("de", "Import ausführen", "Zeilen gesamt"),
                ("es", "Ejecutar importación", "Filas totales"),
                ("en", "Run import", "Total rows"),
            ]:
                choose_language(page, language)
                page.get_by_role("button", name=button, exact=True).wait_for()
                page.get_by_text(label, exact=True).wait_for()
            page.get_by_role("button", name="Overview", exact=True).click()
            page.get_by_role("heading", name="Overview", exact=True).wait_for()
            for language, heading in [("de", "Übersicht"), ("es", "Resumen"), ("en", "Overview")]:
                choose_language(page, language)
                page.get_by_role("heading", name=heading, exact=True).wait_for()
            asset_base = (
                page.locator('link[href$="/app.css"]').first.get_attribute("href").rsplit("/", 1)[0]
            )
            result = page.evaluate(
                """async base => {
                const i18n = await import(`${base}/i18n.js`);
                const {el, notify} = await import(`${base}/dom.js`);
                const {api} = await import(`${base}/store.js`);
                const key = 'ui.note';
                const saved = i18n.catalogs.es[key];
                delete i18n.catalogs.es[key];
                Object.defineProperty(Storage.prototype, 'setItem', {value: () => {throw new Error('disabled');}, configurable:true});
                i18n.setLanguage('es');
                const fallback = String(i18n.msg(key));
                i18n.catalogs.es[key] = saved;
                const node = el('p', {}, i18n.msg('ui.category', {value:'unused'}));
                let apiError = '';
                try { await api('/transactions/missing-language-test'); }
                catch (error) { apiError = String(error.message); }
                document.body.append(node);
                notify(i18n.msg('error.invalid_request', {details:'<img src=x onerror=alert(1)>'}), 'error');
                return {fallback, apiError, text:document.querySelector('#notice').textContent,
                    unsafe:document.querySelector('#notice img') !== null,
                    singular:String(i18n.msg('ui.transactions_updated', {count:1}))};
            }""",
                asset_base,
            )
            assert result["fallback"] == "Note"
            assert result["unsafe"] is False
            assert "Elemento no encontrado" in result["apiError"]
            assert "Solicitud no válida" in result["text"]
            assert result["singular"] == "1 movimiento actualizado."
            assert page_errors == []
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
