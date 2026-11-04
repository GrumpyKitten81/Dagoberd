from __future__ import annotations

import json
import re
from pathlib import Path

from account_eval.gui import app
from account_eval.ledger.database import LedgerDatabase
from account_eval.ledger.repository import LedgerRepository
from account_eval.profiles import install_builtin_profiles


def test_language_catalogs_have_matching_keys_placeholders_and_plural_forms():
    directory = Path(app.__file__).with_name("static") / "locales"
    catalogs = {
        language: json.loads((directory / f"{language}.js").read_text(encoding="utf-8")[15:-2])
        for language in ("en", "de", "es")
    }
    for language, catalog in catalogs.items():
        assert catalog.keys() == catalogs["en"].keys(), language
        for key, value in catalog.items():
            reference = catalogs["en"][key]
            assert isinstance(value, type(reference)), (language, key)
            variants = value if isinstance(value, dict) else {"text": value}
            expected = reference if isinstance(reference, dict) else {"text": reference}
            assert variants.keys() == expected.keys(), (language, key)
            if isinstance(value, dict):
                assert {"one", "other"} <= value.keys()
            for variant, pattern in variants.items():
                assert pattern.strip(), (language, key)
                assert set(re.findall(r"\{(\w+)\}", pattern)) == set(
                    re.findall(r"\{(\w+)\}", expected[variant])
                ), (language, key, variant)
                assert "${" not in pattern

    static = directory.parent
    for path in static.glob("*.js"):
        keys = re.findall(r"msg\(['\"]([^'\"]+)['\"]", path.read_text(encoding="utf-8"))
        assert set(keys) <= catalogs["en"].keys(), path.name


def test_translated_builtin_defaults_preserve_existing_custom_names_and_legacy_rules(tmp_path):
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    repository.ensure_profile("rental", "My custom rental", "My description")
    repository.ensure_category("income.rent", "My custom category")
    install_builtin_profiles(repository)
    with repository.database.connect() as connection:
        connection.execute("UPDATE ledger_rules SET name='Mieteingang' WHERE name='Rent received'")

    install_builtin_profiles(repository)
    install_builtin_profiles(repository)

    with repository.database.connect() as connection:
        assert (
            connection.execute("SELECT name FROM ledger_profiles WHERE code='rental'").fetchone()[0]
            == "My custom rental"
        )
        assert (
            connection.execute(
                "SELECT name FROM ledger_categories WHERE code='income.rent'"
            ).fetchone()[0]
            == "My custom category"
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM ledger_rules WHERE name IN ('Mieteingang', 'Rent received')"
            ).fetchone()[0]
            == 1
        )
