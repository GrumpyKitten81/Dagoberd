from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal

import pytest

from dagoberd.cli import build_parser, main
from dagoberd.ledger.database import LedgerDatabase
from dagoberd.ledger.models import NormalizedTransaction
from dagoberd.ledger.repository import LedgerRepository
from dagoberd.profiles import install_builtin_profiles


def _workspace(tmp_path):
    config = tmp_path / "dagoberd.toml"
    config.write_text("""[database]
path = "ledger.db"
[llm]
enabled = true
model = "local-test-model"
""")
    repo = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    install_builtin_profiles(repo)
    repo.import_transactions(
        "file",
        "canonical-cli",
        [
            NormalizedTransaction("rule", date(2026, 7, 1), Decimal("266"), "EUR", "RULE", "one"),
            NormalizedTransaction("hint", date(2026, 7, 2), Decimal("100"), "EUR", "HINT", "one"),
        ],
    )
    return config, repo


def test_canonical_creation_and_deterministic_classification(tmp_path, monkeypatch):
    config, repo = _workspace(tmp_path)
    args = ["--config", str(config)]

    def reject_provider(*_args, **_kwargs):
        pytest.fail("Deterministic commands must not create an LLM provider")

    monkeypatch.setattr("dagoberd.cli.create_llm_provider", reject_provider)
    assert main([*args, "profiles", "create", "custom", "Custom", "--description", "Test"]) == 0
    assert (
        main([*args, "rules", "add", "rental", "Rent", "RULE", "income.rent", "--priority", "10"])
        == 0
    )
    assert (
        main(
            [
                *args,
                "hints",
                "add",
                "rental",
                "Utilities",
                "--pattern",
                "HINT",
                "--category",
                "income.utilities",
            ]
        )
        == 0
    )
    rows = {row["purpose"]: row for row in repo.list_transactions(limit=None)}
    assert main([*args, "classifications", "apply-rules", rows["RULE"]["id"], "rental"]) == 0
    assert main([*args, "classifications", "deterministic", rows["HINT"]["id"], "rental"]) == 0
    classified = {
        row["purpose"]: row for row in repo.list_transactions(limit=None, profile_code="rental")
    }
    assert classified["RULE"]["category_code"] == "income.rent"
    assert classified["HINT"]["category_code"] == "income.utilities"
    with repo.database.connect() as connection:
        assert (
            connection.execute(
                "SELECT description FROM ledger_profiles WHERE code='custom'"
            ).fetchone()[0]
            == "Test"
        )


def test_manual_classification_and_metadata_document_registration(tmp_path):
    config, repo = _workspace(tmp_path)
    args = ["--config", str(config)]
    row = repo.list_transactions(limit=1)[0]
    command = [
        *args,
        "classifications",
        "set",
        row["id"],
        "rental",
        "income.rent",
        "--version",
        str(row["version"]),
    ]
    assert main([*command, "--suggest"]) == 0
    assert repo.list_classifications(status="suggested")[0]["source"] == "manual"
    assert main(command) == 5
    command[-1] = str(repo.get_transaction(row["id"])["version"])
    assert main(command) == 0
    assert main(command) == 5
    assert (
        main(
            [
                *args,
                "documents",
                "register",
                "synthetic-invoice.pdf",
                "--amount",
                "100",
                "--merchant",
                "Example",
            ]
        )
        == 0
    )
    with repo.database.connect() as connection:
        document = connection.execute("SELECT * FROM ledger_documents").fetchone()
    assert document["filename"] == "synthetic-invoice.pdf"
    assert document["amount_minor"] == 10000
    assert document["storage_path"] is None
    assert main([*args, "documents", "candidates", document["id"]]) == 0


@pytest.mark.parametrize(
    "command",
    [
        "import-file",
        "classify",
        "apply-rules",
        "classify-auto",
        "profile-create",
        "rule-add",
        "hint-add",
        "document",
        "match-document",
        "legacy-consolidation",
    ],
)
def test_removed_commands_are_rejected(command):
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args([command])
    assert error.value.code == 2


def test_top_level_help_describes_every_command():
    parser = build_parser()
    subparsers = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    assert {choice.dest for choice in subparsers._choices_actions if choice.help} == set(
        subparsers.choices
    )
    assert "profile-create" not in parser.format_help()


@pytest.mark.parametrize("missing_mapping", [False, True])
def test_invalid_wiso_run_never_initializes_a_ledger(tmp_path, missing_mapping):
    config = tmp_path / "dagoberd.toml"
    config.write_text('[database]\npath = "ledger.db"\n')
    tree = tmp_path / "categories.Tre"
    tree.write_text("Baumdaten\n\t0\n\tCategories\nSection\n\tValue\n", encoding="cp1252")
    command = [
        "--config",
        str(config),
        "wiso-trees",
        "run",
        "--categories",
        str(tree),
        "--tax",
        str(tree),
        "--fingerprint",
        "invalid",
    ]
    if missing_mapping:
        command.extend(["--free-1", str(tree)])
    assert main(command) != 0
    assert not (tmp_path / "ledger.db").exists()
