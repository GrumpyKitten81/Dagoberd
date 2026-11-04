from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
import tomlkit
from fastapi.testclient import TestClient

from dagoberd.cli import main
from dagoberd.config import initialize_project_config, load_config
from dagoberd.documents.matching import DocumentMatcher
from dagoberd.gui.workspaces import create_workspace_app
from dagoberd.ledger.database import LedgerDatabase
from dagoberd.ledger.organization import OrganizationService
from dagoberd.ledger.repository import LedgerRepository
from dagoberd.mcp_server import MCPService
from dagoberd.reporting.ledger_reports import LedgerReportGenerator
from dagoberd.secrets import SecretStore
from dagoberd.services.import_service import ImportService
from dagoberd.services.portability import PortabilityService
from dagoberd.workspace_cloud import WorkspaceCloud, recover_cloud
from dagoberd.workspace_git import WorkspaceGit, clone_workspace, git
from dagoberd.workspaces import WorkspaceRegistry, resolve_workspace, workspace_operation


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.delenv("ACCOUNT_EVAL_DATA_ROOT", raising=False)
    monkeypatch.delenv("ACCOUNT_EVAL_CLOUD_WORKSPACE", raising=False)
    path = tmp_path / "settings" / "workspaces.toml"
    monkeypatch.setenv("ACCOUNT_EVAL_WORKSPACE_REGISTRY", str(path))
    return WorkspaceRegistry(path)


def test_initialize_repository_preserves_files_and_is_repeatable(registry, tmp_path):
    root = tmp_path / "provided"
    root.mkdir()
    original = root / "README.md"
    original.write_text("Existing repository", encoding="utf-8")
    first = registry.initialize("private", root)
    loaded = registry.load("private")
    with workspace_operation(loaded, registry):
        service = OrganizationService(LedgerRepository(LedgerDatabase(loaded.config.database.path)))
        service.create_manual_account("Cash", account_kind="cash", idempotency_key="same-key")
    second = registry.initialize("private", root)
    assert first["id"] == second["id"]
    assert original.read_text(encoding="utf-8") == "Existing repository"
    assert len(LedgerRepository(LedgerDatabase(loaded.config.database.path)).list_accounts()) == 1
    assert ".env" in (root / ".gitignore").read_text(encoding="utf-8")
    registry.remove("private")
    assert loaded.config.database.path.is_file()


def test_legacy_force_init_cannot_reset_workspace_identity(registry, tmp_path, capsys):
    registry.initialize("private", tmp_path / "private")
    path = tmp_path / "private" / "account-eval.toml"
    before = path.read_bytes()
    assert main(["--config", str(path), "init", "--force"]) == 1
    assert "identity is preserved" in capsys.readouterr().out
    assert path.read_bytes() == before


def test_registration_preserves_ledger_until_first_binding_invalidates_legacy_approval(
    registry, tmp_path
):
    path = initialize_project_config(tmp_path / "legacy" / "account-eval.toml")
    loaded = load_config(path)
    repo = LedgerRepository(LedgerDatabase(loaded.config.database.path))
    source = tmp_path / "synthetic.csv"
    source.write_text("fecha;concepto;importe\n01/07/2026;Synthetic;10,00\n", encoding="utf-8")
    preview = ImportService(repo).preview("tabular", source)
    registry.add("legacy", path)
    assert repo.get_import_preview(preview.preview_id)["status"] == "prepared"
    with workspace_operation(registry.load("legacy"), registry):
        bound = LedgerRepository(LedgerDatabase(loaded.config.database.path))
        assert bound.get_import_preview(preview.preview_id)["status"] == "failed"
        with pytest.raises(ValueError):
            ImportService(bound).execute(preview.preview_id)


def test_cloud_namespaces_and_explicit_selection(registry, tmp_path):
    cloud = tmp_path / "cloud"
    first = registry.initialize("private", tmp_path / "private", cloud_root=cloud)
    second = registry.initialize("business", tmp_path / "business", cloud_root=cloud)
    for entry in (first, second):
        loaded = registry.load(entry["id"])
        expected = cloud / "workspaces" / entry["id"]
        assert loaded.config.documents.storage_dir == expected / "documents"
        assert loaded.config.reporting.output_dir == expected / "reports"
        assert not loaded.config.database.path.is_relative_to(cloud)
    with pytest.raises(ValueError, match="explicitly"):
        resolve_workspace(registry=registry)
    assert resolve_workspace("business", registry=registry).config.workspace.id
    with pytest.raises(ValueError, match="either"):
        resolve_workspace(
            "private", str(tmp_path / "private" / "account-eval.toml"), registry=registry
        )
    with pytest.raises(ValueError, match="overrides"):
        resolve_workspace("private", db_path="other.db", registry=registry)


def test_legacy_configuration_alias_uses_registered_identity_and_rejects_overrides(
    registry, tmp_path
):
    registry.initialize("private", tmp_path / "private")
    registered = registry.load("private")
    legacy = tmp_path / "legacy" / "account-eval.toml"
    legacy.parent.mkdir()
    raw = registered.config.model_dump(mode="json", exclude_none=True, exclude={"workspace"})
    legacy.write_text(tomlkit.dumps(raw), encoding="utf-8")
    selected = resolve_workspace(config_path=legacy, registry=registry)
    assert selected.config.workspace.id == registered.config.workspace.id
    assert selected.project_path == registered.project_path
    with pytest.raises(ValueError, match="overrides"):
        resolve_workspace(
            config_path=legacy, db_path=str(registered.config.database.path), registry=registry
        )
    raw["privacy"]["mask_visible_characters"] = 0
    legacy.write_text(tomlkit.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="registered"):
        resolve_workspace(config_path=legacy, registry=registry)


def test_reject_duplicate_identity_and_path_collision(registry, tmp_path):
    registry.initialize("first", tmp_path / "first")
    other = tmp_path / "other"
    other.mkdir()
    shutil.copy(tmp_path / "first" / "account-eval.toml", other / "account-eval.toml")
    with pytest.raises(ValueError, match="identity"):
        registry.add("duplicate", other / "account-eval.toml")
    (other / "account-eval.toml").write_text(
        "[documents]\nstorage_dir = " + json.dumps(str(tmp_path / "first" / "output" / "nested")),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="overlaps"):
        registry.add("collision", other / "account-eval.toml")


def test_cloud_overrides_and_global_environment_cannot_escape(registry, tmp_path, monkeypatch):
    registry.initialize("first", tmp_path / "first", cloud_root=tmp_path / "cloud")
    local = tmp_path / "first" / "account-eval.local.toml"
    local.write_text(
        local.read_text(encoding="utf-8") + '\n[documents]\nstorage_dir = "elsewhere"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="namespace"):
        registry.load("first")
    local.unlink()
    monkeypatch.setenv("ACCOUNT_EVAL_CLOUD_WORKSPACE", str(tmp_path / "unscoped"))
    with pytest.raises(ValueError, match="environment"):
        registry.load("first")


def test_legacy_cloud_workspace_does_not_move(tmp_path, monkeypatch):
    monkeypatch.delenv("ACCOUNT_EVAL_CLOUD_WORKSPACE", raising=False)
    config = tmp_path / "account-eval.toml"
    config.write_text('[cloud]\nworkspace = "cloud"\n', encoding="utf-8")
    assert load_config(config).config.documents.storage_dir == tmp_path / "cloud" / "documents"
    config.write_text('[cloud]\nworkspace = "cloud"\nroot = "shared"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="either"):
        load_config(config)


def test_secret_namespaces_and_dotenv_do_not_leak(registry, tmp_path, monkeypatch):
    monkeypatch.setenv("SYNTHETIC_TEST_SECRET", "global")
    stores = []
    for name in ("private", "business"):
        registry.initialize(name, tmp_path / name)
        (tmp_path / name / ".env").write_text(f"SYNTHETIC_TEST_SECRET={name}\n", encoding="utf-8")
        with workspace_operation(registry.load(name), registry):
            store = SecretStore()
            stores.append(store)
            monkeypatch.setattr(store, "get_keyring", lambda name: None)
            import keyring

            monkeypatch.setattr(keyring, "get_password", lambda *args: None)
            assert store.get("SYNTHETIC_TEST_SECRET") == name
    assert stores[0].service != stores[1].service
    assert os.environ["SYNTHETIC_TEST_SECRET"] == "global"


def test_cli_and_mcp_require_workspace_and_keep_identical_accounts_separate(
    registry, tmp_path, capsys
):
    for name in ("private", "business"):
        registry.initialize(name, tmp_path / name)
        with workspace_operation(registry.load(name), registry):
            loaded = registry.load(name)
            service = OrganizationService(
                LedgerRepository(LedgerDatabase(loaded.config.database.path))
            )
            service.create_manual_account("Cash", account_kind="cash", idempotency_key="same-key")
    assert main(["accounts", "list"]) == 1
    assert "explicitly" in capsys.readouterr().out
    assert main(["--workspace", "private", "accounts", "list"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 1
    mcp = MCPService(registry_path=str(registry.path))
    assert len(mcp.execute("list_workspaces", {})["workspaces"]) == 2
    with pytest.raises(ValueError, match="explicitly"):
        mcp.execute("list_accounts", {})
    for entry in registry.read().workspaces:
        result = mcp.execute("list_accounts", {"workspace_id": entry.id})
        assert result


def test_gui_scoped_requests_and_jobs_are_isolated(registry, tmp_path):
    entries = [registry.initialize(name, tmp_path / name) for name in ("private", "business")]
    for index, entry in enumerate(entries):
        loaded = registry.load(entry["id"])
        with workspace_operation(loaded, registry):
            OrganizationService(
                LedgerRepository(LedgerDatabase(loaded.config.database.path))
            ).create_manual_account(
                f"Cash {index}", account_kind="cash", idempotency_key="same-key"
            )
    client = TestClient(create_workspace_app(registry, "token"))
    headers = {"X-AccountEval-Token": "token"}
    assert client.get("/api/workspaces").status_code == 401
    assert client.get("/api/meta", headers=headers).status_code == 400
    for index, entry in enumerate(entries):
        response = client.get(f"/api/workspaces/{entry['id']}/meta", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["accounts"][0]["display_name"] == f"Cash {index}"
        assert (
            client.get(f"/api/workspaces/{entry['id']}/jobs/foreign", headers=headers).status_code
            == 404
        )


def test_workspace_lock_blocks_git_and_restore(registry, tmp_path):
    registry.initialize("private", tmp_path / "private")
    loaded = registry.load("private")
    with (
        workspace_operation(loaded, registry),
        pytest.raises(ValueError, match="busy"),
        workspace_operation(loaded, registry, exclusive=True),
    ):
        pass


def test_copied_ledger_cannot_reuse_foreign_previews(registry, tmp_path):
    registry.initialize("first", tmp_path / "first")
    registry.initialize("second", tmp_path / "second")
    first = registry.load("first")
    second = registry.load("second")
    shutil.copy(first.config.database.path, second.config.database.path)
    with (
        pytest.raises(ValueError, match="another workspace"),
        workspace_operation(second, registry),
    ):
        LedgerRepository(LedgerDatabase(second.config.database.path))


def test_cloud_relocation_verifies_files_and_preserves_originals(registry, tmp_path):
    registry.initialize("private", tmp_path / "private")
    loaded = registry.load("private")
    source = loaded.config.documents.storage_dir / "synthetic.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("Synthetic receipt", encoding="utf-8")
    report = LedgerReportGenerator(
        LedgerRepository(LedgerDatabase(loaded.config.database.path)),
        loaded.config.reporting.output_dir,
    ).generate(formats=("json",))[0]
    cloud = WorkspaceCloud(registry, "private")
    stale = cloud.preview(str(tmp_path / "cloud"))
    source.write_text("Changed synthetic receipt", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        cloud.run(stale["preview_id"], stale["fingerprint"], yes=True)
    preview = cloud.preview(str(tmp_path / "cloud"))
    with pytest.raises(ValueError, match="confirmation"):
        cloud.run(preview["preview_id"], preview["fingerprint"], yes=False)
    result = cloud.run(preview["preview_id"], preview["fingerprint"], yes=True)
    updated = registry.load("private")
    assert (updated.config.documents.storage_dir / source.name).read_bytes() == source.read_bytes()
    assert (updated.config.reporting.output_dir / report.name).read_bytes() == report.read_bytes()
    assert result["original_files_preserved"]
    assert loaded.config.database.path == updated.config.database.path
    with LedgerDatabase(updated.config.database.path).connect() as connection:
        paths = json.loads(
            connection.execute("SELECT output_paths_json FROM ledger_report_runs").fetchone()[0]
        )
    assert paths == [str(updated.config.reporting.output_dir / report.name)]


def test_restore_foreign_workspace_is_rejected_before_replacing_data(registry, tmp_path):
    registry.initialize("private", tmp_path / "private")
    registry.initialize("business", tmp_path / "business")
    private = registry.load("private")
    business = registry.load("business")
    before = business.config.database.path.read_bytes()
    with (
        workspace_operation(business, registry, exclusive=True),
        pytest.raises(ValueError, match="another workspace"),
    ):
        LedgerDatabase(business.config.database.path).restore(private.config.database.path)
    assert business.config.database.path.read_bytes() == before


def test_interrupted_cloud_update_blocks_workflow_and_recovers(registry, tmp_path, monkeypatch):
    from dagoberd import workspace_cloud

    registry.initialize("private", tmp_path / "private")
    loaded = registry.load("private")
    cloud = WorkspaceCloud(registry, "private")
    preview = cloud.preview(str(tmp_path / "cloud"))
    original = workspace_cloud.atomic_text

    def interrupt(path, content):
        if path == loaded.local_path:
            raise SystemExit("Synthetic process interruption")
        return original(path, content)

    monkeypatch.setattr(workspace_cloud, "atomic_text", interrupt)
    with pytest.raises(SystemExit):
        cloud.run(preview["preview_id"], preview["fingerprint"], yes=True)
    with (
        pytest.raises(ValueError, match="incomplete"),
        workspace_operation(registry.load("private"), registry),
    ):
        pass
    monkeypatch.setattr(workspace_cloud, "atomic_text", original)
    assert recover_cloud(registry, "private", yes=True)["recovered"]
    with workspace_operation(registry.load("private"), registry):
        assert registry.load("private").config.cloud.root is None
    assert Path(preview["destination"]).is_dir()


def test_import_classification_documents_reports_and_exports_stay_in_workspace(registry, tmp_path):
    first_preview_id = None
    for name in ("private", "business"):
        registry.initialize(name, tmp_path / name, cloud_root=tmp_path / "cloud")
        loaded = registry.load(name)
        with workspace_operation(loaded, registry):
            repo = LedgerRepository(LedgerDatabase(loaded.config.database.path))
            source = tmp_path / name / "synthetic.csv"
            source.write_text(
                f"fecha;concepto;importe\n01/07/2026;{name};10,00\n", encoding="utf-8"
            )
            imports = ImportService(repo)
            if first_preview_id:
                with pytest.raises(KeyError):
                    imports.execute(first_preview_id)
            preview = imports.preview("tabular", source)
            first_preview_id = first_preview_id or preview.preview_id
            imports.execute(preview.preview_id)
            transaction = repo.list_transactions()[0]
            assert transaction["purpose"] == name
            assert len(repo.list_transactions()) == 1
            repo.add_rule("household", "Synthetic rule", name, "expense.food")
            assert repo.apply_rules(transaction["id"], "household")
            receipt = tmp_path / name / "synthetic.jpg"
            receipt.write_bytes(b"\xff\xd8\xff" + name.encode())
            document = DocumentMatcher(repo, loaded.config.documents.storage_dir).import_file(
                receipt
            )
            assert Path(document["storage_path"]).is_relative_to(
                loaded.config.documents.storage_dir
            )
            report = LedgerReportGenerator(repo, loaded.config.reporting.output_dir).generate(
                formats=("json",)
            )[0]
            payload = json.loads(report.read_text(encoding="utf-8"))
            assert [row["purpose"] for row in payload["transactions"]] == [name]
            service = PortabilityService(
                loaded.config.database.path,
                loaded.config.documents.storage_dir,
                loaded.config.reporting.output_dir,
                loaded.project_path,
            )
            archive = tmp_path / name / "synthetic.aex"
            service.export(archive)
            assert service.verify(archive)["valid"]
    with pytest.raises(ValueError, match="another workspace"):
        registry.validate_output(registry.load("private"), tmp_path / "business" / "report.json")


def test_workspace_agent_launcher_and_handshake_preserve_selection(registry, tmp_path, capsys):
    for name in ("private", "business"):
        registry.initialize(name, tmp_path / name)
    assert main(["--workspace", "private", "agents", "configure"]) == 0
    launcher = json.loads(capsys.readouterr().out)["launcher"]
    assert Path(launcher).parent == tmp_path / "private" / ".account-eval"
    assert main(["--workspace", "private", "agents", "doctor"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["handshake"]["safe_tools_present"]


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_git_preview_commit_push_clone_and_changed_fingerprint(registry, tmp_path):
    root = tmp_path / "private"
    registry.initialize("private", root)
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "synthetic@example.invalid")
    git(root, "config", "user.name", "Synthetic Test")
    git(root, "config", "filter.synthetic.clean", "false")
    (root / ".gitattributes").write_text("*.txt filter=synthetic\n", encoding="utf-8")
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "--initial-branch=main")
    git(root, "remote", "add", "origin", str(remote))
    service = WorkspaceGit(registry, "private")
    with pytest.raises(ValueError, match="private origin"):
        service.preview("commit", ["account-eval.toml"], "Initialize")
    service.trust(yes=True)
    files = ["account-eval.toml", ".gitignore", ".gitattributes", "output/account_eval.db"]
    preview = service.preview("commit", files, "Initialize")
    with pytest.raises(ValueError, match="confirmation"):
        service.run(preview["preview_id"], preview["fingerprint"], yes=False)
    service.run(preview["preview_id"], preview["fingerprint"], yes=True)
    push = service.preview("push")
    service.run(push["preview_id"], push["fingerprint"], yes=True)
    (root / "note.txt").write_text("Before", encoding="utf-8")
    stale = service.preview("commit", ["note.txt"], "Note")
    (root / "note.txt").write_text("After", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        service.run(stale["preview_id"], stale["fingerprint"], yes=True)
    (root / ".env").write_text("SYNTHETIC=value", encoding="utf-8")
    for filename in (".env", ".ENV"):
        with pytest.raises(ValueError, match="Secrets"):
            service.preview("commit", [filename], "Forbidden")
    with pytest.raises(ValueError, match="clean"):
        service.preview("pull")
    other_registry = WorkspaceRegistry(tmp_path / "other-settings" / "workspaces.toml")
    cloned = clone_workspace(other_registry, "copy", str(remote), str(tmp_path / "copy"))
    assert cloned["id"] == registry.entry("private").id
    commit = service.preview("commit", ["note.txt"], "Add synthetic note")
    service.run(commit["preview_id"], commit["fingerprint"], yes=True)
    push = service.preview("push")
    service.run(push["preview_id"], push["fingerprint"], yes=True)
    copy_service = WorkspaceGit(other_registry, "copy")
    pull = copy_service.preview("pull")
    copy_service.run(pull["preview_id"], pull["fingerprint"], yes=True)
    assert (tmp_path / "copy" / "note.txt").read_text(encoding="utf-8") == "After"
