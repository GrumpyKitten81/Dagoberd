from __future__ import annotations

from pathlib import Path

import pytest

from dagoberd.ledger.database import LedgerDatabase
from dagoberd.ledger.organization import OrganizationService
from dagoberd.ledger.repository import LedgerRepository
from dagoberd.services.wiso_tree_taxonomy import (
    import_wiso_tree_taxonomy,
    preview_wiso_tree_taxonomy,
    read_wiso_tree,
)


def _tree(path: Path, title: str, body: str) -> Path:
    path.write_text(f"Baumdaten\r\n\t0\r\n\t{title}\r\n{body}", encoding="cp1252")
    return path


def _inputs(tmp_path: Path) -> tuple[Path, Path, dict[str, Path]]:
    return (
        _tree(tmp_path / "Category.Tre", "Category", "Expenses\r\n\tHousing\r\n\t\tPower\r\n"),
        _tree(tmp_path / "Tax.Tre", "Tax", "Income\r\n\tRental\r\n"),
        {
            "Field 1": _tree(tmp_path / "Field 1.Tre", "Field 1", "Group\r\n\tShared\r\n"),
            "Field 2": _tree(
                tmp_path / "Field 2.Tre", "Field 2", "Project\r\n\tGarden\r\nCycle\r\n\tMonthly\r\n"
            ),
            "Field 3": _tree(
                tmp_path / "Field 3.Tre",
                "Field 3",
                "Context\r\n\tWorkshop\r\nDetail\r\n\tMaterial\r\n",
            ),
        },
    )


def _mapping(tmp_path: Path) -> Path:
    path = tmp_path / "mapping.toml"
    path.write_text(
        """version = 1
[free_fields."Field 1".Group]
code = "group"
name = "Group"
[free_fields."Field 2".Project]
code = "project"
name = "Project"
[free_fields."Field 2".Cycle]
code = "cycle"
name = "Cycle"
[free_fields."Field 3".Context]
code = "context"
name = "Context"
[free_fields."Field 3".Detail]
code = "detail"
name = "Detail"
[value_codes.group]
Shared = "group-shared"
""",
        encoding="utf-8",
    )
    return path


def _taxonomy_snapshot(repository: LedgerRepository) -> dict[str, list[tuple[object, ...]]]:
    with repository.database.connect() as connection:
        return {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in (
                "ledger_profiles",
                "ledger_categories",
                "ledger_dimensions",
                "ledger_audit_events",
            )
        }


def test_read_wiso_tree_preserves_nested_paths(tmp_path):
    category_tree, _, _ = _inputs(tmp_path)

    assert [(node.name, node.path) for node in read_wiso_tree(category_tree)] == [
        ("Expenses", ("Expenses",)),
        ("Housing", ("Expenses", "Housing")),
        ("Power", ("Expenses", "Housing", "Power")),
    ]


def test_import_categories_and_tax_without_mapping(tmp_path):
    category_tree, tax_tree, _ = _inputs(tmp_path)
    plan = preview_wiso_tree_taxonomy(category_tree, tax_tree, {})
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))

    dimensions_before = OrganizationService(repository).list_dimensions()
    result = import_wiso_tree_taxonomy(repository, category_tree, tax_tree, {}, plan.fingerprint)

    assert result["created_or_verified"] == {
        "categories": 3,
        "tax_categories": 2,
        "dimension_values": 0,
    }
    assert OrganizationService(repository).list_dimensions() == dimensions_before


def test_import_wiso_tree_taxonomy_builds_hierarchy_and_separate_dimensions(tmp_path):
    category_tree, tax_tree, free_trees = _inputs(tmp_path)
    mapping_file = _mapping(tmp_path)
    plan = preview_wiso_tree_taxonomy(
        category_tree, tax_tree, free_trees, mapping_file=mapping_file
    )
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))

    for _ in range(2):
        result = import_wiso_tree_taxonomy(
            repository,
            category_tree,
            tax_tree,
            free_trees,
            plan.fingerprint,
            mapping_file=mapping_file,
        )
        assert result["created_or_verified"] == {
            "categories": 3,
            "tax_categories": 2,
            "dimension_values": 5,
        }
    categories = OrganizationService(repository).list_profile_categories()["wiso"]
    assert next(item for item in categories if item["name"] == "Power")["path"] == [
        "Expenses",
        "Housing",
        "Power",
    ]
    dimensions = {item["code"]: item for item in OrganizationService(repository).list_dimensions()}
    assert {
        code: {value["code"] for value in dimension["values"]}
        for code, dimension in dimensions.items()
        if code in plan.dimensions
    } == {
        "group": {"group-shared"},
        "project": {"garden"},
        "cycle": {"monthly"},
        "context": {"workshop"},
        "detail": {"material"},
    }


def test_import_wiso_tree_taxonomy_preserves_existing_dimension_labels(tmp_path):
    category_tree, tax_tree, free_trees = _inputs(tmp_path)
    mapping_file = _mapping(tmp_path)
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    organization = OrganizationService(repository)
    organization.create_dimension("group", "Existing group")
    organization.create_dimension_value("group", "group-shared", "Existing shared label")
    plan = preview_wiso_tree_taxonomy(
        category_tree, tax_tree, free_trees, mapping_file=mapping_file
    )

    import_wiso_tree_taxonomy(
        repository, category_tree, tax_tree, free_trees, plan.fingerprint, mapping_file=mapping_file
    )

    group = next(item for item in organization.list_dimensions() if item["code"] == "group")
    assert group["name"] == "Existing group"
    assert group["values"][0]["name"] == "Existing shared label"


@pytest.mark.parametrize("changed_source", ["category", "mapping", "association"])
def test_import_wiso_tree_taxonomy_rejects_changed_inputs(tmp_path, changed_source):
    category_tree, tax_tree, free_trees = _inputs(tmp_path)
    mapping_file = _mapping(tmp_path)
    plan = preview_wiso_tree_taxonomy(
        category_tree, tax_tree, free_trees, mapping_file=mapping_file
    )
    if changed_source == "category":
        _tree(category_tree, "Category", "Changed\r\n")
    elif changed_source == "mapping":
        mapping_file.write_text(mapping_file.read_text().replace('"group-shared"', '"shared-v2"'))
    else:
        category_tree, tax_tree = tax_tree, category_tree
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))

    before = _taxonomy_snapshot(repository)
    with pytest.raises(ValueError, match="fingerprint"):
        import_wiso_tree_taxonomy(
            repository,
            category_tree,
            tax_tree,
            free_trees,
            plan.fingerprint,
            mapping_file=mapping_file,
        )
    assert _taxonomy_snapshot(repository) == before


def test_mapping_order_and_formatting_do_not_change_fingerprint(tmp_path):
    category_tree, tax_tree, free_trees = _inputs(tmp_path)
    mapping_file = _mapping(tmp_path)
    plan = preview_wiso_tree_taxonomy(
        category_tree, tax_tree, free_trees, mapping_file=mapping_file
    )
    mapping_file.write_text("# Mapping formatting\n\n" + mapping_file.read_text())
    reordered = dict(reversed(tuple(free_trees.items())))

    assert (
        preview_wiso_tree_taxonomy(
            category_tree, tax_tree, reordered, mapping_file=mapping_file
        ).fingerprint
        == plan.fingerprint
    )


@pytest.mark.parametrize(
    "invalid_mapping", ["missing", "section", "extra", "code", "collision", "version"]
)
def test_invalid_mappings_fail_before_writes(tmp_path, invalid_mapping):
    category_tree, tax_tree, free_trees = _inputs(tmp_path)
    mapping_file = _mapping(tmp_path)
    content = mapping_file.read_text()
    if invalid_mapping == "missing":
        mapping_file = None
    elif invalid_mapping == "section":
        mapping_file.write_text(
            content.replace('[free_fields."Field 2".Cycle]', '[free_fields."Field 2".Other]')
        )
    elif invalid_mapping == "extra":
        mapping_file.write_text(content.replace("version = 1", "version = 1\nunknown = true"))
    elif invalid_mapping == "code":
        mapping_file.write_text(content.replace('code = "cycle"', 'code = "bad/code"'))
    elif invalid_mapping == "collision":
        mapping_file.write_text(content + 'Other = "group-shared"\n')
    else:
        mapping_file.write_text(content.replace("version = 1", "version = 2"))
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))

    before = _taxonomy_snapshot(repository)
    with pytest.raises(ValueError):
        import_wiso_tree_taxonomy(
            repository, category_tree, tax_tree, free_trees, "unused", mapping_file=mapping_file
        )
    assert _taxonomy_snapshot(repository) == before


@pytest.mark.parametrize(
    "body", ["Group\r\n\tShared\r\n\tSHARED\r\n", "Group\r\n\tValue\r\n\t\tNested\r\n"]
)
def test_invalid_dimension_values_fail_before_writes(tmp_path, body):
    category_tree, tax_tree, free_trees = _inputs(tmp_path)
    mapping_file = _mapping(tmp_path)
    mapping_file.write_text(mapping_file.read_text().replace('Shared = "group-shared"', ""))
    _tree(free_trees["Field 1"], "Field 1", body)
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))

    before = _taxonomy_snapshot(repository)
    with pytest.raises(ValueError):
        import_wiso_tree_taxonomy(
            repository, category_tree, tax_tree, free_trees, "unused", mapping_file=mapping_file
        )
    assert _taxonomy_snapshot(repository) == before
