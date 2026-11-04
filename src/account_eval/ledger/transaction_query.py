"""Shared SQL fragment for transaction search and count operations."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from account_eval.ledger.models import TransactionSearchFilter, transaction_status

SEARCH_COLUMNS = """SELECT t.*, a.display_name AS account_name,
       a.account_type, p.kind AS provider_kind,
       m.canonical_name AS merchant,
       c.code AS category_code, c.name AS category,
       cl.source AS classification_source, cp.code AS classification_profile,
       (SELECT COUNT(*) FROM ledger_transaction_sources lts
        WHERE lts.transaction_id=t.id) AS source_count,
       CASE WHEN EXISTS (
           SELECT 1 FROM ledger_document_matches dm
           WHERE dm.transaction_id=t.id AND dm.status='matched'
       ) THEN 'matched' ELSE 'unmatched' END AS document_status,
       CASE WHEN EXISTS (
           SELECT 1 FROM ledger_review_items review
           WHERE review.entity_type='transaction' AND review.entity_id=t.id
             AND review.status='open'
       ) THEN 1 ELSE 0 END AS has_open_review,
       COALESCE((
           SELECT json_group_array(name) FROM (
               SELECT tag.name AS name FROM ledger_transaction_tags tt
               JOIN ledger_tags tag ON tag.id=tt.tag_id
               WHERE tt.transaction_id=t.id ORDER BY tag.name COLLATE NOCASE
           )
       ), '[]') AS tags_json,
       COALESCE((
           SELECT json_group_object(code, value_code) FROM (
               SELECT d.code AS code, dv.code AS value_code
               FROM ledger_transaction_dimensions td
               JOIN ledger_dimensions d ON d.id=td.dimension_id
               JOIN ledger_dimension_values dv ON dv.id=td.dimension_value_id
               WHERE td.transaction_id=t.id ORDER BY d.code
           )
       ), '{}') AS dimensions_json"""


def build_transaction_query(
    filters: TransactionSearchFilter,
    account_ids: Iterable[str],
) -> tuple[str, list[Any]]:
    ids = tuple(account_ids)
    if not ids:
        return "FROM ledger_transactions t WHERE 1=0", []
    query = """FROM ledger_transactions t
               JOIN ledger_accounts a ON a.id=t.account_id
               JOIN ledger_providers p ON p.id=a.provider_id
               LEFT JOIN ledger_merchants m ON m.id=t.merchant_id
               LEFT JOIN ledger_classifications cl ON cl.id=(
                   SELECT latest.id FROM ledger_classifications latest
                   WHERE latest.transaction_id=t.id AND latest.status='applied'
                   ORDER BY latest.created_at DESC, latest.id DESC LIMIT 1
               )
               LEFT JOIN ledger_categories c ON c.id=cl.category_id
               LEFT JOIN ledger_profiles cp ON cp.id=cl.profile_id
               WHERE t.state<>'void'"""
    parameters: list[Any] = []
    placeholders = ",".join("?" for _ in ids)
    query += f" AND t.account_id IN ({placeholders})"
    parameters.extend(ids)
    if filters.text:
        pattern = f"%{filters.text.strip()}%"
        query += """ AND (
            t.purpose LIKE ? COLLATE NOCASE OR COALESCE(t.counterparty, '') LIKE ? COLLATE NOCASE OR
            COALESCE(t.reference, '') LIKE ? COLLATE NOCASE OR COALESCE(t.note, '') LIKE ? COLLATE NOCASE OR
            COALESCE(m.canonical_name, '') LIKE ? COLLATE NOCASE OR COALESCE(c.code, '') LIKE ? COLLATE NOCASE OR
            EXISTS (SELECT 1 FROM ledger_transaction_tags stt JOIN ledger_tags stag ON stag.id=stt.tag_id
                    WHERE stt.transaction_id=t.id AND stag.name LIKE ? COLLATE NOCASE) OR
            EXISTS (SELECT 1 FROM ledger_transaction_dimensions std
                    JOIN ledger_dimension_values sdv ON sdv.id=std.dimension_value_id
                    WHERE std.transaction_id=t.id AND (sdv.code LIKE ? COLLATE NOCASE OR sdv.name LIKE ? COLLATE NOCASE)) OR
            EXISTS (SELECT 1 FROM ledger_transaction_sources sts
                    JOIN ledger_source_transaction_attributes sta
                      ON sta.source_transaction_id=sts.source_transaction_id
                    WHERE sts.transaction_id=t.id AND (
                        sta.attribute_key LIKE ? COLLATE NOCASE OR
                        sta.attribute_value LIKE ? COLLATE NOCASE))
        )"""
        parameters.extend([pattern] * 11)
    for column, value, operator in (
        ("t.booking_date", filters.booking_from, ">="),
        ("t.booking_date", filters.booking_to, "<="),
        ("t.amount_minor", filters.amount_min_minor, ">="),
        ("t.amount_minor", filters.amount_max_minor, "<="),
    ):
        if value is not None:
            query += f" AND {column} {operator} ?"
            parameters.append(value)
    query, parameters = _append_in_filter(
        query, parameters, "t.currency", filters.currencies, upper=True
    )
    statuses = tuple(transaction_status(status).value for status in filters.booking_statuses)
    query, parameters = _append_in_filter(query, parameters, "t.booking_status", statuses)
    query, parameters = _append_in_filter(query, parameters, "c.code", filters.category_codes)
    query, parameters = _append_in_filter(
        query, parameters, "cl.source", filters.classification_sources
    )
    if filters.merchant:
        query += " AND m.canonical_name LIKE ? COLLATE NOCASE"
        parameters.append(f"%{filters.merchant.strip()}%")
    for tag_name in filters.tags:
        query += """ AND EXISTS (SELECT 1 FROM ledger_transaction_tags ftt JOIN ledger_tags ftag ON ftag.id=ftt.tag_id
                                  WHERE ftt.transaction_id=t.id AND ftag.name=? COLLATE NOCASE)"""
        parameters.append(tag_name)
    for dimension_code, values in sorted(filters.dimensions.items()):
        query += """ AND EXISTS (SELECT 1 FROM ledger_transaction_dimensions ftd
                                  JOIN ledger_dimensions fd ON fd.id=ftd.dimension_id
                                  JOIN ledger_dimension_values fdv ON fdv.id=ftd.dimension_value_id
                                  WHERE ftd.transaction_id=t.id AND fd.code=? COLLATE NOCASE"""
        parameters.append(dimension_code)
        if values:
            value_placeholders = ",".join("?" for _ in values)
            query += f" AND fdv.code IN ({value_placeholders})"
            parameters.extend(values)
        query += ")"
    if filters.document_status:
        if filters.document_status not in {"matched", "unmatched"}:
            raise ValueError("document_status must be matched or unmatched")
        query += " AND " if filters.document_status == "matched" else " AND NOT "
        query += """EXISTS (SELECT 1 FROM ledger_document_matches fdm
                              WHERE fdm.transaction_id=t.id AND fdm.status='matched')"""
    if filters.only_unreviewed:
        query += """ AND EXISTS (SELECT 1 FROM ledger_review_items fri
                                  WHERE fri.entity_type='transaction' AND fri.entity_id=t.id AND fri.status='open')"""
    return query, parameters


def _append_in_filter(
    query: str, parameters: list[Any], column: str, values: Iterable[str], *, upper: bool = False
) -> tuple[str, list[Any]]:
    prepared = tuple(value.upper() if upper else value for value in values)
    if prepared:
        query += f" AND {column} IN ({','.join('?' for _ in prepared)})"
        parameters.extend(prepared)
    return query, parameters
