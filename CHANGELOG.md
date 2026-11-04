# Changelog

## 0.3.0b1 — Public-beta candidate

### Features

- English project documentation, CLI and reports; centrally cataloged English/German/Spanish GUI with persistent language choice and live draft-preserving switching.
- File imports are preferred; Comdirect API coverage remains experimental and incomplete.

- Local token-protected loopback interface for account overview, search, editing, imports, receipt review and Comdirect authentication jobs, sharing the audited CLI/MCP service layer.
- Experimental read-only Comdirect synchronization with source revisions, pending/booked status, balance snapshots, reconciliation and resumable recovery; CAMT.053 and configurable CSV/XLS/XLSX imports with structured previews and duplicate checks.
- Manual accounts and transactions, tags, custom dimensions, merchant aliases, review inbox and atomic version-protected bulk operations.
- Explainable rules, confirmed history, user hints and optional local/cloud LLM suggestions, with account-specific pipeline configuration.
- Generic profile reports and versioned saved analyses for cashflow, income/expenses, fixed costs, trends and net worth. Dated exchange rates retain provenance and expose incomplete totals.
- Explicit local report extensions through CLI and MCP; personal reports, rules, import mappings and portable maintenance workflows can live in a private partner repository.
- Safe setup, synthetic demo, offline self-test, shell completion and optional installation-bound MCP launcher.
- Portable checksummed export, AES-256-GCM backups with Scrypt, verified restore with rollback backup, and fingerprint-bound deletion previews.
- Optional SMTP report delivery with TLS, masked previews, artifact hashes, local confirmation, idempotency and audit.

### Security and quality

- Public issue forms for bugs, feature requests and security reports; verified Action commit pins, no retained checkout credentials and explicit job limits.

- LLM endpoints distinguish loopback from remote targets; local calls ignore environment proxies and model requests never follow redirects. Preview v2 binds the endpoint and request configuration, preserves historical audit data and invalidates old unbound previews. Incomplete or invalid model responses fail visibly without synthesized fields.

- Apache-2.0 licensing and documented security, privacy, support and threat boundaries.
- Cloud LLM requests require an explicit redacted payload preview, expiry and fingerprint; model results remain reviewable suggestions.
- Unfiltered Quality checks for formatting, lint, types, the complete test suite and builds, alongside staged Linux/macOS/Windows artifact checks and separate browser tests.
- Dependency auditing and secrets-baseline checks; release workflows produce SBOMs and build provenance. Repository-hosted security settings require separate verification before publication.

### Changes

- SQLite initialization and integrity/portability reads explicitly close their connections so Windows restore staging and rollback cleanup can release database files immediately.
- The English classifier prompt advances to v3; previews bound to the previous prompt version must be recreated. Historical results and machine-readable contracts are preserved.
- Local endpoint validation explicitly rejects IPv4-mapped IPv6 addresses consistently across Python versions.

- CLI contract advances to `account-eval-cli-v2`: classification actions use `classifications set|apply-rules|deterministic`, profile creation uses `profiles create`, rule/hint creation uses `rules add` and `hints add`, and receipt metadata registration uses `documents register`.
- File imports use `import preview` followed by `import run`; receipt candidates use `documents candidates`. Previous top-level names are removed without compatibility aliases. MCP and report-extension contracts remain unchanged.
- Repeated setup updates only assistant-managed connection, LLM and base-currency values while preserving data locations, extensions, other connections and local limits.
- Restore stages document and report files beside their destinations so atomic replacement also works across filesystem boundaries.
- Restore validates and prepares the complete input before replacement, rebases stored document paths, and rolls back database and file changes on handled transfer errors. Failed rollback retains local recovery material and blocks another restore until manual recovery; CLI errors distinguish rollback success from required recovery.
- Restore resolves Windows drive/UNC and Unix source paths independently of the current platform. Document, report and analysis references must map to exactly one archived file before target data changes; missing, external or ambiguous references are rejected.
- SQLite backups, ledger JSON, portable exports and encrypted backups are prepared with restrictive permissions, verified and synced before atomic creation of the final name. Existing targets are preserved even when creators race; CLI target conflicts report `already_exists`. Target and temporary filesystems must support hard links.
- WISO free-field taxonomy mappings are explicit TOML configuration; source files, field identities and normalized mappings are bound to the preview fingerprint. Category/tax-only imports need no free-field mapping.
- Data repositories are selected explicitly; fresh installations use `output/` and need no partner repository or personal model provider.
- Personal reporting, maintenance scripts and household workflows leave the public core. Private package metadata, unused extraction code and input-dependent legacy tests are removed.

## 0.2.0 — 2026-07-18

- Local, migration-capable ledger with account/provider attribution.
- Experimental read-only Comdirect synchronization and controlled tabular imports.
- Local receipt extraction, matching and review.
- Multi-stage classification with optional local or cloud LLM.
- Account-, period- and profile-scoped JSON/XLSX/HTML reports.
- Client-neutral MCP with Codex and OpenCode integration.

Real bank, cloud-model and SMTP checks remain explicit local smoke tests. Receipt metadata extraction is heuristic and may need manual corrections.
