from dagoberd.privacy import (
    limit_text,
    mask_iban,
    mask_identifier,
    safe_financial_record,
)


def test_financial_identifiers_are_masked_centrally():
    assert mask_identifier("123456789", 4) == "…6789"
    assert mask_iban("DE12 3456 7890", 4) == "…7890"
    assert limit_text("abcdef", 3) == "abc…"

    safe = safe_financial_record(
        {
            "external_id": "account-123456",
            "reference": "invoice-999999",
            "purpose": "x" * 20,
        },
        visible=3,
        maximum_text=5,
    )

    assert safe == {
        "external_id": "…456",
        "reference": "…999",
        "purpose": "xxxxx…",
    }
