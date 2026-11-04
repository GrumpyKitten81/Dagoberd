from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dagoberd.ledger.organization import OrganizationService


class WisoTreeDimension(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: str
    name: str

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return OrganizationService._code(value, "Dimension code")

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return OrganizationService._clean_name(value, "Dimension name")


class WisoTreeMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1]
    free_fields: dict[str, dict[str, WisoTreeDimension]] = Field(default_factory=dict)
    value_codes: dict[str, dict[str, str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_mapping(self) -> Self:
        dimensions: dict[str, str] = {}
        for field, sections in self.free_fields.items():
            if not field.strip() or not sections:
                raise ValueError("Free fields require a source field and mapped sections")
            for section, dimension in sections.items():
                if not section.strip():
                    raise ValueError("Free field section must not be empty")
                if dimensions.setdefault(dimension.code, dimension.name) != dimension.name:
                    raise ValueError(f"Conflicting names for dimension {dimension.code}")
        normalized: dict[str, dict[str, str]] = {}
        for dimension_code, values in self.value_codes.items():
            code = OrganizationService._code(dimension_code, "Dimension code")
            if code not in dimensions or code in normalized:
                raise ValueError(f"Unknown or duplicate value-code dimension {dimension_code}")
            normalized[code] = {}
            for label, value in values.items():
                if not label.strip():
                    raise ValueError("Dimension value label must not be empty")
                value_code = OrganizationService._code(value, "Dimension value code")
                if value_code in normalized[code].values():
                    raise ValueError(f"Value-code collision in dimension {code}: {value_code}")
                normalized[code][label] = value_code
        self.value_codes = normalized
        return self


def read_wiso_tree_mapping(path: Path | None) -> WisoTreeMapping:
    if path is None:
        return WisoTreeMapping(version=1)
    return WisoTreeMapping.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
