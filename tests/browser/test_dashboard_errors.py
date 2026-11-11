from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
from playwright.sync_api import Page, expect, sync_playwright

from dagoberd.config import load_config
from dagoberd.gui import create_app
from dagoberd.ledger.database import LedgerDatabase
from dagoberd.ledger.organization import OrganizationService
from dagoberd.ledger.repository import LedgerRepository

pytestmark = pytest.mark.skipif(
    os.environ.get("DAGOBERD_BROWSER_TESTS") != "1",
    reason="Chromium is installed only in the dedicated GUI test job",
)
DASHBOARD = "**/api/dashboard?*"


@pytest.fixture(scope="module")
def gui_url(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> Iterator[str]:
    root = tmp_path_factory.mktemp("dashboard-browser")
    config = root / "dagoberd.toml"
    config.write_text('[database]\npath = "ledger.db"\n', encoding="utf-8")
    loaded = load_config(config)
    service = OrganizationService(LedgerRepository(LedgerDatabase(loaded.config.database.path)))
    if getattr(request, "param", True):
        service.create_manual_account(
            "Synthetic cash", account_kind="cash", idempotency_key="dashboard-cash"
        )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(loaded, "browser-token"), host="127.0.0.1", port=port, log_level="critical"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.01)
        assert server.started
        yield f"http://127.0.0.1:{port}/#t=browser-token"
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


@pytest.mark.parametrize(
    "language,retry", [("en", "Try again"), ("de", "Erneut versuchen"), ("es", "Reintentar")]
)
@pytest.mark.parametrize(
    "failure",
    [
        "network",
        "http",
        "invalid_json",
        "invalid_envelope",
        "application",
        "unauthorized",
        "workspace",
        "render",
    ],
)
def test_dashboard_failure_and_recovery(page, gui_url, language, retry, failure):
    page.add_init_script(f"localStorage.setItem('dagoberd.gui-language', '{language}')")

    def fail(route):
        if failure == "network":
            route.abort("failed")
        elif failure == "http":
            route.fulfill(status=500, content_type="text/html", body="<h1>PRIVATE BODY</h1>")
        elif failure == "invalid_json":
            route.fulfill(status=200, content_type="application/json", body="PRIVATE BODY")
        elif failure in {"invalid_envelope", "render"}:
            route.fulfill(json={} if failure == "invalid_envelope" else {"ok": True, "data": {}})
        else:
            code = {
                "application": "unknown_future_code",
                "unauthorized": "unauthorized",
                "workspace": "invalid_workspace",
            }[failure]
            route.fulfill(
                status=401 if failure == "unauthorized" else 400,
                json={"ok": False, "error": {"code": code, "message": "PRIVATE BODY"}},
            )

    page.route(DASHBOARD, fail)
    page.goto(gui_url)
    panel = page.locator(".overview-error")
    expect(panel).to_be_visible()
    expect(page.locator(".overview-loading")).to_have_count(0)
    expect(page.locator(".overview-page")).to_have_attribute("aria-busy", "false")
    expect(page.locator(".kpi-grid")).to_have_count(0)
    expect(panel).not_to_contain_text("PRIVATE BODY")
    expect(panel).not_to_contain_text("Failed to fetch")
    expect(panel).not_to_contain_text("Load failed")
    if failure == "http":
        expect(panel.locator("details")).to_contain_text("500")
    if failure == "render":
        expect(panel.locator("details")).to_have_count(0)
    page.locator("#refresh").click()
    expect(panel).to_be_visible()
    expect(page.locator("#refresh")).to_be_enabled()
    expect(page.locator("#notice")).to_be_hidden()
    page.unroute(DASHBOARD, fail)
    page.get_by_role("button", name=retry, exact=True).click()
    expect(page.locator(".kpi-grid")).to_be_visible()
    expect(page.locator(".overview-error")).to_have_count(0)
    expect(page.locator(".overview-loading")).to_have_count(0)


def test_error_language_updates_and_api_contracts(page, gui_url):
    page.route(DASHBOARD, lambda route: route.abort("failed"))
    page.goto(gui_url)
    for language, text in [
        ("de", "Der lokale GUI-Server ist nicht erreichbar."),
        ("es", "No se puede conectar con el servidor local de la interfaz."),
        ("en", "The local GUI server could not be reached."),
    ]:
        page.locator("#language").select_option(language)
        expect(page.locator(".overview-error")).to_contain_text(text)
    page.route(
        "**/api/test-error",
        lambda route: route.fulfill(
            status=409,
            json={
                "ok": False,
                "error": {
                    "code": "version_conflict",
                    "message": "conflict",
                    "details": {"current": {"version": 7}},
                },
            },
        ),
    )
    result = page.evaluate("""async () => {
        const base = document.querySelector('link[rel="stylesheet"]').href;
        const {api, apiBlob} = await import(new URL('./store.js', base));
        const errors = [];
        for (const load of [api, apiBlob]) {
            try { await load('/test-error'); }
            catch (e) { errors.push({kind:e.kind, status:e.status, code:e.code, details:e.details}); }
        }
        return errors;
    }""")
    assert (
        result
        == [
            {
                "kind": "http",
                "status": 409,
                "code": "version_conflict",
                "details": {"current": {"version": 7}},
            }
        ]
        * 2
    )


@pytest.mark.parametrize("navigate", ["tab", "period"])
@pytest.mark.parametrize("late_failure", [False, True])
def test_late_dashboard_response_is_ignored(page, gui_url, navigate, late_failure):
    held = []
    page.route(DASHBOARD, lambda route: held.append(route) if not held else route.continue_())
    page.goto(gui_url)
    expect(page.locator(".overview-loading")).to_be_visible()
    assert len(held) == 1
    original = held[0]
    if navigate == "tab":
        page.get_by_role("button", name="Transactions", exact=True).click()
        expect(page.locator("#transaction-body")).to_be_attached()
    else:
        with page.expect_response(lambda response: "period=3m" in response.url):
            page.get_by_role("button", name="3 months", exact=True).click()
        expect(page.locator('.period-switch button[aria-pressed="true"]')).to_have_text("3 months")
        expect(page.locator(".kpi-grid")).to_be_visible()
    if late_failure:
        original.fulfill(status=500, body="PRIVATE BODY")
    else:
        original.fulfill(json={"ok": True, "data": {}})
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
    expect(page.locator(".overview-error")).to_have_count(0)
    if navigate == "tab":
        expect(page.locator(".overview-page")).to_have_count(0)
    else:
        expect(page.locator(".kpi-grid")).to_be_visible()
    expect(page.locator("#notice.error")).to_have_count(0)


def test_api_helpers_normalize_failures_without_repeating_writes(page, gui_url):
    page.goto(gui_url)
    expect(page.locator(".kpi-grid")).to_be_visible()
    for scenario, status, kind in [
        ("network", None, "network"),
        ("html", 503, "http"),
        ("application", 200, "http"),
    ]:
        requests = []

        def fail(route, *, scenario=scenario, requests=requests):
            requests.append(route.request.method)
            if scenario == "network":
                route.abort("failed")
            elif scenario == "html":
                route.fulfill(status=503, content_type="text/html", body="PRIVATE BODY")
            else:
                route.fulfill(json={"ok": False, "error": {"code": "unknown_code"}})

        page.route("**/api/test-error", fail)
        result = page.evaluate("""async () => {
            const base = document.querySelector('link[rel="stylesheet"]').href;
            const {api, apiBlob, apiUpload} = await import(new URL('./store.js', base));
            const errors = [];
            for (const load of [() => api('/test-error', {method:'POST', body:'{}'}),
                () => apiBlob('/test-error'),
                () => apiUpload('/test-error', new File(['synthetic'], 'test.txt'))]) {
                try { await load(); }
                catch (e) { errors.push({kind:e.kind, status:e.status, message:String(e.message)}); }
            }
            return errors;
        }""")
        assert len(result) == (2 if scenario == "application" else 3)
        assert all(error["kind"] == kind and error["status"] == status for error in result)
        assert all("PRIVATE BODY" not in error["message"] for error in result)
        assert requests == ["POST", "GET", "POST"]
        page.unroute("**/api/test-error", fail)
    page.route(
        "**/api/test-blob",
        lambda route: route.fulfill(
            content_type="application/octet-stream", body=b"synthetic binary"
        ),
    )
    assert (
        page.evaluate("""async () => {
        const base = document.querySelector('link[rel="stylesheet"]').href;
        const {apiBlob} = await import(new URL('./store.js', base));
        return (await apiBlob('/test-blob')).text();
    }""")
        == "synthetic binary"
    )


@pytest.mark.parametrize("gui_url", [False], indirect=True)
def test_empty_dashboard_is_a_valid_result(page, gui_url):
    page.goto(gui_url)
    expect(page.locator(".kpi-grid")).to_be_visible()
    expect(page.locator(".overview-page")).to_have_attribute("aria-busy", "false")
    expect(page.locator(".overview-loading, .overview-error")).to_have_count(0)
    expect(page.locator(".quality-banner")).to_contain_text("Display currency is missing")


def test_retry_preserves_account_scope_and_period(page, gui_url):
    base = gui_url.split("#", 1)[0]
    metadata = page.request.get(base + "api/meta", headers={"X-Dagoberd-Token": "browser-token"})
    account_id = metadata.json()["data"]["accounts"][0]["id"]
    page.route(DASHBOARD, lambda route: route.abort("failed"))
    page.goto(base + f"?account={account_id}#t=browser-token")
    expect(page.locator(".overview-error")).to_be_visible()
    page.get_by_role("button", name="3 months", exact=True).click()
    expect(page.locator(".overview-error")).to_be_visible()
    page.unroute(DASHBOARD)
    held = []
    page.route(DASHBOARD, lambda route: held.append(route))
    page.get_by_role("button", name="Try again", exact=True).click()
    expect(page.locator(".overview-loading")).to_be_visible()
    expect(page.get_by_role("button", name="Try again", exact=True)).to_have_count(0)
    assert len(held) == 1
    assert f"account_id={account_id}" in held[0].request.url
    assert "period=3m" in held[0].request.url
    held[0].fulfill(response=held[0].fetch())
    expect(page.locator(".kpi-grid")).to_be_visible()
    expect(page.locator(".overview-error")).to_have_count(0)
