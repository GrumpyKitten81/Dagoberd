"""Import WISO ``.Tre`` category trees into the local taxonomy."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from account_eval.ledger.organization import OrganizationService
from account_eval.ledger.repository import LedgerRepository
from account_eval.services.import_annotations import slugify_label
from account_eval.services.wiso_tree_mapping import WisoTreeMapping, read_wiso_tree_mapping


@dataclass(frozen=True, slots=True)
class WisoTreeNode:
    """A category node together with its unambiguous path in a WISO tree."""

    name: str
    path: tuple[str, ...]

    @property
    def parent_path(self) -> tuple[str, ...] | None:
        return self.path[:-1] or None


@dataclass(frozen=True, slots=True)
class WisoTreeImportPlan:
    fingerprint: str
    category_nodes: int
    tax_nodes: int
    dimensions: dict[str, tuple[str, ...]]

    def as_dict(self) -> dict[str, object]:
        return {
            "fingerprint": self.fingerprint,
            "categories": {"profile": "wiso", "nodes": self.category_nodes},
            "tax_categories": {"profile": "wiso-tax", "nodes": self.tax_nodes},
            "dimensions": {code: list(values) for code, values in self.dimensions.items()},
        }


@dataclass(frozen=True, slots=True)
class _PreparedImport:
    plan: WisoTreeImportPlan
    category_nodes: tuple[WisoTreeNode, ...]
    tax_nodes: tuple[WisoTreeNode, ...]
    dimensions: dict[str, tuple[str, dict[str, str]]]


def read_wiso_tree(path: Path) -> tuple[WisoTreeNode, ...]:
    return _parse_wiso_tree(path, path.read_bytes())


def _parse_wiso_tree(path: Path, content: bytes) -> tuple[WisoTreeNode, ...]:
    try:
        text = content.decode("cp1252")
    except UnicodeDecodeError:
        text = content.decode("latin-1")
    lines = [line.rstrip("\r") for line in text.splitlines() if line.strip()]
    if len(lines) < 4 or lines[0] != "Baumdaten":
        raise ValueError(f"{path.name} is not a supported WISO .Tre file")

    nodes: list[WisoTreeNode] = []
    parents: dict[int, tuple[str, ...]] = {}
    for line in lines[3:]:
        depth = len(line) - len(line.lstrip("\t"))
        name = line.lstrip("\t").strip()
        if not name:
            continue
        if depth and depth - 1 not in parents:
            raise ValueError(f"{path.name} contains an orphaned node: {name}")
        parent = parents.get(depth - 1, ())
        node = WisoTreeNode(name=name, path=(*parent, name))
        nodes.append(node)
        parents[depth] = node.path
        for child_depth in tuple(level for level in parents if level > depth):
            del parents[child_depth]
    return tuple(nodes)


def preview_wiso_tree_taxonomy(
    category_tree: Path,
    tax_tree: Path,
    free_trees: dict[str, Path],
    *,
    mapping_file: Path | None = None,
) -> WisoTreeImportPlan:
    return _prepare_import(category_tree, tax_tree, free_trees, mapping_file).plan


def _prepare_import(
    category_tree: Path,
    tax_tree: Path,
    free_trees: dict[str, Path],
    mapping_file: Path | None,
) -> _PreparedImport:
    mapping = read_wiso_tree_mapping(mapping_file)
    category_content = category_tree.read_bytes()
    tax_content = tax_tree.read_bytes()
    category_nodes = _parse_wiso_tree(category_tree, category_content)
    tax_nodes = _parse_wiso_tree(tax_tree, tax_content)
    sources = {
        "categories": hashlib.sha256(category_content).hexdigest(),
        "tax_categories": hashlib.sha256(tax_content).hexdigest(),
    }
    dimensions: dict[str, tuple[str, dict[str, str]]] = {}
    for field, path in sorted(free_trees.items()):
        content = path.read_bytes()
        sources[f"free_field:{field}"] = hashlib.sha256(content).hexdigest()
        _prepare_dimensions(field, _parse_wiso_tree(path, content), mapping, dimensions)
    fingerprint = hashlib.sha256(
        json.dumps(
            {"mapper_version": 1, "mapping": mapping.model_dump(), "sources": sources},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    plan = WisoTreeImportPlan(
        fingerprint=fingerprint,
        category_nodes=len(category_nodes),
        tax_nodes=len(tax_nodes),
        dimensions={code: tuple(values.values()) for code, (_, values) in dimensions.items()},
    )
    return _PreparedImport(plan, category_nodes, tax_nodes, dimensions)


def _prepare_dimensions(
    field: str,
    nodes: tuple[WisoTreeNode, ...],
    mapping: WisoTreeMapping,
    dimensions: dict[str, tuple[str, dict[str, str]]],
) -> None:
    if field not in mapping.free_fields:
        raise ValueError(f"Missing WISO free-field mapping for {field}")
    sections = mapping.free_fields[field]
    for node in nodes:
        section = node.path[0]
        if section not in sections:
            raise ValueError(f"Missing WISO section mapping for {field}: {section}")
        dimension = sections[section]
        _, values = dimensions.setdefault(dimension.code, (dimension.name, {}))
        if len(node.path) == 1:
            continue
        if len(node.path) != 2:
            raise ValueError(f"WISO dimension values must be direct section children: {node.path}")
        value_code = mapping.value_codes.get(dimension.code, {}).get(
            node.name, slugify_label(node.name)
        )
        if value_code in values and values[value_code] != node.name:
            raise ValueError(f"Value-code collision in dimension {dimension.code}: {value_code}")
        values[value_code] = node.name


def import_wiso_tree_taxonomy(
    repository: LedgerRepository,
    category_tree: Path,
    tax_tree: Path,
    free_trees: dict[str, Path],
    fingerprint: str,
    *,
    mapping_file: Path | None = None,
) -> dict[str, object]:
    prepared = _prepare_import(category_tree, tax_tree, free_trees, mapping_file)
    if fingerprint != prepared.plan.fingerprint:
        raise ValueError("WISO tree fingerprint does not match the current inputs and mapping")

    organization = OrganizationService(repository)
    existing = {item["code"]: item for item in organization.list_dimensions()}
    category_count = _install_categories(
        repository, "wiso", "WISO Kategorien", prepared.category_nodes
    )
    tax_count = _install_categories(repository, "wiso-tax", "WISO Steuer", prepared.tax_nodes)
    dimension_count = 0
    for dimension_code, (dimension_name, values) in prepared.dimensions.items():
        if dimension_code not in existing:
            organization.create_dimension(dimension_code, dimension_name)
        existing_codes = {
            value["code"] for value in existing.get(dimension_code, {}).get("values", [])
        }
        for value_code, label in values.items():
            if value_code not in existing_codes:
                organization.create_dimension_value(dimension_code, value_code, label)
            dimension_count += 1
    return {
        **prepared.plan.as_dict(),
        "created_or_verified": {
            "categories": category_count,
            "tax_categories": tax_count,
            "dimension_values": dimension_count,
        },
    }


def _install_categories(
    repository: LedgerRepository,
    profile_code: str,
    profile_name: str,
    nodes: tuple[WisoTreeNode, ...],
) -> int:
    profile_id = repository.ensure_profile(profile_code, profile_name)
    for node in nodes:
        code = _category_code(profile_code, node.path)
        parent_code = _category_code(profile_code, node.parent_path) if node.parent_path else None
        category_id = repository.ensure_category(code, node.name, parent_code)
        with repository.database.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO ledger_profile_categories(profile_id, category_id) VALUES (?, ?)",
                (profile_id, category_id),
            )
    return len(nodes)


def _category_code(profile_code: str, path: tuple[str, ...]) -> str:
    readable = ".".join(slugify_label(part) for part in path)
    digest = hashlib.sha256("\x1f".join(path).encode("utf-8")).hexdigest()[:10]
    return f"{profile_code}.tree.{readable}-{digest}"
