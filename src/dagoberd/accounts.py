"""Account selection shared by CLI, MCP, matching and reports."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class AccountScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["all", "selected"] = "all"
    account_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_scope(self) -> AccountScope:
        if self.mode == "all" and self.account_ids:
            raise ValueError("All-account scope cannot contain account IDs")
        if self.mode == "selected" and not self.account_ids:
            raise ValueError("Selected-account scope requires account IDs")
        if len(set(self.account_ids)) != len(self.account_ids):
            raise ValueError("Account IDs in a scope must be unique")
        return self

    @classmethod
    def all(cls) -> AccountScope:
        return cls(mode="all")

    @classmethod
    def selected(cls, account_ids: list[str] | tuple[str, ...]) -> AccountScope:
        return cls(mode="selected", account_ids=tuple(account_ids))
