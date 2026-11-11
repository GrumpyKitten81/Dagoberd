from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import expect
from test_workspace_picker import gui as gui
from test_workspace_picker import page as page

PREVIEWS = {
    "git-pull": "git-run",
    "git-commit": "git-run",
    "git-push": "git-run",
    "sync-publish": "sync-run",
    "sync-restore": "sync-run",
    "cloud-preview": "cloud-run",
}
CONFIRMED = {
    "git-init",
    "git-remote",
    "git-disconnect",
    "git-trust",
    "sync-configure",
    "sync-disconnect",
    "snapshot-adopt",
    "cloud-recover",
    "adopt-credentials",
}
FIELDS = {
    "create": ["newName", "directory", "cloud"],
    "init": ["newName", "directory", "cloud"],
    "add": ["newName", "configuration"],
    "status": ["workspace"],
    "remove": ["workspace"],
    "git-init": ["workspace"],
    "git-status": ["workspace"],
    "git-remote": ["workspace", "remote"],
    "git-disconnect": ["workspace"],
    "git-trust": ["workspace"],
    "git-pull": ["workspace"],
    "git-commit": ["workspace", "files", "message"],
    "git-push": ["workspace"],
    "clone": ["newName", "directory", "remote", "cloud"],
    "sync-status": ["workspace"],
    "sync-configure": ["workspace", "snapshotRoot"],
    "sync-disconnect": ["workspace"],
    "sync-publish": ["workspace"],
    "sync-restore": ["workspace"],
    "snapshot-inspect": ["snapshot"],
    "snapshot-adopt": ["newName", "directory", "snapshot", "fingerprint"],
    "cloud-preview": ["workspace", "destination"],
    "cloud-recover": ["workspace"],
    "adopt-credentials": ["workspace", "credentials"],
}
VALUES = {
    "newName": "new-workspace",
    "directory": "/synthetic/new",
    "configuration": "/synthetic/dagoberd.toml",
    "cloud": "/synthetic/documents",
    "remote": "https://example.invalid/private.git",
    "files": "dagoberd.toml\n README.md \n",
    "message": "Synthetic commit",
    "snapshotRoot": "/synthetic/snapshots",
    "snapshot": "/synthetic/revision.json",
    "fingerprint": "synthetic-fingerprint",
    "destination": "/synthetic/cloud",
    "credentials": "comdirect.test.username\ncomdirect.test.client-id",
}
KEYS = {
    "newName": "name",
    "workspace": "name",
    "directory": "path",
    "configuration": "path",
    "cloud": "cloud_root",
    "snapshotRoot": "cloud_root",
    "destination": "cloud_root",
    "remote": "remote",
    "files": "files",
    "credentials": "files",
    "message": "message",
    "snapshot": "snapshot",
    "fingerprint": "fingerprint",
}


def open_management(page, gui, *, selected=True):
    base, entries = gui
    query = f"?workspace={entries[0]['id']}" if selected else ""
    page.goto(base + query + "#t=picker-token")
    expect(page.locator("#workspace-switcher")).to_be_visible()
    if selected:
        expect(page.locator(".kpi-grid")).to_be_visible()
    else:
        expect(page.locator("main h1")).to_have_text("Choose workspace")
    page.locator("#workspace-switcher").click()
    page.locator("#workspace-picker footer button").first.click()
    dialog = page.locator(".workspace-management[open]")
    expect(dialog).to_be_visible()
    return dialog


def choose(dialog, group, action):
    dialog.locator("#workspace-management-group").select_option(group)
    dialog.locator("#workspace-management-action").select_option(action)


def fill_action(dialog, action, workspace):
    for field in FIELDS[action]:
        control = dialog.locator(f"#workspace-management-{field}")
        if field == "workspace":
            control.select_option(workspace)
        else:
            control.fill(VALUES[field])


def submit(dialog):
    return dialog.locator('footer button[type="submit"]')


def execute(dialog):
    return dialog.locator('footer button.primary[type="button"]')


def test_all_actions_are_reachable_with_only_their_payload_fields(page, gui):
    _, entries = gui
    workspace = entries[0]["id"]
    records = []
    confirmations = []
    page.on("dialog", lambda dialog: (confirmations.append(dialog.message), dialog.accept()))

    def respond(route):
        payload = route.request.post_data_json
        records.append(payload)
        result = {"workspace_id": workspace, "valid": True}
        if payload["action"] in PREVIEWS:
            result |= {"preview_id": "synthetic-preview", "fingerprint": "synthetic-fingerprint"}
        route.fulfill(json={"ok": True, "data": result})

    page.route("**/api/workspace-actions", respond)
    dialog = open_management(page, gui)
    reached = []
    for group in ["local", "git", "snapshots", "cloud", "credentials"]:
        dialog.locator("#workspace-management-group").select_option(group)
        actions = dialog.locator("#workspace-management-action option").evaluate_all(
            "nodes => nodes.map(n => n.value)"
        )
        for action in actions:
            dialog.locator("#workspace-management-action").select_option(action)
            fill_action(dialog, action, workspace)
            previous = len(records)
            submit(dialog).click()
            expect(dialog).to_have_attribute("aria-busy", "false")
            assert len(records) == previous + 1, action
            expected = {"action": action}
            for field in FIELDS[action]:
                value = workspace if field == "workspace" else VALUES[field].strip()
                expected[KEYS[field]] = (
                    [line.strip() for line in value.splitlines() if line.strip()]
                    if field in {"files", "credentials"}
                    else value
                )
            if action in CONFIRMED:
                expected["confirmed"] = True
            assert records[-1] == expected, action
            reached.append(action)
    assert set(reached) == set(FIELDS) and len(reached) == 24
    assert len(confirmations) == len(CONFIRMED)


def test_drafts_validation_and_preview_invalidation(page, gui):
    _, entries = gui
    page.route(
        "**/api/workspace-actions",
        lambda route: route.fulfill(
            json={
                "ok": True,
                "data": {
                    "preview_id": "preview-one",
                    "fingerprint": "fingerprint-one",
                    "files": ["dagoberd.toml"],
                },
            }
        ),
    )
    dialog = open_management(page, gui)
    expect(submit(dialog)).to_be_disabled()
    choose(dialog, "git", "git-commit")
    fill_action(dialog, "git-commit", entries[0]["id"])
    submit(dialog).click()
    expect(execute(dialog)).to_be_visible()
    dialog.locator("#workspace-management-message").fill("Changed message")
    expect(execute(dialog)).to_be_hidden()
    submit(dialog).click()
    expect(execute(dialog)).to_be_visible()
    dialog.locator("#workspace-management-workspace").select_option(entries[1]["id"])
    expect(execute(dialog)).to_be_hidden()
    choose(dialog, "local", "status")
    dialog.locator("#workspace-management-workspace").select_option(entries[0]["id"])
    choose(dialog, "git", "git-commit")
    expect(dialog.locator("#workspace-management-message")).to_have_value("Changed message")
    expect(dialog.locator("#workspace-management-workspace")).to_have_value(entries[1]["id"])
    expect(execute(dialog)).to_be_hidden()


def test_busy_state_and_late_preview_cannot_override_new_form_state(page, gui):
    _, entries = gui
    held = []
    page.route("**/api/workspace-actions", lambda route: held.append(route))
    dialog = open_management(page, gui)
    choose(dialog, "git", "git-commit")
    fill_action(dialog, "git-commit", entries[0]["id"])
    submit(dialog).click()
    expect(dialog).to_have_attribute("aria-busy", "true")
    expect(dialog.locator("#workspace-management-group")).to_be_disabled()
    expect(dialog.locator("#workspace-management-message")).to_be_disabled()
    expect(dialog.get_by_role("button", name="Close", exact=True)).to_be_disabled()
    page.keyboard.press("Escape")
    expect(dialog).to_be_visible()
    dialog.evaluate(
        "node => { const input = node.querySelector('#workspace-management-workspace'); input.value = input.options[2].value; input.dispatchEvent(new Event('change', {bubbles:true})); }"
    )
    assert held[0].request.post_data_json["name"] == entries[0]["id"]
    held[0].fulfill(
        json={"ok": True, "data": {"preview_id": "late-preview", "fingerprint": "late-fingerprint"}}
    )
    expect(dialog).to_have_attribute("aria-busy", "false")
    expect(execute(dialog)).to_be_hidden()
    expect(dialog.locator(".workspace-result")).to_have_count(0)
    expect(dialog.get_by_role("button", name="Close", exact=True)).to_be_enabled()


@pytest.mark.parametrize(
    "action,group",
    [("git-commit", "git"), ("sync-restore", "snapshots"), ("cloud-preview", "cloud")],
)
def test_execution_uses_explicit_binding_once_and_requires_new_preview_after_failure(
    page, gui, action, group
):
    _, entries = gui
    records = []
    page.on("dialog", lambda dialog: dialog.accept())

    def respond(route):
        payload = route.request.post_data_json
        records.append(payload)
        if payload["action"] == action:
            # cloud_root is deliberately present for every family: it must not select cloud-run.
            route.fulfill(
                json={
                    "ok": True,
                    "data": {
                        "preview_id": "bound-preview",
                        "fingerprint": "bound-fingerprint",
                        "cloud_root": "/synthetic/cloud",
                    },
                }
            )
        else:
            route.fulfill(
                status=400,
                json={
                    "ok": False,
                    "error": {
                        "code": "invalid_request",
                        "message": "State changed; create a new preview",
                    },
                },
            )

    page.route("**/api/workspace-actions", respond)
    dialog = open_management(page, gui)
    choose(dialog, group, action)
    fill_action(dialog, action, entries[0]["id"])
    submit(dialog).click()
    expect(execute(dialog)).to_be_visible()
    execute(dialog).click()
    expect(dialog.locator('[role="alert"]')).to_contain_text("State changed")
    expect(execute(dialog)).to_be_hidden()
    dialog.locator('footer button.primary[type="button"]').evaluate("button => button.click()")
    assert records[-1] == {
        "action": PREVIEWS[action],
        "name": entries[0]["id"],
        "preview_id": "bound-preview",
        "fingerprint": "bound-fingerprint",
        "confirmed": True,
    }
    assert len(records) == 2


@pytest.mark.parametrize("failure", ["network", "server"])
def test_inline_errors_keep_fields_and_reenable_controls(page, gui, failure):
    def fail(route):
        if failure == "network":
            route.abort("failed")
        else:
            route.fulfill(status=500, content_type="text/plain", body="Private server traceback")

    page.route("**/api/workspace-actions", fail)
    dialog = open_management(page, gui)
    dialog.locator("#workspace-management-newName").fill("new-draft")
    dialog.locator("#workspace-management-directory").fill("/synthetic/draft")
    submit(dialog).click()
    expect(dialog.locator('[role="alert"]')).to_be_visible()
    expect(dialog).to_have_attribute("aria-busy", "false")
    expect(dialog.locator("#workspace-management-newName")).to_have_value("new-draft")
    expect(dialog.locator("#workspace-management-directory")).to_have_value("/synthetic/draft")
    expect(dialog.get_by_role("button", name="Close", exact=True)).to_be_enabled()
    expect(dialog.locator('[role="alert"]')).not_to_contain_text("Private server traceback")


@pytest.mark.parametrize("action", ["create", "add"])
def test_create_and_register_show_result_then_open_explicitly(page, gui, tmp_path, action):
    _, entries = gui
    dialog = open_management(page, gui)
    if action == "add":
        config = tmp_path / "existing" / "dagoberd.toml"
        config.parent.mkdir()
        config.write_text('[database]\npath="ledger.db"\n', encoding="utf-8")
        dialog.locator("#workspace-management-action").select_option("add")
        dialog.locator("#workspace-management-configuration").fill(str(config))
    else:
        dialog.locator("#workspace-management-directory").fill(str(tmp_path / "created"))
    dialog.locator("#workspace-management-newName").fill("new-workspace")
    original_url = page.url
    submit(dialog).click()
    open_button = dialog.get_by_role("button", name="Open workspace", exact=True)
    expect(open_button).to_be_visible()
    assert page.url == original_url
    assert "new-workspace" in page.locator("#workspace-picker").text_content()
    assert page.locator('#workspace-picker [role="menuitemradio"]').count() == 3
    page.once("dialog", lambda prompt: prompt.dismiss())
    open_button.click()
    expect(dialog).to_be_visible()
    assert page.url == original_url
    page.once("dialog", lambda prompt: prompt.accept())
    open_button.click()
    expect(page.locator(".kpi-grid")).to_be_visible()
    assert f"workspace={entries[0]['id']}" not in page.url
    expect(page.locator("#workspace-switcher")).to_contain_text("new-workspace")


@pytest.mark.parametrize("gui", ["empty"], indirect=True)
def test_first_creation_does_not_auto_navigate_on_registry_refresh(page, gui, tmp_path):
    dialog = open_management(page, gui, selected=False)
    dialog.locator("#workspace-management-newName").fill("first-workspace")
    dialog.locator("#workspace-management-directory").fill(str(tmp_path / "first-created"))
    original_url = page.url
    submit(dialog).click()
    expect(dialog.get_by_role("button", name="Open workspace")).to_be_visible()
    dialog.get_by_role("button", name="Close", exact=True).click()
    assert page.url == original_url
    expect(page.locator("main h1")).to_have_text("Choose workspace")
    expect(page.locator("#refresh")).to_be_disabled()
    page.locator("#workspace-switcher").click()
    expect(page.get_by_role("menuitemradio", name="first-workspace")).to_be_visible()


def test_removing_active_registration_gates_financial_view_without_deleting_files(page, gui):
    _, entries = gui
    config = Path(entries[0]["config_path"])
    dialog = open_management(page, gui)
    dialog.locator("#workspace-management-action").select_option("remove")
    submit(dialog).click()
    expect(dialog.locator(".workspace-result")).to_contain_text("Original files preserved")
    expect(page.locator(".kpi-grid")).to_have_count(0)
    expect(page.locator("#refresh")).to_be_disabled()
    expect(page.locator("#workspace-switcher")).to_contain_text("Workspace unavailable")
    assert config.is_file()
    assert page.locator('#workspace-picker [role="menuitemradio"]').count() == 1


@pytest.mark.parametrize(
    "action,group",
    [
        ("git-pull", "git"),
        ("sync-restore", "snapshots"),
        ("cloud-preview", "cloud"),
        ("cloud-recover", "cloud"),
        ("git-init", "git"),
        ("git-remote", "git"),
        ("git-disconnect", "git"),
        ("git-trust", "git"),
        ("sync-configure", "snapshots"),
        ("sync-disconnect", "snapshots"),
        ("adopt-credentials", "credentials"),
    ],
)
def test_active_workspace_data_changes_reload_only_after_closing(page, gui, action, group):
    _, entries = gui
    page.on("dialog", lambda dialog: dialog.accept())
    records = []

    def respond(route):
        payload = route.request.post_data_json
        records.append(payload)
        result = {"workspace_id": entries[0]["id"], "recovered": True}
        if payload["action"] == action and action in PREVIEWS:
            result |= {"preview_id": "reload-preview", "fingerprint": "reload-fingerprint"}
        route.fulfill(json={"ok": True, "data": result})

    page.route("**/api/workspace-actions", respond)
    dialog = open_management(page, gui)
    choose(dialog, group, action)
    fill_action(dialog, action, entries[0]["id"])
    navigations = []
    page.on("framenavigated", lambda frame: navigations.append(frame.url))
    submit(dialog).click()
    if action in PREVIEWS:
        expect(execute(dialog)).to_be_visible()
        execute(dialog).click()
    expect(dialog.locator(".workspace-result h3")).to_have_text("Action completed")
    assert navigations == []
    with page.expect_navigation():
        dialog.get_by_role("button", name="Close", exact=True).click()
    expect(page.locator(".kpi-grid")).to_be_visible()
    assert f"workspace={entries[0]['id']}" in page.url
    assert len(records) == (2 if action in PREVIEWS else 1)


@pytest.mark.parametrize("width", [320, 800, 1280])
@pytest.mark.parametrize("language", ["en", "de", "es"])
def test_long_preview_labels_and_footer_fit_all_languages(page, gui, width, language):
    _, entries = gui
    page.set_viewport_size({"width": width, "height": 700})
    page.add_init_script(f"localStorage.setItem('dagoberd.gui-language', '{language}')")
    root = "/synthetic/" + "long-directory-" * 12
    preview = {
        "preview_id": "long-preview",
        "fingerprint": "long-fingerprint",
        "workspace_id": entries[0]["id"],
        "cloud_root": root,
        "destination": root + "/workspaces/synthetic",
        "files": [
            {
                "source": root + f"/receipt-{index}.pdf",
                "relative": f"documents/receipt-{index}.pdf",
                "sha256": "a" * 64,
            }
            for index in range(20)
        ],
    }
    page.route(
        "**/api/workspace-actions", lambda route: route.fulfill(json={"ok": True, "data": preview})
    )
    dialog = open_management(page, gui)
    choose(dialog, "cloud", "cloud-preview")
    fill_action(dialog, "cloud-preview", entries[0]["id"])
    submit(dialog).click()
    expect(execute(dialog)).to_be_visible()
    expect(dialog.locator(".workspace-result-facts")).to_contain_text(
        root + "/workspaces/synthetic"
    )
    expect(dialog.locator(".workspace-file-list li")).to_have_count(20)
    expect(dialog.locator(".workspace-technical")).not_to_have_attribute("open", "")
    assert dialog.locator(".workspace-file-list").evaluate(
        "node => node.scrollWidth <= node.clientWidth"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    bounds = dialog.bounding_box()
    footer = dialog.locator("footer").bounding_box()
    assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width
    assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= 700
    assert footer["y"] >= 0 and footer["y"] + footer["height"] <= 700


def test_registry_refresh_failure_does_not_turn_completed_action_into_a_failed_action(page, gui):
    dialog = open_management(page, gui)
    page.route("**/api/workspaces", lambda route: route.abort("failed"))
    page.route(
        "**/api/workspace-actions",
        lambda route: route.fulfill(json={"ok": True, "data": {"id": "synthetic-created"}}),
    )
    dialog.locator("#workspace-management-newName").fill("new-workspace")
    dialog.locator("#workspace-management-directory").fill("/synthetic/new")
    submit(dialog).click()
    expect(dialog.locator(".workspace-result h3")).to_have_text("Action completed")
    expect(dialog.get_by_role("button", name="Open workspace")).to_be_visible()
    expect(dialog.locator('[role="alert"]:visible')).to_contain_text(
        "workspace list could not be refreshed"
    )


def test_cancelled_confirmation_does_not_send_a_write_or_consume_preview(page, gui):
    _, entries = gui
    records = []
    page.route(
        "**/api/workspace-actions",
        lambda route: (
            records.append(route.request.post_data_json),
            route.fulfill(
                json={
                    "ok": True,
                    "data": {"preview_id": "keep-preview", "fingerprint": "keep-fingerprint"},
                }
            ),
        ),
    )
    dialog = open_management(page, gui)
    choose(dialog, "git", "git-init")
    page.once("dialog", lambda prompt: prompt.dismiss())
    submit(dialog).click()
    assert records == []
    choose(dialog, "git", "git-commit")
    fill_action(dialog, "git-commit", entries[0]["id"])
    submit(dialog).click()
    expect(execute(dialog)).to_be_visible()
    page.once("dialog", lambda prompt: prompt.dismiss())
    execute(dialog).click()
    assert len(records) == 1
    expect(execute(dialog)).to_be_visible()


def test_new_workspace_name_validation_matches_registry_constraints(page, gui):
    dialog = open_management(page, gui)
    dialog.locator("#workspace-management-directory").fill("/synthetic/new")
    name = dialog.locator("#workspace-management-newName")
    for invalid in ["UPPERCASE", "contains spaces", "-leading-hyphen", "a" * 65]:
        name.fill(invalid)
        expect(submit(dialog)).to_be_disabled()
    name.fill("valid-name-1")
    expect(submit(dialog)).to_be_enabled()
    dialog.locator("#workspace-management-directory").fill("   ")
    expect(submit(dialog)).to_be_disabled()


def test_management_focus_stays_inside_dialog_and_returns_to_workspace_control(page, gui):
    dialog = open_management(page, gui)
    first = dialog.locator("#workspace-management-group")
    close = dialog.get_by_role("button", name="Close", exact=True)
    first.focus()
    page.keyboard.press("Shift+Tab")
    expect(close).to_be_focused()
    page.keyboard.press("Tab")
    expect(first).to_be_focused()
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
    expect(page.locator("#workspace-switcher")).to_be_focused()


def test_preview_for_another_workspace_is_not_executable(page, gui):
    _, entries = gui
    page.route(
        "**/api/workspace-actions",
        lambda route: route.fulfill(
            json={
                "ok": True,
                "data": {
                    "preview_id": "wrong-scope",
                    "fingerprint": "wrong-fingerprint",
                    "workspace_id": entries[1]["id"],
                },
            }
        ),
    )
    dialog = open_management(page, gui)
    choose(dialog, "git", "git-push")
    submit(dialog).click()
    expect(dialog.locator('[role="alert"]')).to_contain_text("usable preview")
    expect(execute(dialog)).to_be_hidden()
