from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Page, expect, sync_playwright

from dagoberd.config import load_config
from dagoberd.gui import create_app
from dagoberd.ledger.database import LedgerDatabase
from dagoberd.ledger.organization import OrganizationService
from dagoberd.ledger.repository import LedgerRepository
from dagoberd.profiles import install_builtin_profiles
from dagoberd.reporting.analysis import AnalysisDefinition, AnalysisService
from dagoberd.reporting.ledger_reports import LedgerReportGenerator

pytestmark = pytest.mark.skipif(
    os.environ.get("DAGOBERD_BROWSER_TESTS") != "1",
    reason="Chromium is installed only in the dedicated GUI test job",
)


@pytest.fixture(scope="module")
def design_workspace(tmp_path_factory):
    root = tmp_path_factory.mktemp("design-system")
    config = root / "dagoberd.toml"
    config.write_text(
        '[database]\npath="ledger.db"\n[documents]\nstorage_dir="documents"\n', encoding="utf-8"
    )
    loaded = load_config(config, use_environment=False)
    repository = LedgerRepository(LedgerDatabase(loaded.config.database.path))
    install_builtin_profiles(repository)
    service = OrganizationService(repository)
    account = service.create_manual_account(
        "Synthetic cash",
        account_kind="cash",
        opening_balance=Decimal("300"),
        balance_date=date(2026, 7, 1),
        idempotency_key="design-system-cash",
    )
    for day, purpose in [(2, "Synthetic groceries"), (3, "Synthetic supplies")]:
        service.create_manual_transaction(
            account["id"],
            booking_date=date(2026, 7, day),
            amount=Decimal("-12.50"),
            purpose=purpose,
            category_profile="household",
            category_code="expense.food",
            dimensions={"person": "shared"},
            idempotency_key=f"design-system-{day}",
        )
    report = LedgerReportGenerator(repository, root / "reports").generate(
        profile_code="household", formats=("html",)
    )[0]
    analyses = AnalysisService(repository)
    saved = analyses.create_saved_analysis(
        "design-preview",
        "Synthetic analysis",
        AnalysisDefinition(booking_from="2026-07-01", booking_to="2026-07-31", formats=("html",)),
    )
    execution = analyses.run_saved_analysis(saved["id"], root / "reports", as_of=date(2026, 7, 31))

    analysis = next(Path(path) for path in execution["paths"] if path.endswith(".html"))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(loaded, "design-token"), host="127.0.0.1", port=port, log_level="critical"
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
        yield f"http://127.0.0.1:{port}/", report, analysis, account["id"]
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


@pytest.mark.parametrize("width", [320, 800, 1280])
def test_gui_theme_all_tabs_languages_and_focus(page, design_workspace, width):
    base, _, _, account_id = design_workspace
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(base + f"?account={account_id}#t=design-token")
    expect(page.locator(".kpi-grid")).to_be_visible()
    assert page.locator(".kpi").evaluate_all("""nodes => nodes.every(node => {
        const label = node.querySelector('span').getBoundingClientRect();
        const value = node.querySelector('strong').getBoundingClientRect();
        return label.bottom <= value.top || label.right <= value.left;
    })""")
    assert (
        page.locator("body").evaluate("el => getComputedStyle(el).backgroundColor")
        == "rgb(246, 245, 241)"
    )
    for language in ("en", "de", "es"):
        page.locator("#language").select_option(language)
        for tab in ("overview", "transactions", "admin", "data", "classification"):
            page.locator(f'[data-tab="{tab}"]').click()
            expect(page.locator("main")).not_to_be_empty()
            if tab == "transactions":
                expect(page.locator("#transaction-body tr")).to_have_count(2)
            else:
                expect(page.locator("main .ui-card").first).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator('[data-tab="transactions"]').click()
    primary = page.locator("button.primary").first
    expect(primary).to_be_visible()
    primary.focus()
    page.keyboard.press("Tab")
    assert page.evaluate("getComputedStyle(document.activeElement).outlineStyle") == "solid"
    page.emulate_media(reduced_motion="reduce")
    assert set(primary.evaluate("el => getComputedStyle(el).transitionDuration").split(", ")) == {
        "0s"
    }


@pytest.mark.parametrize("kind", ["profile", "analysis"])
@pytest.mark.parametrize("width", [320, 800, 1280])
def test_reports_share_components_work_offline_and_print(page, design_workspace, kind, width):
    base, report, analysis, _ = design_workspace
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(base + "?tab=transactions#t=design-token")
    expect(page.locator(".ui-table")).to_be_visible()
    expect(page.locator("#transaction-body tr")).to_have_count(2)
    reference = page.evaluate("""() => ({
        font: getComputedStyle(document.body).fontFamily,
        background: getComputedStyle(document.body).backgroundColor,
        radius: getComputedStyle(document.querySelector('.ui-card')).borderRadius,
        header: getComputedStyle(document.querySelector('.ui-table th')).backgroundColor
    })""")
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    page.goto((report if kind == "profile" else analysis).as_uri())
    assert (
        page.evaluate("""() => ({
        font: getComputedStyle(document.body).fontFamily,
        background: getComputedStyle(document.body).backgroundColor,
        radius: getComputedStyle(document.querySelector('.ui-card')).borderRadius,
        header: getComputedStyle(document.querySelector('.ui-table th')).backgroundColor
    })""")
        == reference
    )
    assert not any(url.startswith(("http:", "https:")) for url in requests)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if kind == "profile":
        page.locator("#transactions > summary").click()
        expect(page.locator("#transactions-table")).to_be_visible()
        page.locator("#transactions-table").get_by_placeholder("Purpose", exact=True).fill(
            "groceries"
        )
        expect(page.locator("#transactions-visible-count")).to_have_text(
            "1 of 2 transactions visible"
        )
    else:
        assert page.locator("#dagoberd-analysis").count() == 1
        assert page.locator(".ui-table tbody tr").evaluate_all(
            "nodes => nodes.every(node => node.getBoundingClientRect().height < 100)"
        )
    page.emulate_media(media="print")
    assert page.locator(".ui-card").first.evaluate("el => getComputedStyle(el).boxShadow") == "none"
    if kind == "profile":
        assert page.locator("#transactions").get_attribute("open") is not None
        assert page.locator("#metadata").get_attribute("open") is None
        expect(page.locator("#transactions-visible-count")).to_have_text(
            "1 of 2 transactions visible"
        )
