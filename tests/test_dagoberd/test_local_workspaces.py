from __future__ import annotations

import json
import shutil
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from dagoberd.cli import main
from dagoberd.gui.workspaces import WorkspaceAction, create_workspace_app, workspace_action
from dagoberd.ledger.database import LedgerDatabase
from dagoberd.ledger.organization import OrganizationService
from dagoberd.ledger.repository import LedgerRepository
from dagoberd.mcp_server import MCPService
from dagoberd.reporting.ledger_reports import LedgerReportGenerator
from dagoberd.services.import_service import ImportService
from dagoberd.workspace_git import WorkspaceGit, adopt_git, git
from dagoberd.workspace_status import workspace_status
from dagoberd.workspace_sync import (
    WorkspaceSync,
    adopt_snapshot,
    inspect_snapshot,
    validate_snapshot_members,
)
from dagoberd.workspaces import WorkspaceRegistry, workspace_operation


@pytest.fixture
def local_registry(tmp_path, monkeypatch):
    registry = WorkspaceRegistry(tmp_path / "settings" / "workspaces.toml")
    monkeypatch.setenv("DAGOBERD_WORKSPACE_REGISTRY", str(registry.path))
    return registry


def populate(registry, directory):
    registry.initialize("local", directory)
    loaded = registry.load("local")
    with workspace_operation(loaded, registry):
        repo = LedgerRepository(LedgerDatabase(loaded.config.database.path))
        source = directory / "synthetic.csv"
        source.write_text(
            "fecha;concepto;importe;account\n01/07/2026;Synthetic invoice;10,00;SYN-A\n"
        )
        imports = ImportService(repo)
        preview = imports.preview("tabular", source)
        imports.execute(preview.preview_id)
        assert imports.execute(imports.preview("tabular", source).preview_id).imported == 0
        loaded.config.documents.storage_dir.mkdir(parents=True, exist_ok=True)
        (loaded.config.documents.storage_dir / "synthetic.txt").write_text("Synthetic receipt")
        LedgerReportGenerator(repo, loaded.config.reporting.output_dir).generate(formats=("json",))
    return loaded


def exchange(sync, action):
    preview = sync.preview(action)
    return sync.run(preview["preview_id"], preview["fingerprint"], yes=True)


@pytest.mark.parametrize("name", ["other.local.toml", "other.local.toml.bak", ".account-old/state"])
def test_local_settings_and_old_runtime_are_excluded_from_transfers(tmp_path, name):
    service = WorkspaceGit.__new__(WorkspaceGit)
    service.root = tmp_path
    with pytest.raises(ValueError, match="local settings"):
        service._files([name])
    with pytest.raises(ValueError, match="machine-local"):
        validate_snapshot_members([name])


def test_local_cli_gui_and_mcp_do_not_require_git_or_network(
    local_registry, tmp_path, monkeypatch, capsys
):
    def unavailable(*args, **kwargs):
        raise AssertionError("Local workspace operation attempted a Git/network call")

    monkeypatch.setattr("dagoberd.workspace_git.git", unavailable)
    monkeypatch.setattr("socket.create_connection", unavailable)
    monkeypatch.setattr("dagoberd.workspace_status.shutil.which", lambda _name: None)
    directory = tmp_path / "ordinary-folder"
    assert main(["workspaces", "create", "local", str(directory)]) == 0
    capsys.readouterr()
    loaded = populate(local_registry, directory)
    assert not (directory / ".git").exists()
    assert main(["workspaces", "doctor", "local"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["git"] == {"configured": False, "available": False}
    assert status["synchronization"]["backend"] is None
    service = MCPService(
        workspace_id=str(loaded.config.workspace.id), registry_path=local_registry.path
    )
    assert service.execute("workspace_status", {})["valid"]
    assert service.execute("workspace_sync_status", {})["backend"] is None
    with TestClient(create_workspace_app(local_registry, "test-token")) as client:
        headers = {"X-Dagoberd-Token": "test-token"}
        result = client.post(
            "/api/workspace-actions", headers=headers, json={"action": "status", "name": "local"}
        )
        assert result.status_code == 200
        assert result.json()["data"]["storage"] == "local"


@pytest.mark.skipif(shutil.which("git") is None, reason="Optional Git adoption requires Git")
def test_later_git_adoption_preserves_identity_previews_and_allows_local_commits(
    local_registry, tmp_path
):
    root = tmp_path / "local"
    loaded = populate(local_registry, root)
    before = loaded.config.database.path.read_bytes()
    configuration = loaded.project_path.read_bytes()
    with pytest.raises(ValueError, match="confirmation"):
        adopt_git(local_registry, "local", yes=False)
    assert not (root / ".git").exists()
    first = adopt_git(local_registry, "local", yes=True)
    second = adopt_git(local_registry, "local", yes=True)
    assert first == second
    assert first["origin"] == "" and first["head"] == ""
    assert loaded.project_path.read_bytes() == configuration
    assert loaded.config.database.path.read_bytes() == before
    git(root, "config", "user.email", "synthetic@example.invalid")
    git(root, "config", "user.name", "Synthetic test")
    service = WorkspaceGit(local_registry, "local")
    preview = service.preview("commit", ["dagoberd.toml", ".gitignore"], "Local version")
    assert service.run(preview["preview_id"], preview["fingerprint"], yes=True)["head"]
    assert loaded.config.database.path.read_bytes() == before
    with pytest.raises(ValueError, match="requires an origin"):
        service.preview("push")
    remote = tmp_path / "private-remote.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    service.configure_remote(remote.as_uri(), yes=True)
    with pytest.raises(ValueError, match="private origin"):
        service.preview("push")
    service.trust(yes=True)
    preview = service.preview("push")
    service.run(preview["preview_id"], preview["fingerprint"], yes=True)
    assert service.configure_remote(None, yes=True)["origin"] == ""
    assert local_registry.entry("local").private_remote is None
    assert workspace_status(local_registry, "local")["valid"]


def test_snapshot_roundtrip_without_git_preserves_identity_and_detects_local_conflicts(
    local_registry, tmp_path
):
    loaded = populate(local_registry, tmp_path / "local")
    sync = WorkspaceSync(local_registry, "local")
    root_a = tmp_path / "cloud-a"
    sync.configure(str(root_a), yes=True)
    preview = sync.preview("publish")
    with pytest.raises(ValueError, match="confirmation"):
        sync.run(preview["preview_id"], preview["fingerprint"], yes=False)
    doc = loaded.config.documents.storage_dir / "synthetic.txt"
    doc.write_text("Updated synthetic receipt")
    with pytest.raises(ValueError, match="changed"):
        sync.run(preview["preview_id"], preview["fingerprint"], yes=True)
    published = exchange(sync, "publish")
    root_b = tmp_path / "cloud-b"
    shutil.copytree(root_a, root_b)
    record = next(root_b.rglob("*.json"))
    inspected = inspect_snapshot(record)
    registry_b = WorkspaceRegistry(tmp_path / "machine-b-settings" / "workspaces.toml")
    with pytest.raises(ValueError, match="confirmation"):
        adopt_snapshot(
            registry_b,
            "copy",
            str(tmp_path / "copy"),
            str(record),
            inspected["fingerprint"],
            yes=False,
        )
    adopt_snapshot(
        registry_b, "copy", str(tmp_path / "copy"), str(record), inspected["fingerprint"], yes=True
    )
    copy = registry_b.load("copy")
    assert copy.config.workspace.id == loaded.config.workspace.id
    assert (copy.config.documents.storage_dir / "synthetic.txt").read_text() == doc.read_text()
    assert not (copy.project_path.parent / ".git").exists()
    with workspace_operation(copy, registry_b):
        repo = LedgerRepository(LedgerDatabase(copy.config.database.path))
        assert len(repo.list_transactions()) == 1
        OrganizationService(repo).create_manual_account(
            "Synthetic cash", account_kind="cash", idempotency_key="new-cash"
        )
    newer = exchange(WorkspaceSync(registry_b, "copy"), "publish")
    assert newer["remote_revision"] != published["remote_revision"]
    shutil.copytree(root_b, root_a, dirs_exist_ok=True)
    restored = exchange(sync, "restore")
    assert restored["local_revision"] == newer["remote_revision"]
    with workspace_operation(loaded, local_registry):
        repo = LedgerRepository(LedgerDatabase(loaded.config.database.path))
        assert len(repo.list_accounts()) == 2
    doc.write_text("Local unshared changes")
    with pytest.raises(ValueError, match="Local changes conflict"):
        sync.preview("restore")
    before = loaded.config.database.path.read_bytes()
    sync.configure(None, yes=True)
    assert sync.status()["backend"] is None
    assert loaded.config.database.path.read_bytes() == before
    assert list(root_a.rglob("*.aex"))


def test_snapshot_failure_corruption_and_branches_do_not_block_local_operations(
    local_registry, tmp_path, monkeypatch
):
    loaded = populate(local_registry, tmp_path / "local")
    sync = WorkspaceSync(local_registry, "local")
    root = tmp_path / "cloud"
    sync.configure(str(root), yes=True)
    original = sync.portability.export

    def interrupted(path):
        raise OSError("Synthetic interrupted replication")

    monkeypatch.setattr(sync.portability, "export", interrupted)
    preview = sync.preview("publish")
    with pytest.raises(OSError, match="interrupted"):
        sync.run(preview["preview_id"], preview["fingerprint"], yes=True)
    assert sync.status()["remote_revision"] is None
    assert workspace_status(local_registry, "local")["valid"]
    monkeypatch.setattr(sync.portability, "export", original)
    exchange(sync, "publish")
    record_path = next(root.rglob("*.json"))
    bundle = record_path.with_suffix(".aex")
    good = bundle.read_bytes()
    bundle.write_bytes(b"Synthetic corrupted snapshot")
    before = loaded.config.database.path.read_bytes()
    with pytest.raises(ValueError, match="checksum"):
        sync.preview("restore")
    assert loaded.config.database.path.read_bytes() == before
    bundle.write_bytes(good)
    record = json.loads(record_path.read_text())
    sibling = str(uuid4())
    record["revision"] = sibling
    (record_path.parent / f"{sibling}.aex").write_bytes(good)
    (record_path.parent / f"{sibling}.json").write_text(json.dumps(record))
    assert sync.status()["available"] is False
    with pytest.raises(ValueError, match="Conflicting"):
        sync.preview("publish")
    assert workspace_action(local_registry, WorkspaceAction(action="status", name="local"))["valid"]
    assert loaded.config.database.path.read_bytes() == before


def test_sync_root_and_foreign_snapshot_safety(local_registry, tmp_path):
    loaded = populate(local_registry, tmp_path / "local")
    sync = WorkspaceSync(local_registry, "local")
    with pytest.raises(ValueError, match=r"outside|overlaps"):
        sync.configure(str(tmp_path), yes=True)
    root = tmp_path / "private-cloud"
    sync.configure(str(root), yes=True)
    with pytest.raises(ValueError, match="outside synchronized snapshot roots"):
        local_registry.initialize("unsafe", root / "unsafe-local-workspace")
    assert not (root / "unsafe-local-workspace" / "output" / "dagoberd.db").exists()
    exchange(sync, "publish")
    record = next(root.rglob("*.json"))
    inspected = inspect_snapshot(record)
    with pytest.raises(ValueError, match="already registered"):
        adopt_snapshot(
            local_registry,
            "duplicate",
            str(tmp_path / "duplicate"),
            str(record),
            inspected["fingerprint"],
            yes=True,
        )
    assert not (tmp_path / "duplicate").exists()
    with pytest.raises(ValueError, match="changed"):
        adopt_snapshot(
            WorkspaceRegistry(tmp_path / "other-registry.toml"),
            "copy",
            str(tmp_path / "copy"),
            str(record),
            "wrong-fingerprint",
            yes=True,
        )
    assert not (tmp_path / "copy").exists()
    shutil.move(root, root.with_name("temporarily-offline"))
    assert sync.status()["available"] is False
    assert workspace_status(local_registry, "local")["valid"]
    assert loaded.config.database.path.is_file()


def test_snapshot_api_confirmation_and_private_config_exclusion(local_registry, tmp_path):
    loaded = populate(local_registry, tmp_path / "local")
    (loaded.project_path.parent / ".env").write_text("SYNTHETIC_SECRET=not-for-export")
    root = tmp_path / "private-cloud"
    with TestClient(create_workspace_app(local_registry, "test-token")) as client:
        headers = {"X-Dagoberd-Token": "test-token"}
        action = {"action": "sync-configure", "name": "local", "cloud_root": str(root)}
        assert client.post("/api/workspace-actions", json=action).status_code == 401
        assert (
            client.post("/api/workspace-actions", json=action, headers=headers).status_code == 400
        )
        action["confirmed"] = True
        assert (
            client.post("/api/workspace-actions", json=action, headers=headers).status_code == 200
        )
        result = client.post(
            "/api/workspace-actions",
            headers=headers,
            json={"action": "sync-publish", "name": "local"},
        )
        preview = result.json()["data"]
        result = client.post(
            "/api/workspace-actions",
            headers=headers,
            json={
                "action": "sync-run",
                "name": "local",
                "preview_id": preview["preview_id"],
                "fingerprint": preview["fingerprint"],
                "confirmed": True,
            },
        )
        assert result.status_code == 200, result.text
    import zipfile

    bundle = next(root.rglob("*.aex"))
    with zipfile.ZipFile(bundle) as archive:
        assert json.loads(archive.read("metadata/config.json")) == {}
        assert not any(name.endswith((".env", ".toml")) for name in archive.namelist())
        assert all(b"not-for-export" not in archive.read(name) for name in archive.namelist())
    (loaded.config.documents.storage_dir / ".env").write_text("SYNTHETIC_SECRET=must-stay-local")
    sync = WorkspaceSync(local_registry, "local")
    preview = sync.preview("publish")
    before = set(root.rglob("*.aex"))
    with pytest.raises(ValueError, match="credential"):
        sync.run(preview["preview_id"], preview["fingerprint"], yes=True)
    assert set(root.rglob("*.aex")) == before


def test_snapshot_record_change_and_missing_ancestry_are_rejected(local_registry, tmp_path):
    loaded = populate(local_registry, tmp_path / "local")
    sync = WorkspaceSync(local_registry, "local")
    root = tmp_path / "cloud"
    sync.configure(str(root), yes=True)
    exchange(sync, "publish")
    record_path = next(root.rglob("*.json"))
    inspected = inspect_snapshot(record_path)
    other = WorkspaceRegistry(tmp_path / "other-settings" / "registry.toml")
    with pytest.raises(ValueError, match="outside"):
        adopt_snapshot(
            other,
            "copy",
            str(root / "active"),
            str(record_path),
            inspected["fingerprint"],
            yes=True,
        )
    preview = sync.preview("publish")
    record = json.loads(record_path.read_text())
    record["parent"] = str(uuid4())
    record_path.write_text(json.dumps(record))
    before = loaded.config.database.path.read_bytes()
    with pytest.raises(ValueError, match="ancestry is incomplete"):
        sync.run(preview["preview_id"], preview["fingerprint"], yes=True)
    assert loaded.config.database.path.read_bytes() == before
    assert not (root / "active").exists()


@pytest.mark.parametrize("failure", ["restore", "state", "registration"])
def test_failed_adoption_does_not_reserve_workspace_identity(
    local_registry, tmp_path, monkeypatch, failure
):
    from dagoberd.services.portability import PortabilityService
    from dagoberd.workspace_sync import atomic_text

    populate(local_registry, tmp_path / "local")
    sync = WorkspaceSync(local_registry, "local")
    sync.configure(str(tmp_path / "cloud"), yes=True)
    exchange(sync, "publish")
    record = next((tmp_path / "cloud").rglob("*.json"))
    inspected = inspect_snapshot(record)
    registry = WorkspaceRegistry(tmp_path / "other-settings" / "workspaces.toml")
    original_restore = PortabilityService.restore

    def restore(service, *args, **kwargs):
        assert registry.list() == []
        if failure == "restore":
            raise OSError("Synthetic adoption failure")
        return original_restore(service, *args, **kwargs)

    def write(path, content):
        if path.name == "sync-state.json" and failure == "state":
            raise OSError("Synthetic adoption failure")
        atomic_text(path, content)

    def register(*args, **kwargs):
        assert registry.list() == []
        raise OSError("Synthetic adoption failure")

    target = tmp_path / "failed-adoption"
    with monkeypatch.context() as patch:
        patch.setattr(PortabilityService, "restore", restore)
        patch.setattr("dagoberd.workspace_sync.atomic_text", write)
        if failure == "registration":
            patch.setattr(registry, "add", register)
        with pytest.raises(OSError, match="Synthetic adoption failure"):
            adopt_snapshot(
                registry, "copy", str(target), str(record), inspected["fingerprint"], yes=True
            )
    assert registry.list() == []
    assert (target / "dagoberd.toml").is_file()
    preserved = (target / "dagoberd.toml").read_bytes()
    with pytest.raises(ValueError, match="new or empty"):
        adopt_snapshot(
            registry, "copy", str(target), str(record), inspected["fingerprint"], yes=True
        )
    assert (target / "dagoberd.toml").read_bytes() == preserved
    result = adopt_snapshot(
        registry,
        "copy",
        str(tmp_path / "successful-adoption"),
        str(record),
        inspected["fingerprint"],
        yes=True,
    )
    assert result["workspace_id"] == inspected["workspace_id"]
    assert registry.entry("copy").sync_root == tmp_path / "cloud"
    assert WorkspaceSync(registry, "copy").status()["local_revision"] == inspected["revision"]


@pytest.mark.parametrize(
    ("action", "failure"),
    [
        ("publish", "state"),
        ("restore", "state"),
        ("publish", "checkpoint"),
        ("restore", "checkpoint"),
        ("publish", "publication"),
    ],
)
@pytest.mark.parametrize("later_changes", [False, True])
def test_completed_exchange_survives_status_write_failure(
    local_registry, tmp_path, monkeypatch, action, later_changes, failure
):
    from dagoberd.safe_files import prepared_new_file
    from dagoberd.services.restore_files import RestoreError
    from dagoberd.workspace_sync import atomic_text

    loaded = populate(local_registry, tmp_path / "local")
    sync = WorkspaceSync(local_registry, "local")
    root = tmp_path / "cloud"
    sync.configure(str(root), yes=True)
    if action == "restore":
        exchange(sync, "publish")
        record = next(root.rglob("*.json"))
        inspected = inspect_snapshot(record)
        registry_b = WorkspaceRegistry(tmp_path / "other-settings" / "workspaces.toml")
        adopt_snapshot(
            registry_b,
            "copy",
            str(tmp_path / "copy"),
            str(record),
            inspected["fingerprint"],
            yes=True,
        )
        copy = registry_b.load("copy")
        (copy.config.documents.storage_dir / "synthetic.txt").write_text("Remote receipt")
        exchange(WorkspaceSync(registry_b, "copy"), "publish")

    before = sync._local_digest()
    checkpoint = sync.state_path.with_name("sync-completed.json")

    def write(path, content):
        if path == (checkpoint if failure == "checkpoint" else sync.state_path):
            raise OSError("Synthetic status write failure")
        atomic_text(path, content)

    def publish(path):
        if path.suffix == ".json":
            raise OSError("Synthetic revision publication failure")
        return prepared_new_file(path)

    preview = sync.preview(action)
    with monkeypatch.context() as patch:
        if failure == "publication":
            patch.setattr("dagoberd.workspace_sync.prepared_new_file", publish)
        else:
            patch.setattr("dagoberd.workspace_sync.atomic_text", write)
        expected = RestoreError if failure == "checkpoint" and action == "restore" else OSError
        with pytest.raises(expected):
            sync.run(preview["preview_id"], preview["fingerprint"], yes=True)
    if failure != "state":
        assert sync._local_digest() == before
        if action == "restore":
            assert sync._state()["revision"] != sync._head()["revision"]
            assert (
                loaded.config.documents.storage_dir / "synthetic.txt"
            ).read_text() == "Synthetic receipt"
        else:
            assert sync._state() == {}
        assert workspace_status(local_registry, "local")["valid"]
        # A fresh preview must remain executable after a preparation failure.
        exchange(sync, action)
    assert not (sync.state_path.parent / "sync-previews" / f"{preview['preview_id']}.json").exists()
    revisions = sorted(root.rglob("*.json"))
    assert len(revisions) == (1 if action == "publish" else 2)
    if action == "restore":
        assert (
            loaded.config.documents.storage_dir / "synthetic.txt"
        ).read_text() == "Remote receipt"
    baseline = sync._local_digest()
    if later_changes:
        (loaded.config.documents.storage_dir / "synthetic.txt").write_text("Later local receipt")
    restarted = WorkspaceSync(local_registry, "local")
    status = restarted.status()
    assert status["available"]
    assert status["local_revision"] == status["remote_revision"]
    assert restarted._state()["local_digest"] == baseline
    assert sorted(root.rglob("*.json")) == revisions
    if later_changes:
        with pytest.raises(ValueError, match="Local changes conflict"):
            restarted.preview("restore")
    else:
        assert restarted.preview("restore")["base"] == status["remote_revision"]
    assert restarted.preview("publish")["base"] == status["remote_revision"]
    assert workspace_status(local_registry, "local")["valid"]
    exchange(restarted, "publish")
    assert not restarted.state_path.with_name("sync-completed.json").exists()
    assert restarted._state()["local_digest"] == restarted._local_digest()
    assert len(list(root.rglob("*.json"))) == len(revisions) + 1


def test_unpublished_checkpoint_keeps_previous_completion(local_registry, tmp_path, monkeypatch):
    from dagoberd.safe_files import prepared_new_file
    from dagoberd.workspace_sync import atomic_text

    populate(local_registry, tmp_path / "local")
    sync = WorkspaceSync(local_registry, "local")
    sync.configure(str(tmp_path / "cloud"), yes=True)

    def write(path, content):
        if path == sync.state_path:
            raise OSError("Synthetic status failure")
        atomic_text(path, content)

    with monkeypatch.context() as patch:
        patch.setattr("dagoberd.workspace_sync.atomic_text", write)
        with pytest.raises(OSError, match="status failure"):
            exchange(sync, "publish")
    completed = sync._state()
    assert completed["revision"] == sync.status()["remote_revision"]

    def publish(path):
        if path.suffix == ".json":
            raise OSError("Synthetic publication failure")
        return prepared_new_file(path)

    with monkeypatch.context() as patch:
        patch.setattr("dagoberd.workspace_sync.prepared_new_file", publish)
        with pytest.raises(OSError, match="publication failure"):
            exchange(sync, "publish")
    restarted = WorkspaceSync(local_registry, "local")
    assert restarted._state() == completed
    assert restarted.preview("restore")["base"] == completed["revision"]
    exchange(restarted, "publish")
    assert restarted.status()["local_revision"] != completed["revision"]
