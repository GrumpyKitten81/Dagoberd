"""Built-in, local finance profiles. Users may add rules without changing code."""

from __future__ import annotations

from typing import TypedDict

from dagoberd.ledger.repository import LedgerRepository

RuleDefinition = tuple[str, str, str, int]


class ProfileDefinition(TypedDict):
    name: str
    description: str
    categories: list[tuple[str, str]]
    rules: list[RuleDefinition]


PROFILE_DEFINITIONS: dict[str, ProfileDefinition] = {
    "rental": {
        "name": "Rental property",
        "description": "Rental income, utilities and property expenses.",
        "categories": [
            ("income.rent", "Rental income"),
            ("income.utilities", "Utility payments"),
            ("expense.utilities", "Utility bills"),
            ("expense.property", "Property expenses"),
            ("income.deposit", "Deposits received"),
            ("expense.deposit_refund", "Deposit refunds"),
            ("excluded.non_rental", "Unrelated to rental"),
            ("review.unresolved", "Needs review"),
        ],
        "rules": [
            ("Rent received", r"(?i)\b(miete|alquiler|mensualidad)\b", "income.rent", 10),
            ("Utilities", r"(?i)\b(luz|gas|nebenkosten)\b", "expense.utilities", 20),
        ],
    },
    "spain-tax": {
        "name": "Spanish tax",
        "description": "Local categories for Spanish tax preparation; not tax advice.",
        "categories": [
            ("income.rental", "Rental earnings"),
            ("expense.tax", "Taxes and charges"),
            ("expense.deductible", "Deductible expenses"),
        ],
        "rules": [("Tax", r"(?i)\b(impuesto|ibi|tributo)\b", "expense.tax", 10)],
    },
    "freelance": {
        "name": "Freelance",
        "description": "Self-employment income, business expenses and tax reserve.",
        "categories": [
            ("income.freelance", "Freelance income"),
            ("expense.business", "Business expenses"),
            ("expense.tax-reserve", "Tax reserve"),
        ],
        "rules": [("Software", r"(?i)\b(openai|github|adobe|software)\b", "expense.business", 20)],
    },
    "household": {
        "name": "Household ledger",
        "description": "Household cashbook categories for income and expenses.",
        "categories": [
            ("expense.children", "Children"),
            ("expense.household", "Household and living costs"),
            ("expense.food", "Food and drinks"),
            ("expense.leisure", "Leisure"),
            ("expense.repairs", "Renovation and repairs"),
            ("expense.other", "Other expenses"),
            ("expense.gifts", "Gifts and donations"),
            ("expense.property-tax", "Other taxes"),
            ("income.rent-cash", "Rent and lease"),
            ("income.other", "Other income"),
            ("income.gifts", "Gifts and winnings"),
            ("income.earnings", "Earnings"),
            ("review.uncategorized", "Needs review"),
        ],
        "rules": [],
    },
}


def install_builtin_profiles(repository: LedgerRepository) -> None:
    for code, definition in PROFILE_DEFINITIONS.items():
        profile_id = repository.ensure_profile(code, definition["name"], definition["description"])
        category_ids = []
        for category_code, category_name in definition["categories"]:
            category_id = repository.ensure_category(category_code, category_name)
            category_ids.append(category_id)
        with repository.database.connect() as connection:
            linked_categories = {
                row["category_id"]
                for row in connection.execute(
                    "SELECT category_id FROM ledger_profile_categories WHERE profile_id=?",
                    (profile_id,),
                )
            }
            missing_categories = [
                category_id for category_id in category_ids if category_id not in linked_categories
            ]
            if missing_categories:
                connection.executemany(
                    "INSERT INTO ledger_profile_categories(profile_id, category_id) VALUES (?, ?)",
                    ((profile_id, category_id) for category_id in missing_categories),
                )
            existing = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM ledger_rules WHERE profile_id=?", (profile_id,)
                )
            }
        for name, pattern, category_code, priority in definition["rules"]:
            if (
                name not in existing
                and {
                    "Rent received": "Mieteingang",
                    "Utilities": "Nebenkosten",
                    "Tax": "Steuer",
                }.get(name)
                not in existing
            ):
                repository.add_rule(code, name, pattern, category_code, priority)
