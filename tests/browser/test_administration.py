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
from playwright.sync_api import expect
from test_workspace_picker import page as page

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


@pytest.fixture
def admin_gui(tmp_path):
    config = tmp_path / "dagoberd.toml"
    config.write_text('[database]\npath="ledger.db"\n', encoding="utf-8")
    loaded = load_config(config, use_environment=False)
    repository = LedgerRepository(LedgerDatabase(loaded.config.database.path))
    install_builtin_profiles(repository)
    organization = OrganizationService(repository)
    accounts = [
        organization.create_manual_account(
            name, account_kind="cash", idempotency_key=f"admin-account-{index}"
        )
        for index, name in enumerate(["Synthetic first", "Synthetic second"])
    ]
    dimension = organization.create_dimension("project-code", "Synthetic projects")
    value = organization.create_dimension_value("project-code", "used", "Used synthetic value")
    organization.create_dimension_value("project-code", "unused", "Unused synthetic value")
    organization.create_tag("Existing tag")
    organization.create_merchant("Existing shop")
    transaction = organization.create_manual_transaction(
        accounts[0]["id"],
        booking_date=date(2026, 7, 2),
        amount=Decimal("-12.50"),
        purpose="Synthetic transaction",
        idempotency_key="admin-transaction",
    )
    organization.set_transaction_dimension(
        transaction["id"], "project-code", "used", expected_version=transaction["version"]
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(loaded, "admin-token"), host="127.0.0.1", port=port, log_level="critical"
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
        yield f"http://127.0.0.1:{port}/", organization, accounts, dimension, value
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def open_admin(page, admin_gui, selected=True):
    base, _, accounts, _, _ = admin_gui
    page.goto(
        base
        + f"?tab=admin&{('account=' + accounts[0]['id']) if selected else 'scope=all'}#t=admin-token"
    )
    expect(page.locator(".admin-page")).to_be_visible()
    if selected:
        expect(page.locator("#admin-columns-form")).to_be_visible()
    return page.locator(".admin-page")


def add_entry(page, id, name, code=None):
    details = page.locator(f"#{id}")
    if not details.get_attribute("open") and not details.evaluate("node => node.open"):
        details.locator(":scope > summary").click()
    if code is not None:
        details.locator(f"#{id}-code").fill(code)
    details.locator(f"#{id}-name").fill(name)
    details.locator('button[type="submit"]').click()
    expect(details.locator("form")).to_have_attribute("aria-busy", "false")
    expect(details.locator('button[type="submit"]')).to_be_disabled()
    return details


def dimension_card(page, code="project-code"):
    node = page.locator(f'.admin-dimension[data-dimension="{code}"]')
    if not node.evaluate("node => node.open"):
        node.locator(":scope > summary").click()
    return node


def save_form(form):
    form.locator('button[type="submit"]').click()
    expect(form.locator('[role="status"]')).not_to_have_text("Saving…")
    expect(form.locator('[role="status"]')).to_be_visible()


def test_create_forms_normalization_validation_and_existing_entries(page, admin_gui):
    open_admin(page, admin_gui)
    tag = page.locator("#admin-add-tag")
    tag.locator("summary").click()
    tag.locator("input").fill("   ")
    expect(tag.locator('button[type="submit"]')).to_be_disabled()
    expect(tag.locator('[aria-invalid="true"]')).to_be_visible()
    add_entry(page, "admin-add-tag", "New synthetic tag")
    expect(page.locator("#admin-tags .admin-reference-list")).to_contain_text("New synthetic tag")
    add_entry(page, "admin-add-tag", "Existing tag")
    expect(tag).to_contain_text("already exists")
    add_entry(page, "admin-add-merchant", "New synthetic shop")
    expect(page.locator("#admin-merchants .admin-reference-list")).to_contain_text(
        "New synthetic shop"
    )
    create = page.locator("#admin-add-dimension")
    create.locator("summary").click()
    create.locator('input[id$="-name"]').fill("New dimension")
    create.locator('input[id$="-code"]').fill("bad/code")
    expect(create.locator('button[type="submit"]')).to_be_disabled()
    add_entry(page, "admin-add-dimension", "New dimension", "Projekt Ä")
    expect(create).to_contain_text("projekt-ä")
    card = dimension_card(page, "projekt-ä")
    expect(card).to_contain_text("No values yet")
    id = card.locator(".admin-add").get_attribute("id")
    add_entry(page, id, "New answer", "Answer 1")
    expect(card.locator(".admin-value .admin-code")).to_contain_text("answer-1")
    expect(
        card.locator(".admin-value input[type=text], .admin-value input:not([type])")
    ).to_have_value("New answer")


def test_saves_preserve_other_drafts_expansions_and_update_versions(page, admin_gui):
    open_admin(page, admin_gui)
    card = dimension_card(page)
    dim_form = card.locator(":scope > form")
    dim_form.locator("input:not([type])").fill("Unsaved dimension draft")
    value = card.locator(".admin-value").first
    value.locator("input:not([type])").fill("Unsaved value draft")
    tag = page.locator("#admin-add-tag")
    tag.locator("summary").click()
    tag.locator("input").fill("Unsaved tag draft")
    add_entry(page, "admin-add-merchant", "New shop")
    expect(tag.locator("input")).to_have_value("Unsaved tag draft")
    expect(dim_form.locator("input:not([type])")).to_have_value("Unsaved dimension draft")
    expect(value.locator("input:not([type])")).to_have_value("Unsaved value draft")
    assert card.evaluate("node => node.open")
    for name in ["Renamed once", "Renamed twice"]:
        page.locator("#admin-account-name").fill(name)
        save_form(page.locator("#admin-account-form"))
        expect(page.locator("#account-scope")).to_have_text(name)
    for name in ["Dimension once", "Dimension twice"]:
        dim_form.locator("input:not([type])").fill(name)
        save_form(dim_form)
        expect(card.locator(":scope > summary")).to_contain_text(name)
    for name in ["Value once", "Value twice"]:
        value.locator("input:not([type])").fill(name)
        save_form(value)
    choose_language(page, "de")
    expect(tag.locator("input")).to_have_value("Unsaved tag draft")
    expect(value.locator("input:not([type])")).to_have_value("Value twice")
    tag.locator("summary").click()
    tag.locator("summary").click()
    expect(tag.locator("input")).to_have_value("Unsaved tag draft")


def test_delete_confirmation_usage_rejection_and_deactivation(page, admin_gui):
    open_admin(page, admin_gui)
    card = dimension_card(page)
    used = card.locator(".admin-value").filter(has=page.get_by_text("used", exact=True))
    requests = []
    page.on(
        "request",
        lambda request: requests.append(request.method) if request.method == "DELETE" else None,
    )
    page.once("dialog", lambda prompt: prompt.dismiss())
    used.get_by_role("button", name="Delete", exact=True).click()
    assert requests == []
    expect(used.locator('[role="status"]')).to_have_count(0)
    page.once("dialog", lambda prompt: prompt.accept())
    used.get_by_role("button", name="Delete", exact=True).click()
    expect(used.locator('[role="alert"]')).to_be_visible()
    used.locator('input[type="checkbox"]').uncheck()
    save_form(used)
    expect(used.locator(".ui-badge")).to_have_text("Inactive")
    unused = card.locator(".admin-value").filter(has=page.get_by_text("unused", exact=True))
    page.once("dialog", lambda prompt: prompt.accept())
    unused.get_by_role("button", name="Delete", exact=True).click()
    expect(unused).to_have_count(0)
    add_entry(page, "admin-add-dimension", "Disposable dimension", "disposable")
    disposable = dimension_card(page, "disposable")
    page.once("dialog", lambda prompt: prompt.accept())
    disposable.locator(":scope > form").get_by_role("button", name="Delete", exact=True).click()
    expect(disposable).to_have_count(0)


@pytest.mark.parametrize(
    "endpoint,failed", [("columns", "admin-columns"), ("options/selected", "admin-options")]
)
def test_independent_section_failure_and_retry(page, admin_gui, endpoint, failed):
    pattern = f"**/api/accounts/*/{endpoint}"
    page.route(
        pattern,
        lambda route: route.fulfill(status=500, content_type="text/html", body="PRIVATE TRACEBACK"),
    )
    open_admin(page, admin_gui, selected=False)
    _, _, accounts, _, _ = admin_gui
    page.evaluate(
        "id => window.dispatchEvent(new CustomEvent('dagoberd:navigate', {detail:{accountIds:[id],tab:'admin'}}))",
        accounts[0]["id"],
    )
    section = page.locator(f"#{failed}")
    expect(section.locator('[role="alert"]')).to_contain_text("Could not load")
    expect(section).not_to_contain_text("PRIVATE TRACEBACK")
    expect(page.locator("#admin-reference")).to_be_visible()
    other = "#admin-options-category" if failed == "admin-columns" else "#admin-columns-form"
    expect(page.locator(other)).to_be_attached()
    page.unroute(pattern)
    section.get_by_role("button", name="Load again").click()
    expect(section.locator("form").first).to_be_attached()


def test_busy_network_failure_and_duplicate_submission_protection(page, admin_gui):
    open_admin(page, admin_gui)
    held = []
    page.route("**/api/tags", lambda route: held.append(route))
    form = page.locator("#admin-add-tag")
    form.locator("summary").click()
    form.locator("input").fill("Pending tag")
    form.locator('button[type="submit"]').click()
    expect(form.locator("input")).to_be_disabled()
    expect(page.locator('#admin-account-form button[type="submit"]')).to_be_disabled()
    form.locator("form").evaluate(
        "node => node.dispatchEvent(new Event('submit', {bubbles:true,cancelable:true}))"
    )
    assert len(held) == 1
    held[0].abort("failed")
    expect(form.locator('[role="alert"]')).to_be_visible()
    expect(form.locator("input")).to_have_value("Pending tag")
    expect(form.locator('button[type="submit"]')).to_be_enabled()
    expect(page.locator('#admin-account-form button[type="submit"]')).to_be_enabled()


def test_refresh_failure_retries_only_reads_after_confirmed_write(page, admin_gui):
    open_admin(page, admin_gui)
    page.route("**/api/meta", lambda route: route.abort("failed"))
    writes = []
    page.on(
        "request",
        lambda request: writes.append(request.method) if request.method == "POST" else None,
    )
    form = add_entry(page, "admin-add-tag", "Confirmed tag")
    expect(form.locator('[role="alert"]')).to_contain_text("Saved, but")
    assert writes == ["POST"]
    page.unroute("**/api/meta")
    form.get_by_role("button", name="Load again").click()
    expect(form.locator('[role="status"]')).to_contain_text("current state")
    expect(page.locator("#admin-tags .admin-reference-list")).to_contain_text("Confirmed tag")
    assert writes == ["POST"]


def test_account_scope_gate_and_deactivation_syncs_scope(page, admin_gui):
    open_admin(page, admin_gui, selected=False)
    page.locator("#admin-account-gate button").click()
    scope = page.locator(".scope-dialog")
    scope.get_by_role("button", name="Clear selection").click()
    scope.locator(".scope-list label").first.locator("input").check()
    scope.get_by_role("button", name="Apply", exact=True).click()
    expect(page.locator("#admin-account-name")).to_have_value("Synthetic first")
    page.locator("#admin-account-active").uncheck()
    page.locator('#admin-account-form button[type="submit"]').click()
    expect(page.locator("#admin-account-gate")).to_be_visible()
    expect(page.locator("#account-scope")).to_have_text("All accounts")
    assert "scope=all" in page.url


@pytest.mark.parametrize("width", [320, 800, 1280])
@pytest.mark.parametrize("language", ["en", "de", "es"])
def test_admin_layout_labels_and_languages(page, admin_gui, tmp_path, width, language):
    _, organization, _, _, _ = admin_gui
    for index in range(15):
        organization.create_dimension_value(
            "project-code", f"long-code-{index}-" + "x" * 60, "Long synthetic value " * 6
        )
    page.set_viewport_size({"width": width, "height": 800})
    page.add_init_script(f"localStorage.setItem('dagoberd.gui-language', '{language}')")
    open_admin(page, admin_gui)
    page.screenshot(path=str(tmp_path / "administration-top.png"))
    card = dimension_card(page)
    expect(card.locator(".admin-value")).to_have_count(17)
    assert page.locator(".admin-field input").evaluate_all(
        "nodes => nodes.every(node => node.labels.length && node.getAttribute('aria-describedby'))"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    for node in card.locator(".admin-value").all():
        assert node.evaluate("node => node.scrollWidth <= node.clientWidth")
    card.locator(":scope > summary").focus()
    page.keyboard.press("Tab")
    expect(card.locator(":scope > form input:not([type])")).to_be_focused()
    page.screenshot(path=str(tmp_path / "administration.png"))
    card.locator(":scope > .admin-add > summary").click()
    card.locator(":scope > .admin-add").scroll_into_view_if_needed()
    page.screenshot(path=str(tmp_path / "administration-add-value.png"))


def test_columns_and_restrictions_save_payloads_counts_and_reset(page, admin_gui):
    open_admin(page, admin_gui)
    records = []
    page.on(
        "request",
        lambda request: records.append(request.post_data_json) if request.method == "PUT" else None,
    )
    columns = page.locator("#admin-columns-form")
    codes = ["project-code", "person"]
    for code in codes:
        columns.locator(f'[data-code="{code}"] input').check()
    row = columns.locator('[data-code="project-code"]')
    row.get_by_role("button", name="Move Synthetic projects down").click()
    expect(row.locator("input")).to_be_focused()
    save_form(columns)
    assert records[-1] == {"dimension_codes": ["person", "project-code"]}
    for kind in ["category", "tag", "dimension_value"]:
        picker = page.locator(f'.admin-picker[data-kind="{kind}"]')
        picker.locator("summary").click()
        checkbox = picker.locator('input[type="checkbox"]').first
        code = checkbox.get_attribute("data-option-code")
        checkbox.check()
        expect(picker.locator("summary")).to_contain_text("1")
        save_form(picker.locator("form"))
        assert records[-1] == {"field_kind": kind, "codes": [code]}
        picker.get_by_role("button", name="Allow all", exact=True).click()
        expect(picker.locator("summary")).to_contain_text("all allowed")
        assert records[-1] == {"field_kind": kind, "codes": []}
    page.locator('[data-tab="transactions"]').click()
    page.locator('[data-tab="admin"]').click()
    expect(page.locator("#admin-columns-form input:checked")).to_have_count(2)
    assert (
        page.locator("#admin-columns-form .admin-column-row").first.get_attribute("data-code")
        == "person"
    )


def test_version_conflict_requires_explicit_editor_reload_preserving_other_drafts(page, admin_gui):
    open_admin(page, admin_gui)
    card = dimension_card(page)
    form = card.locator(":scope > form")
    field = form.locator("input:not([type])")
    field.fill("Conflicting draft")
    page.locator("#admin-add-tag > summary").click()
    page.locator("#admin-add-tag-name").fill("Keep this other draft")
    path = "**/api/dimensions/project-code"
    page.route(
        path,
        lambda route: route.fulfill(
            status=409, json={"ok": False, "error": {"code": "version_conflict"}}
        ),
    )
    form.locator('button[type="submit"]').click()
    expect(form.locator('[role="alert"]')).to_be_visible()
    expect(form.locator('button[type="submit"]')).to_be_disabled()
    page.once("dialog", lambda prompt: prompt.dismiss())
    form.get_by_role("button", name="Reload this editor").click()
    expect(field).to_have_value("Conflicting draft")
    page.once("dialog", lambda prompt: prompt.accept())
    form.get_by_role("button", name="Reload this editor").click()
    expect(field).to_have_value("Synthetic projects")
    expect(page.locator("#admin-add-tag-name")).to_have_value("Keep this other draft")
    page.unroute(path)
    field.fill("Reviewed new name")
    save_form(form)
    expect(card.locator(":scope > summary")).to_contain_text("Reviewed new name")


def test_late_write_stays_bound_to_original_account_without_overwriting_new_view(page, admin_gui):
    open_admin(page, admin_gui)
    _, _, accounts, _, _ = admin_gui
    held = []
    page.route(f"**/api/accounts/{accounts[0]['id']}", lambda route: held.append(route))
    page.locator("#admin-account-name").fill("First captured write")
    page.locator('#admin-account-form button[type="submit"]').click()
    expect(page.locator("#admin-account-form")).to_have_attribute("aria-busy", "true")
    page.evaluate(
        "id => window.dispatchEvent(new CustomEvent('dagoberd:navigate', {detail:{accountIds:[id],tab:'admin'}}))",
        accounts[1]["id"],
    )
    expect(page.locator("#admin-account-name")).to_have_value("Synthetic second")
    assert held[0].request.post_data_json["display_name"] == "First captured write"
    held[0].fulfill(response=held[0].fetch())
    expect(page.locator('#admin-account-form button[type="submit"]')).to_be_enabled()
    expect(page.locator("#admin-account-name")).to_have_value("Synthetic second")
    expect(page.locator("#account-scope")).to_have_text("Synthetic second")
    expect(page.locator(".admin-page [role=status]")).to_have_count(0)
    page.unroute(f"**/api/accounts/{accounts[0]['id']}")
    page.evaluate(
        "id => window.dispatchEvent(new CustomEvent('dagoberd:navigate', {detail:{accountIds:[id],tab:'admin'}}))",
        accounts[0]["id"],
    )
    expect(page.locator("#admin-account-name")).to_have_value("First captured write")


def test_late_section_response_cannot_replace_new_account_section(page, admin_gui):
    _, _, accounts, _, _ = admin_gui
    held = []
    page.route(f"**/api/accounts/{accounts[0]['id']}/columns", lambda route: held.append(route))
    base = admin_gui[0]
    page.goto(base + f"?tab=admin&account={accounts[0]['id']}#t=admin-token")
    expect(page.locator("#admin-columns [role=status]")).to_have_text("Loading this section…")
    page.evaluate(
        "id => window.dispatchEvent(new CustomEvent('dagoberd:navigate', {detail:{accountIds:[id],tab:'admin'}}))",
        accounts[1]["id"],
    )
    expect(page.locator("#admin-columns-form")).to_be_visible()
    held[0].fulfill(json={"ok": True, "data": [{"code": "project-code"}]})
    expect(page.locator("#admin-columns-form input:checked")).to_have_count(0)
    expect(page.locator("#admin-account-name")).to_have_value("Synthetic second")


def test_deactivated_account_is_hidden_and_can_be_reactivated(page, admin_gui):
    _, organization, accounts, _, _ = admin_gui
    identifier = accounts[1]["id"]
    organization.repository.set_account_enabled(identifier, False)
    open_admin(page, admin_gui)
    page.locator("#account-scope").click()
    expect(page.locator(".scope-dialog")).to_be_visible()
    expect(page.locator(".scope-dialog")).not_to_contain_text("Synthetic second")
    page.locator(".scope-dialog").get_by_role("button", name="Cancel", exact=True).click()
    panel = page.locator("#admin-inactive-accounts")
    panel.locator("summary").click()
    expect(panel).to_contain_text("Synthetic second")
    panel.get_by_role("button", name="Reactivate", exact=True).click()
    expect(panel).to_contain_text("No deactivated accounts.")
    assert organization.repository.get_account(identifier)["enabled"]
    page.locator("#account-scope").click()
    expect(page.locator(".scope-dialog")).to_contain_text("Synthetic second")
