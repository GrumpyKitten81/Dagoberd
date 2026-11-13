PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ledger_providers (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    display_name TEXT NOT NULL,
    configuration_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(kind, display_name)
);

CREATE TABLE IF NOT EXISTS ledger_accounts (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL REFERENCES ledger_providers(id),
    external_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'EUR',
    account_type TEXT NOT NULL DEFAULT 'checking' CHECK(account_type IN (
        'checking', 'credit_card', 'savings', 'cash', 'loan', 'asset',
        'liability'
    )),
    last_balance_minor INTEGER,
    last_balance_at TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(provider_id, external_id)
);

CREATE TABLE IF NOT EXISTS ledger_account_groups (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ledger_account_group_members (
    group_id TEXT NOT NULL REFERENCES ledger_account_groups(id) ON DELETE CASCADE,
    account_id TEXT NOT NULL REFERENCES ledger_accounts(id) ON DELETE CASCADE,
    PRIMARY KEY(group_id, account_id)
);

CREATE TABLE IF NOT EXISTS ledger_account_source_routes (
    source_account_id TEXT PRIMARY KEY REFERENCES ledger_accounts(id) ON DELETE CASCADE,
    target_account_id TEXT NOT NULL REFERENCES ledger_accounts(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(source_account_id <> target_account_id)
);
CREATE INDEX IF NOT EXISTS ix_ledger_account_source_routes_target
ON ledger_account_source_routes(target_account_id);

CREATE TABLE IF NOT EXISTS ledger_source_transactions (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL REFERENCES ledger_providers(id),
    account_id TEXT NOT NULL REFERENCES ledger_accounts(id),
    external_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    raw_payload_json TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(provider_id, account_id, external_id)
);

CREATE TABLE IF NOT EXISTS ledger_source_transaction_revisions (
    id TEXT PRIMARY KEY,
    source_transaction_id TEXT NOT NULL REFERENCES ledger_source_transactions(id),
    idempotency_key TEXT NOT NULL UNIQUE,
    raw_payload_json TEXT NOT NULL,
    normalized_payload_json TEXT NOT NULL,
    booking_status TEXT NOT NULL CHECK(booking_status IN ('pending', 'booked', 'reversed', 'corrected')),
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_ledger_source_revisions_source
ON ledger_source_transaction_revisions(source_transaction_id, imported_at);

CREATE TABLE IF NOT EXISTS ledger_sync_runs (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL REFERENCES ledger_providers(id),
    status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed')),
    account_ids_json TEXT NOT NULL DEFAULT '[]',
    booking_from TEXT,
    booking_to TEXT,
    cursor_json TEXT NOT NULL DEFAULT '{}',
    imported_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_ledger_sync_runs_provider_started
ON ledger_sync_runs(provider_id, started_at DESC);

CREATE TABLE IF NOT EXISTS ledger_sync_account_runs (
    sync_run_id TEXT NOT NULL REFERENCES ledger_sync_runs(id) ON DELETE CASCADE,
    account_id TEXT NOT NULL REFERENCES ledger_accounts(id),
    status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed')),
    cursor INTEGER NOT NULL DEFAULT 0,
    imported_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    PRIMARY KEY(sync_run_id, account_id)
);

CREATE TABLE IF NOT EXISTS ledger_import_previews (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    account_id TEXT REFERENCES ledger_accounts(id),
    status TEXT NOT NULL CHECK(status IN ('prepared', 'executed', 'failed')),
    mapper_version TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    executed_at TEXT
);

CREATE TABLE IF NOT EXISTS ledger_import_runs (
    id TEXT PRIMARY KEY,
    preview_id TEXT NOT NULL UNIQUE REFERENCES ledger_import_previews(id),
    imported_count INTEGER NOT NULL,
    skipped_count INTEGER NOT NULL,
    updated_count INTEGER NOT NULL DEFAULT 0,
    account_count INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ledger_transactions (
    id TEXT PRIMARY KEY,
    source_transaction_id TEXT NOT NULL UNIQUE REFERENCES ledger_source_transactions(id),
    account_id TEXT NOT NULL REFERENCES ledger_accounts(id),
    booking_date TEXT NOT NULL,
    valuta_date TEXT,
    amount_minor INTEGER NOT NULL,
    currency TEXT NOT NULL,
    purpose TEXT NOT NULL,
    counterparty TEXT,
    reference TEXT,
    note TEXT NOT NULL DEFAULT '',
    merchant_id TEXT REFERENCES ledger_merchants(id),
    booking_status TEXT NOT NULL DEFAULT 'booked' CHECK(booking_status IN ('pending', 'booked', 'reversed', 'corrected')),
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active', 'split_parent', 'void')),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_ledger_transactions_date ON ledger_transactions(booking_date);
CREATE INDEX IF NOT EXISTS ix_ledger_transactions_amount ON ledger_transactions(amount_minor, currency);
CREATE INDEX IF NOT EXISTS ix_ledger_transactions_booking_status
ON ledger_transactions(booking_status, booking_date);
CREATE INDEX IF NOT EXISTS ix_ledger_transactions_merchant
ON ledger_transactions(merchant_id, booking_date);

CREATE TABLE IF NOT EXISTS ledger_transaction_sources (
    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(id) ON DELETE CASCADE,
    source_transaction_id TEXT NOT NULL UNIQUE
        REFERENCES ledger_source_transactions(id) ON DELETE CASCADE,
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0, 1)),
    linked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(transaction_id, source_transaction_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_transaction_sources_primary
ON ledger_transaction_sources(transaction_id) WHERE is_primary=1;
CREATE INDEX IF NOT EXISTS ix_ledger_transaction_sources_transaction
ON ledger_transaction_sources(transaction_id, linked_at);

CREATE TABLE IF NOT EXISTS ledger_source_transaction_attributes (
    source_transaction_id TEXT NOT NULL
        REFERENCES ledger_source_transactions(id) ON DELETE CASCADE,
    attribute_key TEXT NOT NULL COLLATE NOCASE,
    attribute_value TEXT NOT NULL,
    value_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(source_transaction_id, attribute_key)
);
CREATE INDEX IF NOT EXISTS ix_ledger_source_attributes_key_value
ON ledger_source_transaction_attributes(attribute_key, attribute_value COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS ix_ledger_source_attributes_value
ON ledger_source_transaction_attributes(attribute_value COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS ledger_account_field_options (
    account_id TEXT NOT NULL REFERENCES ledger_accounts(id) ON DELETE CASCADE,
    field_kind TEXT NOT NULL CHECK(field_kind IN ('category', 'tag', 'dimension_value')),
    field_code TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(account_id, field_kind, field_code)
);

CREATE TABLE IF NOT EXISTS ledger_transaction_status_events (
    id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(id),
    from_status TEXT CHECK(from_status IS NULL OR from_status IN ('pending', 'booked', 'reversed', 'corrected')),
    to_status TEXT NOT NULL CHECK(to_status IN ('pending', 'booked', 'reversed', 'corrected')),
    transaction_version INTEGER NOT NULL,
    reason TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_ledger_transaction_status_events_transaction
ON ledger_transaction_status_events(transaction_id, created_at);

CREATE TABLE IF NOT EXISTS ledger_transaction_relations (
    id TEXT PRIMARY KEY,
    relation_type TEXT NOT NULL CHECK(relation_type IN ('transfer', 'refund', 'reversal', 'correction')),
    source_transaction_id TEXT NOT NULL REFERENCES ledger_transactions(id),
    target_transaction_id TEXT NOT NULL REFERENCES ledger_transactions(id),
    status TEXT NOT NULL DEFAULT 'suggested' CHECK(status IN ('suggested', 'confirmed', 'rejected', 'resolved')),
    score REAL NOT NULL DEFAULT 1.0 CHECK(score >= 0.0 AND score <= 1.0),
    reasons_json TEXT NOT NULL DEFAULT '[]',
    fee_amount_minor INTEGER NOT NULL DEFAULT 0 CHECK(fee_amount_minor >= 0),
    source_version INTEGER NOT NULL,
    target_version INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(source_transaction_id <> target_transaction_id),
    UNIQUE(relation_type, source_transaction_id, target_transaction_id)
);
CREATE INDEX IF NOT EXISTS ix_ledger_transaction_relations_status
ON ledger_transaction_relations(status, relation_type, updated_at);
CREATE INDEX IF NOT EXISTS ix_ledger_transaction_relations_source
ON ledger_transaction_relations(source_transaction_id, status);
CREATE INDEX IF NOT EXISTS ix_ledger_transaction_relations_target
ON ledger_transaction_relations(target_transaction_id, status);

CREATE TABLE IF NOT EXISTS ledger_balance_snapshots (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES ledger_accounts(id),
    balance_date TEXT NOT NULL,
    balance_minor INTEGER NOT NULL,
    currency TEXT NOT NULL,
    source TEXT NOT NULL,
    external_id TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_ledger_balance_snapshots_account_date
ON ledger_balance_snapshots(account_id, balance_date, created_at);

CREATE TABLE IF NOT EXISTS ledger_reconciliations (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES ledger_accounts(id),
    snapshot_id TEXT NOT NULL UNIQUE REFERENCES ledger_balance_snapshots(id),
    previous_snapshot_id TEXT REFERENCES ledger_balance_snapshots(id),
    calculated_balance_minor INTEGER,
    difference_minor INTEGER,
    transaction_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN ('baseline', 'balanced', 'difference')),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_ledger_reconciliations_account
ON ledger_reconciliations(account_id, created_at);

CREATE TABLE IF NOT EXISTS ledger_merchants (
    id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ledger_merchant_aliases (
    id TEXT PRIMARY KEY,
    merchant_id TEXT NOT NULL REFERENCES ledger_merchants(id) ON DELETE CASCADE,
    normalized_alias TEXT NOT NULL COLLATE NOCASE UNIQUE,
    display_alias TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ledger_tags (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ledger_transaction_tags (
    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(id) ON DELETE CASCADE,
    tag_id TEXT NOT NULL REFERENCES ledger_tags(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(transaction_id, tag_id)
);
CREATE INDEX IF NOT EXISTS ix_ledger_transaction_tags_tag
ON ledger_transaction_tags(tag_id, transaction_id);

CREATE TABLE IF NOT EXISTS ledger_dimensions (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL COLLATE NOCASE UNIQUE,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ledger_dimension_values (
    id TEXT PRIMARY KEY,
    dimension_id TEXT NOT NULL REFERENCES ledger_dimensions(id) ON DELETE CASCADE,
    code TEXT NOT NULL COLLATE NOCASE,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(dimension_id, code)
);

CREATE TABLE IF NOT EXISTS ledger_transaction_dimensions (
    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(id) ON DELETE CASCADE,
    dimension_id TEXT NOT NULL REFERENCES ledger_dimensions(id) ON DELETE CASCADE,
    dimension_value_id TEXT NOT NULL REFERENCES ledger_dimension_values(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(transaction_id, dimension_id)
);
CREATE INDEX IF NOT EXISTS ix_ledger_transaction_dimensions_value
ON ledger_transaction_dimensions(dimension_value_id, transaction_id);

CREATE TABLE IF NOT EXISTS ledger_account_columns (
    account_id TEXT NOT NULL REFERENCES ledger_accounts(id) ON DELETE CASCADE,
    dimension_id TEXT NOT NULL REFERENCES ledger_dimensions(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    PRIMARY KEY(account_id, dimension_id)
);

CREATE TABLE IF NOT EXISTS ledger_review_items (
    id TEXT PRIMARY KEY,
    item_type TEXT NOT NULL CHECK(item_type IN (
        'new_transaction', 'pending_transaction', 'classification_suggestion',
        'relation_suggestion', 'document_match', 'unmatched_document',
        'reconciliation_difference', 'duplicate_candidate', 'import_warning',
        'processing_error'
    )),
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 50 CHECK(priority BETWEEN 0 AND 100),
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'resolved', 'dismissed')),
    version INTEGER NOT NULL DEFAULT 1,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_review_items_open_entity
ON ledger_review_items(item_type, entity_type, entity_id)
WHERE status='open';
CREATE INDEX IF NOT EXISTS ix_ledger_review_items_status
ON ledger_review_items(status, priority DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS ledger_bulk_previews (
    id TEXT PRIMARY KEY,
    action TEXT NOT NULL CHECK(action IN (
        'categorize', 'tag_add', 'tag_remove', 'dimension_set',
        'dimension_clear', 'merchant_set', 'edit_fields'
    )),
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'prepared' CHECK(status IN ('prepared', 'executed', 'expired')),
    transaction_count INTEGER NOT NULL,
    version_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    executed_at TEXT
);

CREATE TABLE IF NOT EXISTS ledger_bulk_preview_items (
    preview_id TEXT NOT NULL REFERENCES ledger_bulk_previews(id) ON DELETE CASCADE,
    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(id),
    expected_version INTEGER NOT NULL,
    PRIMARY KEY(preview_id, transaction_id)
);

CREATE TABLE IF NOT EXISTS ledger_saved_analyses (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL COLLATE NOCASE UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    definition_json TEXT NOT NULL,
    contract_version TEXT NOT NULL DEFAULT 'dagoberd-analysis-v1',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ledger_exchange_rates (
    id TEXT PRIMARY KEY,
    source_currency TEXT NOT NULL,
    target_currency TEXT NOT NULL,
    rate_decimal TEXT NOT NULL,
    rate_date TEXT NOT NULL,
    source TEXT NOT NULL,
    source_version TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(length(source_currency)=3),
    CHECK(length(target_currency)=3),
    CHECK(source_currency<>target_currency),
    UNIQUE(source_currency, target_currency, rate_date, source, source_version)
);
CREATE INDEX IF NOT EXISTS ix_ledger_exchange_rates_lookup
ON ledger_exchange_rates(source_currency, target_currency, rate_date DESC);

CREATE TABLE IF NOT EXISTS ledger_analysis_runs (
    id TEXT PRIMARY KEY,
    saved_analysis_id TEXT REFERENCES ledger_saved_analyses(id),
    definition_version INTEGER NOT NULL,
    definition_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    result_hash TEXT NOT NULL,
    output_paths_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_ledger_analysis_runs_saved
ON ledger_analysis_runs(saved_analysis_id, created_at DESC);

CREATE TABLE IF NOT EXISTS ledger_splits (
    id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(id),
    source_transaction_id TEXT REFERENCES ledger_source_transactions(id),
    amount_minor INTEGER NOT NULL,
    category_id TEXT,
    memo TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ledger_categories (
    id TEXT PRIMARY KEY,
    parent_id TEXT REFERENCES ledger_categories(id),
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ledger_profiles (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ledger_profile_categories (
    profile_id TEXT NOT NULL REFERENCES ledger_profiles(id),
    category_id TEXT NOT NULL REFERENCES ledger_categories(id),
    PRIMARY KEY(profile_id, category_id)
);

CREATE TABLE IF NOT EXISTS ledger_rules (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES ledger_profiles(id),
    name TEXT NOT NULL,
    pattern TEXT NOT NULL,
    category_id TEXT NOT NULL REFERENCES ledger_categories(id),
    priority INTEGER NOT NULL DEFAULT 100,
    active INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 1,
    account_id TEXT REFERENCES ledger_accounts(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ledger_hints (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES ledger_profiles(id),
    text TEXT NOT NULL,
    pattern TEXT,
    category_id TEXT REFERENCES ledger_categories(id),
    priority INTEGER NOT NULL DEFAULT 100,
    active INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 1,
    account_id TEXT REFERENCES ledger_accounts(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ledger_account_profiles (
    account_id TEXT NOT NULL REFERENCES ledger_accounts(id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL REFERENCES ledger_profiles(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    PRIMARY KEY(account_id, profile_id)
);

CREATE TABLE IF NOT EXISTS ledger_account_stages (
    account_id TEXT NOT NULL REFERENCES ledger_accounts(id) ON DELETE CASCADE,
    stage TEXT NOT NULL CHECK(stage IN ('rule', 'history', 'hint', 'llm')),
    enabled INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(account_id, stage)
);

CREATE TABLE IF NOT EXISTS ledger_classifications (
    id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(id),
    profile_id TEXT NOT NULL REFERENCES ledger_profiles(id),
    category_id TEXT REFERENCES ledger_categories(id),
    source TEXT NOT NULL CHECK(source IN ('rule', 'history', 'hint', 'llm', 'manual', 'historical_transfer')),
    confidence REAL NOT NULL,
    rationale TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('applied', 'suggested', 'rejected')),
    model_version TEXT,
    prompt_hash TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_ledger_classifications_transaction ON ledger_classifications(transaction_id, profile_id, created_at DESC);

CREATE TABLE IF NOT EXISTS ledger_llm_previews (
    id TEXT PRIMARY KEY,
    profile_code TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('local', 'cloud')),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    request_context_json TEXT,
    payload_json TEXT NOT NULL,
    transaction_versions_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ready', 'running', 'executed', 'failed', 'expired', 'invalidated')),
    result_json TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    executed_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_ledger_llm_previews_status
ON ledger_llm_previews(status, expires_at);

CREATE TABLE IF NOT EXISTS ledger_documents (
    id TEXT PRIMARY KEY,
    external_id TEXT,
    filename TEXT NOT NULL,
    document_date TEXT,
    amount_minor INTEGER,
    currency TEXT NOT NULL DEFAULT 'EUR',
    merchant TEXT,
    reference TEXT,
    storage_path TEXT,
    content_hash TEXT,
    mime_type TEXT,
    extraction_status TEXT NOT NULL DEFAULT 'metadata',
    status TEXT NOT NULL DEFAULT 'open',
    version INTEGER NOT NULL DEFAULT 1,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_documents_content_hash
ON ledger_documents(content_hash) WHERE content_hash IS NOT NULL;

CREATE TABLE IF NOT EXISTS ledger_document_origins (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES ledger_documents(id) ON DELETE CASCADE,
    origin_key TEXT NOT NULL UNIQUE,
    relative_path TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    mtime_ns INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_ledger_document_origins_document
ON ledger_document_origins(document_id, created_at);
CREATE INDEX IF NOT EXISTS ix_ledger_document_origins_content_hash
ON ledger_document_origins(content_hash);

CREATE TABLE IF NOT EXISTS ledger_document_batch_previews (
    id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    source_root TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'prepared'
        CHECK(status IN ('prepared', 'executed', 'failed')),
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    executed_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_ledger_document_batch_previews_status
ON ledger_document_batch_previews(status, created_at DESC);

CREATE TABLE IF NOT EXISTS ledger_document_matches (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES ledger_documents(id),
    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(id),
    score REAL NOT NULL,
    reasons_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('suggested', 'matched', 'rejected')),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(document_id, transaction_id)
);

CREATE TABLE IF NOT EXISTS ledger_report_runs (
    id TEXT PRIMARY KEY,
    profile_code TEXT,
    account_scope_json TEXT NOT NULL DEFAULT '{}',
    booking_from TEXT,
    booking_to TEXT,
    formats_json TEXT NOT NULL,
    output_paths_json TEXT NOT NULL,
    transaction_count INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ledger_delivery_previews (
    id TEXT PRIMARY KEY,
    report_run_id TEXT NOT NULL REFERENCES ledger_report_runs(id),
    target_name TEXT NOT NULL,
    recipients_json TEXT NOT NULL,
    message_json TEXT NOT NULL,
    artifacts_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ready', 'sent', 'expired', 'invalidated')),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_ledger_delivery_previews_status
ON ledger_delivery_previews(status, expires_at);

CREATE TABLE IF NOT EXISTS ledger_service_runs (
    id TEXT PRIMARY KEY,
    service TEXT NOT NULL,
    operation TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS ledger_mcp_requests (
    idempotency_key TEXT PRIMARY KEY,
    tool_name TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ledger_audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    actor TEXT NOT NULL DEFAULT 'system',
    correlation_id TEXT,
    idempotency_key TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_audit_events_idempotency_key
ON ledger_audit_events(idempotency_key)
WHERE idempotency_key IS NOT NULL;

DROP VIEW IF EXISTS v_ledger_active_transactions;
CREATE VIEW v_ledger_active_transactions AS
SELECT t.id, t.source_transaction_id, t.account_id, t.booking_date, t.valuta_date,
       t.amount_minor, t.currency, t.purpose, t.counterparty, t.reference, t.note,
       t.merchant_id,
       t.booking_status, t.state,
       t.version, t.created_at, t.updated_at, NULL AS split_category_id,
       a.external_id AS account_external_id, a.display_name AS account_name,
       CASE WHEN t.booking_status <> 'booked' OR EXISTS (
           SELECT 1 FROM ledger_transaction_relations relation
           WHERE relation.status='confirmed'
             AND relation.relation_type IN ('transfer', 'reversal')
             AND (relation.source_transaction_id=t.id OR relation.target_transaction_id=t.id)
       ) THEN 1 ELSE 0 END AS cashflow_excluded,
       CASE WHEN EXISTS (
           SELECT 1 FROM ledger_transaction_relations relation
           WHERE relation.status='confirmed' AND relation.relation_type='transfer'
             AND (relation.source_transaction_id=t.id OR relation.target_transaction_id=t.id)
       ) THEN 1 ELSE 0 END AS is_internal_transfer
FROM ledger_transactions t JOIN ledger_accounts a ON a.id = t.account_id
WHERE t.state = 'active'
UNION ALL
SELECT s.id, t.source_transaction_id, t.account_id, t.booking_date, t.valuta_date,
       s.amount_minor, t.currency, t.purpose || ' (Split)', t.counterparty, t.reference, t.note,
       t.merchant_id,
       t.booking_status, 'active',
       t.version, s.created_at, s.created_at, s.category_id,
       a.external_id, a.display_name,
       CASE WHEN t.booking_status <> 'booked' THEN 1 ELSE 0 END,
       0
FROM ledger_splits s
JOIN ledger_transactions t ON t.id = s.transaction_id
JOIN ledger_accounts a ON a.id = t.account_id
WHERE t.state = 'split_parent';

CREATE TABLE IF NOT EXISTS ledger_account_deletions (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('prepared', 'executed')),
    receipt_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    executed_at TEXT
);
