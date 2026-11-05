# Dagoberd Runbook

This is the operational, architecture, integration and verification reference for V1/V2. Product scope and acceptance criteria are defined in the [PRD](PRD.md). Examples use synthetic identifiers. Prefix commands with `poetry run` in a checkout; an installed package exposes `dagoberd` directly. Global `--config`, `--db` and `--output human|json|legacy-json` options precede the command.

## 1. Architecture and security model

```text
CSV / XLS / XLSX / CAMT.053 / experimental Comdirect
                   ↓
provider → account → immutable source → normalized transaction
                                     ↘ classification / splits
                                     ↘ document / match
                                     ↘ scoped report / analysis
```

SQLite stores money in integer minor units. Each transaction belongs to one account; stable identities and source hashes provide idempotency. Versions reject concurrent overwrites and audit events record mutations. Active splits replace their parent in reports and must sum exactly to its amount.

Booking lifecycle (`pending`, `booked`, `reversed`, `corrected`) is separate from split state. Source revisions and status events are immutable. Transfers, refunds, reversals and corrections are explicit versioned relations. Balance snapshots reconcile booked movements between dates; the first snapshot is a baseline.

CLI, GUI and MCP share services. Bulk changes require a persistent version-bound preview and atomic execution. Analyses retain definition/rate snapshots, data fingerprints and one canonical result for every renderer. Missing conversion rates leave separate currencies and incomplete null totals.

File imports are recommended. Comdirect is experimental, strictly read-only and incomplete; savings-account coverage is not verified and Visa credit-card transactions are unavailable. Other bank APIs are future work. Never infer financial completeness from a successful API request.

MCP accepts no PINs, TANs or secrets. Authentication is local through CLI or the token-protected loopback GUI. Cloud classification and SMTP require exact previews and deliberate authorization. Agent-owned data transfers remain outside Dagoberd's controls.

## 2. Installation and initialization

Use Python 3.12+ and Poetry 2.2+. Keep the tracked lockfile synchronized:

```bash
poetry sync
poetry run pre-commit install
poetry run dagoberd setup --yes
poetry run dagoberd config validate
poetry run dagoberd self-test
```

Install the hook once per checkout. Setup creates project configuration, restrictive local overrides, database and built-in profiles as needed. Repeated setup preserves TOML structure and changes only `comdirect.connections.primary.enabled`, managed LLM endpoint/model/mode/opt-in fields and `reporting.base_currency`. It preserves data paths, extensions, formats, other connections, session parameters, model limits and SMTP targets. Validate the complete candidate before atomic replacement; invalid configuration must neither replace the original nor initialize a database.

Self-test uses synthetic data, starts a real local stdio MCP and checks reporting and portability without external networking:

```bash
poetry run dagoberd demo create
poetry run dagoberd --config .account-eval/demo/account-eval.toml gui
poetry run dagoberd demo reset --yes
```

A built wheel can be installed with pipx. Before upgrades or uninstalling, create and verify a backup. Uninstalling the program does not delete workspace files or keyring secrets.

## 3. Local configuration

See `account-eval.toml.example`. Personal choices belong in ignored `account-eval.local.toml`.

### Secrets

General resolution is OS keyring → environment → ignored local `.env`, where supported. Interactive entry avoids shell-history exposure:

```bash
poetry run dagoberd config set-secret comdirect.private.username
poetry run dagoberd config set-secret comdirect.private.client-id
poetry run dagoberd config set-secret comdirect.private.client-secret
poetry run dagoberd config set-secret llm.api-key
```

Comdirect credentials use the keyring exclusively. PIN entry is local and interactive, never an environment variable, `.env` value or saved secret. GUI PIN/TAN input is held only in process memory and never returned by endpoints. Timed sessions may explicitly retain tokens in the keyring; they never persist the PIN.

### Optional classifier

The calling coding agent and Dagoberd classifier are independent. Dagoberd does not launch an agent or inherit its model/login. The classifier has no research, file or agent tools. See [Agent and LLM integration](AGENT_LLM_INTEGRATION.md).

```toml
[llm]
enabled = true
mode = "local"
provider = "openai-compatible"
base_url = "http://127.0.0.1:11434/v1"
model = "local-model"
allow_cloud = false
```

Local targets must be IPv4/IPv6 loopback or `localhost` resolving exclusively to loopback. LAN servers need cloud mode and opt-in. Requests never follow redirects; local requests ignore environment proxies. These rules also apply to `llm test`.

Cloud use requires `enabled=true`, `mode="cloud"` and `allow_cloud=true`, plus the chosen endpoint/model and secret reference. The exact redacted payload, endpoint, non-secret configuration snapshot, expiry and fingerprint are preview-bound:

```bash
poetry run dagoberd llm show
poetry run dagoberd llm test
poetry run dagoberd classifications preview freelance --transaction <transaction-id>
poetry run dagoberd classifications run freelance --preview <preview-id> --fingerprint <sha256>
poetry run dagoberd classifications review --profile freelance --status suggested
```

Preview creation can already apply deterministic classifications and requires authorization to mutate. Changed transactions or request settings require a new preview. Preview v2 preserves historical results while invalidating legacy unbound open previews.

### Data roots and base currency

Without overrides, Dagoberd uses its own `output/`; it never selects an adjacent repository automatically. Optional private data and synchronized file storage are independent:

```toml
[data]
root = "/path/to/private-workspace"
[cloud]
workspace = "/path/to/synchronized-files"
```

Derived paths are `{data.root}/ledger/account_eval.db`, `{cloud.workspace}/documents` and `{cloud.workspace}/reports`; explicit database/document/report paths still override them. Environment overrides are `ACCOUNT_EVAL_DATA_ROOT` and `ACCOUNT_EVAL_CLOUD_WORKSPACE`.

Close GUI/MCP and database writers before versioning a private ledger. Never write to the same synchronized database from two machines. On a new machine, explicitly configure roots/extensions and secrets, then run `doctor` and `accounts list`.

`reporting.base_currency` defaults to EUR. It does not enable network rate queries. Rates are local dated records with source/version provenance.

## 4. Accounts and data intake

### Local GUI

```bash
poetry run dagoberd gui
poetry run dagoberd gui --port 8766 --no-browser
Scripts/start-gui.sh --port 8766
Scripts/gui-control.sh status
Scripts/stop-gui.sh
```

The GUI binds only to loopback. Its URL carries an ephemeral fragment token, immediately removed and used in request headers. It displays unmasked data; do not share the URL or bind externally. Managed launcher scripts restart only their verified process, store restrictive temporary process metadata and stop gracefully. A legacy foreground process can be adopted with `stop-gui.sh --port PORT` only when command and working directory identify this checkout; never kill a process solely by port.

Five areas cover overview, transactions, administration, data and classification. Scope and area survive reload through the URL. Overview and transactions share all enabled accounts, a group or a custom selection. The grid fetches 200-row pages; click or `F2` edits a cell, `Enter` saves, `Escape` cancels and `Tab` moves between editable cells. Conflicts reload the affected row. Details include splits, source, documents, classification, status and relations; the bulk inspector uses preview/fingerprint/version protection.

The language picker offers English, German and Spanish. First use is English; selection is stored locally. Switching updates existing text, accessible names and number/date formatting without reloading, losing drafts or repeating operations. Central catalogs use semantic keys, named parameters and plural variants; a new language adds a catalog and registry entry. User labels and source data are not automatically translated.

### Preferred file workflow

For the complete authorized first-run and follow-up agent journey, including a
synthetic demonstration, see [agent-guided account imports](AGENT_ACCOUNT_IMPORT.md).

```bash
poetry run dagoberd accounts register file BankExport ACCOUNT-EXAMPLE "Import account"
poetry run dagoberd accounts register file VisaExample VISA-1234 "Visa example" --type credit_card
poetry run dagoberd accounts list
poetry run dagoberd import preview --source tabular --account <account-id> export.xlsx
poetry run dagoberd import preview --source camt053 statement.xml
poetry run dagoberd import preview --source tabular --account <account-id> --profile bank_export export.csv
poetry run dagoberd import run <preview-id>
poetry run dagoberd import history --account <account-id>
```

Preview shows target routing, mapper version, rows, valid transactions, period, totals, structured errors and duplicate identities. Execution requires the same unchanged file/mapper with no errors. `imports.max_file_size_mib` limits files. CAMT derives identity from IBAN/proprietary ID and rejects DTD/entities.

Tabular rows without an account identity require an explicitly selected account. If a
source identity conflicts with the selection, the import is blocked. Confirm a missing
source identity with the user before executing the preview.

Profiles under `[imports.profiles.<name>]` configure source, mapping, delimiter, encoding, sheet, currency, date and decimal format. Legacy `[imports.mappings.*]` remains compatible. Extra-column annotations can set categories, dimensions, tags and additional profiles; unknown values are warnings. `ignore_values` and `skip_purpose_prefixes` filter placeholders/opening rows.

The `wiso365` adapter supports generic multi-account sheets with stable `BuchungID`, source attributes and opening/month-end/closing balances. Personal mappings stay local. Unknown/ambiguous account routing blocks execution. Opening balances are observations, not cashflow. Tabular imports target file/manual accounts; API bank accounts remain sync-only.

```bash
poetry run dagoberd source-reconciliation preview --source-account <source-id> --target-account <target-id>
poetry run dagoberd source-reconciliation run --source-account <source-id> --target-account <target-id> --fingerprint <sha256>
```

Use fingerprint-bound reconciliation to connect an existing history to an additional source. For cash, create a manual account, import controlled history and use `manual add` afterward. Opening balances belong to the account, not the transaction list.

### WISO tree mappings

Category/tax trees need no personal mapping. Each supplied `--free-1`, `--free-2` or `--free-3` tree needs complete source-field/root-section mapping:

```bash
poetry run dagoberd wiso-trees preview --categories categories.Tre --tax tax.Tre
poetry run dagoberd wiso-trees run --categories categories.Tre --tax tax.Tre --fingerprint <sha256>
```

Select the file with `imports.wiso_trees.mapping_file`. It contains `version=1`, `free_fields` assigning each source field/root to dimension `code`/`name`, and optional `value_codes` assigning source labels to stable codes. Actual German source-field names remain unchanged. Direct children become values; deeper trees, unknown attributes, missing mappings and collisions fail before writes. Existing labels are preserved. Use identical free-field arguments for preview/run; hashes, source identities and normalized mapping bind the fingerprint. Formatting-only TOML changes do not invalidate it.

### Source coverage

| Source | Coverage and limits |
| --- | --- |
| CSV/XLS/XLSX | Preferred; profiles handle layout/date/decimal/encoding/sheet differences. Formulas are not executed. |
| CAMT.053 | Statements, IDs, debit/credit, dates, statuses and references; bank extras stay raw. No CAMT.052/054. |
| WISO 365 | Multi-account sheets/provenance; portfolio cashflows may import, but holdings, quantities and tax lots are outside scope. |
| Experimental Comdirect | Read-only OAuth/2FA, paging and account/balance/transaction paths have fake-provider tests. Savings coverage is not verified; Visa credit-card transactions are unavailable. |
| Other bank APIs | Not offered; use file exports. API expansion is future work. |

### Experimental Comdirect

Each named connection has separate keyring entries. The old single `[comdirect]` connection is unsupported. An HTTP allowlist excludes payments, brokerage, portfolio, PostBox and orders.

```toml
[comdirect.connections.private]
enabled = true
session_mode = "ephemeral"
[comdirect.connections.shared]
enabled = true
session_mode = "keychain_timed"
allow_mcp_sync = true
```

Credentials are `comdirect.<name>.username`, `.client-id` and `.client-secret`; old unscoped entries are not used. Default ephemeral sync requests local authentication and revokes the session afterward. Timed mode retains a separate session for at most twelve hours. MCP additionally requires `allow_mcp_sync=true`; it uses only a valid access token and cannot authenticate or renew it.

```bash
poetry run dagoberd auth comdirect shared
poetry run dagoberd sync comdirect shared --accounts <account-id> --from 2026-01-01 --to 2026-01-31
poetry run dagoberd comdirect diagnose shared
poetry run dagoberd comdirect recover shared --resume <failed-sync-id>
poetry run dagoberd comdirect logout shared
```

CLI uses masked PIN/TAN input; PushTAN is confirmed in the bank app. The GUI supplies an in-process bridge. Neither persists PIN/TAN or exposes them to agents. Paging, refresh, rate limits, timeouts and resumable errors have fake-provider coverage, not a guarantee of complete live support.

Checking/savings account-type mappings do not establish reliable product coverage. Unknown/card/portfolio products are skipped. Visa debit belongs to checking activity where exposed; Visa credit needs a separate file `credit_card` account.

For settlement, import the positive card payment and negative checking debit and link only that pair as a transfer, not every card purchase. Suggestions use opposite 1:1 amounts within `transaction_semantics.transfer_max_days` (default 5). Other configurable defaults are `transfer_max_fee_minor=1000`, `refund_max_days=90`, `correction_max_days=14` and `minimum_candidate_score=0.6`. Fees use minor units. Review suggestions; increase date limits explicitly if justified.

## 5. Documents and matching

Import documents locally with `documents import`; `documents register` records metadata without a file. Hashes prevent duplicates. Extraction is local/heuristic; configure optional OCR locally. Inspect `documents candidates` and accept/reject/unmatch with current versions. Automatic matching requires a minimum top score and gap to the runner-up; ambiguity stays open. Never send document contents to models or Issues.

### Document commands

```bash
poetry run dagoberd documents import documents/
poetry run dagoberd documents import invoice.pdf --amount 42.00 --date 2026-07-01 --merchant ACME --reference INV-1
poetry run dagoberd documents preview documents/
poetry run dagoberd documents run <preview-id> --fingerprint <sha256>
poetry run dagoberd documents list --status open
poetry run dagoberd documents preview-matches <document-id>
poetry run dagoberd documents apply-matches <document-id> \
  --transaction <transaction-id> --fingerprint <sha256> --document-version <version>
poetry run dagoberd documents accept <match-id> --version <version>
poetry run dagoberd documents reject <match-id> --version <version>
poetry run dagoberd documents unmatch <match-id> --version <version>
```

## 6. Classification and splits

The order is rules → confirmed history → hints → optional model. Rules inspect purpose in priority order; history requires an exact confirmed previous counterparty/purpose match. Hints provide deterministic patterns or model context. Account-specific profiles/stages restrict decisions.

```bash
poetry run dagoberd classifications deterministic <transaction-id> <profile>
poetry run dagoberd classifications apply-rules <transaction-id> <profile>
poetry run dagoberd classifications review --profile <profile> --status suggested
poetry run dagoberd classifications accept <classification-id> --version <transaction-version>
```

`explain` follows execution; deterministic commands never call a model. Invalid/truncated model responses fail without synthesized rationale or reconstructed fields. Category must be allowed or null; confidence and rationale remain required. Suggestions need explicit version-protected acceptance/rejection.

Splits use `splits create|replace|revert`, require the parent version and must sum exactly. Reporting never counts both parent and active splits. Consult current command help for arguments.

### Classification and split examples

```bash
poetry run dagoberd rules add rental "Rent" "(?i)miete|alquiler" income.rent --priority 10
poetry run dagoberd hints add freelance "ACME is business-related" --pattern "(?i)ACME" --category expense.business
poetry run dagoberd profiles create custom "Custom profile"
poetry run dagoberd classifications set <transaction-id> freelance expense.business --version <version>
poetry run dagoberd classifications apply-rules <transaction-id> freelance
poetry run dagoberd classifications deterministic <transaction-id> freelance
poetry run dagoberd classifications run freelance --limit 100
poetry run dagoberd classifications review --profile freelance --status suggested
poetry run dagoberd classifications accept <classification-id> --version <transaction-version>
poetry run dagoberd classifications reject <classification-id> --version <transaction-version>
```

```bash
poetry run dagoberd splits create <transaction-id> rental \
  --part '70.00:income.rent:Rent' \
  --part '30.00:income.utilities:Utilities' --version <version>
poetry run dagoberd splits revert <transaction-id> --version <version>
```

## 7. Transaction semantics and organization

Normal transitions are `pending → booked|reversed` and `booked → reversed|corrected`. Terminal states change internally only when a confirmed relation is explicitly unlinked with version protection. Revert a split before changing split booking status.

Use `relations suggest|create|confirm|reject|resolve` for transfers, refunds, reversals and corrections. Confirm only unambiguous cases with current versions. Transfers remove internal cashflow while preserving a fee once; refunds and reversals are not interchangeable. Pending transactions are excluded from booked cashflow/reconciliation. Reconciliation uses booked movements between snapshots and exposes differences rather than automatically adjusting transactions.

Manual entry is restricted to manual accounts. Stable idempotency keys prevent duplicates. Tags, one value per dimension, merchants/aliases, notes, combined search and a persistent review inbox support organization. Resolving a review item does not repair its underlying transaction.

Bulk changes require `bulk preview` then the exact `bulk run` with fingerprint, for at most 500 targets. A changed version aborts all writes. Completed previews are idempotent. Show the action, count and fingerprint before execution.

### Organization examples

```bash
poetry run dagoberd transactions --status pending booked
poetry run dagoberd transactions --all-statuses
poetry run dagoberd transaction-history <transaction-id>
poetry run dagoberd transaction-status <transaction-id> booked \
  --version <version> --reason "Bank booked the transaction"
```

```bash
poetry run dagoberd relations suggest transfer --accounts <account-1> <account-2>
poetry run dagoberd relations suggest refund --transaction <transaction-id>
poetry run dagoberd relations list --status suggested
poetry run dagoberd relations confirm <relation-id> --version <version>
poetry run dagoberd relations reject <relation-id> --version <version>
poetry run dagoberd relations resolve <relation-id> --version <version>
```

```bash
poetry run dagoberd relations create transfer <debit-id> <credit-id> \
  --source-version <version> --target-version <version> --fee 1.00 \
  --reason "Own accounts"
```

```bash
poetry run dagoberd accounts show <account-id>
poetry run dagoberd balances add <account-id> 1234.56 \
  --date 2026-07-31 --version <account-version>
poetry run dagoberd balances list --account <account-id>
poetry run dagoberd balances reconciliations --account <account-id>
poetry run dagoberd balances reconcile <snapshot-id>
```

```bash
poetry run dagoberd tags create Food
poetry run dagoberd tags add <transaction-id> Food --version <version>
poetry run dagoberd dimensions value-add household family "Family"
poetry run dagoberd dimensions set <transaction-id> household family --version <version>
poetry run dagoberd merchants create Supermarket
poetry run dagoberd merchants alias-add Supermarket "Example Market Ltd"
poetry run dagoberd merchants set <transaction-id> Supermarket --version <version>
```

```bash
poetry run dagoberd search --text supermarket --accounts <account-id> \
  --from 2026-01-01 --amount-max -1 --tag Food \
  --dimension household=family --document-status unmatched --unreviewed
poetry run dagoberd review list --status open --accounts <account-id>
poetry run dagoberd review resolve <review-id> --version <version>
```

```bash
poetry run dagoberd bulk preview categorize <transaction-id-1> <transaction-id-2> \
  --profile freelance --category expense.business
poetry run dagoberd bulk run <bulk-preview-id>
poetry run dagoberd bulk preview tag_add <transaction-id-1> <transaction-id-2> \
  --tag Food
poetry run dagoberd bulk preview edit_fields <transaction-id-1> <transaction-id-2> \
  --patch-json '{"note":{"mode":"append","text":"Reviewed together"}}'
poetry run dagoberd bulk run <bulk-preview-id> --fingerprint <fingerprint>
```

## 8. Reports and saved analyses

```bash
poetry run dagoberd report --profile rental --accounts <account-id> --from 2026-01-01 --to 2026-01-31
poetry run dagoberd reports list
```

Generic profiles produce JSON/XLSX/HTML transactions, category/month/account/currency totals and open items. Explicit IDs, groups and all-enabled scope share semantics. Reports and CLI are English; user/source labels remain unchanged. Results are not tax or investment advice.

Extensions are configured locally and selected explicitly with `report --extension <name>` or MCP `generate_report`. API v1 passes scoped copies, metadata, formats and a dedicated directory, not a ledger service. Historical data is a separate opt-in retaining account/profile/currency boundaries. Extensions are trusted local Python code with process privileges, not sandboxed. Output validation does not establish module trust. Only complete valid artifacts are registered; reporting never authorizes delivery.

Versioned `analyses` definitions store fixed/relative periods, scope, profile, filters, grouping, base currency, types and formats. Update using a full JSON definition/current version. Runs resolve time, freeze definition/rates and store canonical result, fingerprint, hash and artifact paths. XLSX and embedded HTML JSON render the same result. Group by category/month/account/merchant/tag or `dimension:<code>`.

Rates are explicit dated local records. `USD EUR 0.92` values one USD minor unit at 0.92 EUR minor units; round the final amount with Decimal. Prefer direct rates; inverses retain provenance. No silent live rates or interpolation. Missing rates make combined base totals null/incomplete. Fixed costs use the configured tag (default `fixed`) or `expense.fixed`; trends compare monthly nets. Net worth uses the latest snapshot per account up to the valuation date. Credit cards/loans/liabilities are negative absolute contributions; missing/stale balances are warned.

### Extension and analysis examples

```toml
[reporting.extensions.custom-rental]
path = "../private-reports/rental/__init__.py"
profile = "rental"
include_history = true
```

```bash
poetry run dagoberd report --extension custom-rental --accounts <account-1> \
  --from 2026-01-01 --to 2026-12-31 --currency EUR --formats json xlsx html
```

```bash
poetry run dagoberd rates add USD EUR 0.92 --date 2026-07-01 \
  --source ECB-manual --source-version 2026-07-01 \
  --idempotency-key usd-eur-2026-07-01
poetry run dagoberd rates list --source-currency USD --target-currency EUR

poetry run dagoberd analyses create personal-month "Personal monthly analysis" \
  --relative-months 1 --accounts <account-1> <account-2> \
  --tag personal --base-currency EUR \
  --sections cashflow income_expense fixed_costs trends net_worth \
  --formats json xlsx html
poetry run dagoberd analyses show personal-month
poetry run dagoberd analyses run personal-month --as-of 2026-07-31
```

## 9. Coding agents, MCP and optional delivery

Load the repository skill and use shared services through CLI or optional stdio MCP. `agents configure` creates an ignored installation-bound launcher; `agents doctor` checks it. Missing MCP never blocks a client or CLI. Codex/OpenCode use their own model/login settings, separate from the classifier.

MCP reads are bounded/masked. Mutations need a stable idempotency key (at least eight characters) and applicable `expected_version`. Secrets, PIN/TAN, sending, restore and deletion stay local. See [integration](AGENT_LLM_INTEGRATION.md).

SMTP is optional and configured locally. Remote hosts require STARTTLS or SSL; credentials use SecretStore:

```toml
[delivery]
enabled = true
host = "smtp.example.org"
port = 587
starttls = true
use_ssl = false
from_address = "sender@example.org"
[delivery.targets.personal]
recipients = ["owner@example.org"]
subject_prefix = "My finances"
```

```bash
poetry run dagoberd config set-secret smtp.username
poetry run dagoberd config set-secret smtp.password
poetry run dagoberd deliveries preview personal --report-run latest
poetry run dagoberd deliveries send <preview-id> --fingerprint <sha256> --idempotency-key report-example-2026 --yes
```

Previews expire after 30 minutes, retain full recipients locally but mask output, and bind each artifact's path/size/hash. Changes block sending. Repeated idempotency keys return stored results without another email. MCP may preview but cannot send.

## 10. Maintenance, backup, export and recovery

```bash
poetry run dagoberd database backup --output output/backups/example.sqlite3
poetry run dagoberd database restore output/backups/example.sqlite3 --yes
poetry run dagoberd database export --output output/ledger.json
poetry run dagoberd portability integrity
poetry run dagoberd portability export --output output/example.aex
poetry run dagoberd config set-secret backup.password
poetry run dagoberd portability backup --output output/backups/example.aebak
poetry run dagoberd portability verify output/backups/example.aebak --password-secret ACCOUNT_EVAL_BACKUP_PASSWORD
poetry run dagoberd audit --limit 100
```

`database` is ledger-only. Portable exports include database, documents, registered reports, non-secret config, product/schema version and SHA-256 manifest. `.aex` is unencrypted; `.aebak` wraps it in AES-256-GCM/Scrypt. Never pass passwords directly as arguments.

### Safe output creation

Prepare backups/exports at `.account-eval-write-*/payload` beside the destination. Unix files use `0600`, directories `0700`; inherited Windows ACLs remain authoritative. After writing, format/integrity checks and synchronization, an atomic hardlink creates the final name. Filesystems must support hardlinks. Existing targets are never overwritten, even under concurrency; use a new filename. CLI conflicts return `already_exists`, exit 1.

Handled failures leave no incomplete final output. Hard interruptions may leave private staging whose payload is not an approved backup. Cleanup failure after adoption can leave a valid output: verify it before retrying and remove only the exact associated staging directory. Existing backups are not automatically deleted.

### Safe restore

Stop GUI, MCP, syncs and all writers. Provide space for input, prepared contents and rollback copies. Restore is not a transaction across filesystems.

1. Copy/verify the input archive and use that unchanged copy. Check entries, duplicate paths, hashes, schema, SQLite/foreign keys, rebased references and every target before mutation. Reject overlaps, symlinks, internal metadata/config/database/backup conflicts and unmanaged files.
2. For an existing ledger, create a verified unencrypted rollback export at `backups/before-restore-<timestamp>-<id>.aex`. Fully prepare new contents and previous managed-file copies in restrictive `.account-eval-restore-*` directories beside targets. Record locations in `.account-eval/restore-recovery/restore-*/recovery.json` before adoption.
3. Adopt the database, atomically replace files on their own filesystems and remove surplus managed files. Handled adoption failures, failed final checks and `Ctrl+C` trigger rollback. Remove a newly created ledger if none existed before. Success/rollback cleans temporary files but retains the rollback export; unrelated unregistered reports remain untouched.

Interpret Windows drive/UNC and Unix source references independently of the current platform, mapping actual archive entries without opening original paths. Windows matching is case-insensitive/separator-aware; Unix is exact. Every nonempty reference must be absolute, inside its exported root and match one archive file. Missing/external/relative/ambiguous references fail before target changes. Metadata-only documents need no archive file. Early failures may leave empty directories or rollback exports, but not changed prior ledger/files.

### Recovery after restore failure

| Result | Exit | Meaning |
| --- | --- | --- |
| `restored=true` | 0 | Backup adopted; verify expected contents/integrity. |
| `restore_rolled_back` | 1 | Adoption failed; prior state restored. Resolve cause before retrying. |
| `restore_cleanup_required` | 1 | Adoption completed; preserve recovery material, verify and clean associated temporary files. |
| `restore_recovery_required` | 7 | Rollback failed/unresolved recovery. Stop writes and preserve material. |

Structured details include `restore_complete`, `rollback_complete`, `rollback_export`, `recovery_directory`, `rollback_failures` and `cleanup_failures`. Internal exceptions are not blindly emitted. Unresolved `restore-*` directories block another restore; no force bypass exists.

Hard kills/power loss can leave mixed state even without a CLI result. Preserve recovery directories and `before-restore-*.aex`; do not resume ordinary use or delete evidence:

1. Stop writers and separately back up the failed workspace, all journal locations, hidden staging and rollback export. They can span drives and contain sensitive paths/data; never attach them to Issues.
2. Prefer restoring the rollback export into a fresh separate workspace without private overrides/partner linkage. Set `RECOVERY_CONFIG` to its absolute config path and `ROLLBACK_EXPORT` to the specific saved archive:

   ```bash
   poetry run dagoberd --config "$RECOVERY_CONFIG" setup --yes
   poetry run dagoberd --config "$RECOVERY_CONFIG" portability verify "$ROLLBACK_EXPORT"
   poetry run dagoberd --config "$RECOVERY_CONFIG" portability restore "$ROLLBACK_EXPORT" --yes
   poetry run dagoberd --config "$RECOVERY_CONFIG" portability integrity
   ```

3. Compare transactions, documents, reports and paths with the saved state before deliberately repointing production configuration. Database-only restore does not recover files.
4. Without a rollback export, use the journal. `previous_database` names the old copy, restorable with `database restore ... --yes` in the stopped original workspace. Restore each `previous` to `destination` and verify `previous_sha256`. If partial rollback consumed a copy, first verify the destination hash. Entries without `previous` were new files: remove only those exact validated destinations. `previous_database=null` means no old ledger; remove new database/sidecars only after verifying this. Preserve state and stop if required copies are missing.
5. After complete verification, archive evidence and remove only associated staging and the resolved `restore-*` directory. Deleting a journal alone restores nothing.

### Controlled deletion

```bash
poetry run dagoberd portability delete-preview --scope documents --scope reports
poetry run dagoberd portability delete-run <preview-id> --fingerprint <sha256> --yes
```

Scopes are database/documents/reports/backups/exports. Preview binds absolute path, size and hash. Any change aborts before the first deletion. Restrictive receipts remain in `.account-eval/deletion-receipts/`; config and keyring secrets are not automatically deleted. Never substitute free paths or recursive shell deletion.

## 11. Troubleshooting

| Symptom | Action |
| --- | --- |
| Unsupported Poetry/pyenv shim | Select Python 3.12+/Poetry 2.2+, verify executables and sync. Keep tooling outside the project environment that sync may prune. |
| Schema/integrity error | Back up and check code/config versions; migrations are numbered/checksummed. |
| Experimental sync missing data | Prefer file export; API coverage is incomplete. Diagnose/recover the named connection locally only when appropriate. |
| Import errors | Fix row/field/code or mapping and create a new preview. |
| Wrong import account | Check provider kind, currency and internal ID with `accounts list`. |
| LLM unavailable | Check server, endpoint/model, secret and cloud opt-in. Deterministic stages still work. |
| Invalid/expired preview | Reread data and preview again; never replace a fingerprint manually. |
| Missing MCP | Run `agents configure/doctor` or use CLI directly. |
| Delivery blocked | Check activation, TLS, target, secret aliases and unchanged artifacts; preview again. |
| `version_conflict` | Reread/review the current entity before retrying. |
| Ambiguous document | Add metadata and review candidates explicitly. |
| Wrong report scope | Check accounts/group/enabled status and recorded metadata. |
| Missing transfer | Check scope, currency, opposite amounts, date limits; pending/split entries are excluded. |
| Reconciliation difference | Check source completeness, snapshot dates, status and bank cutoffs; never fabricate balancing entries. |
| Restore failure | Follow section 10; do not remove recovery locks or continue writing. |

Use the matching public GitHub issue form for bugs/security concerns, with sanitized diagnostics and synthetic examples only.

## 12. Architecture decisions

- **ADR-001 — Local ledger:** SQLite, integer minor units, immutable sources, numbered checksummed migrations.
- **ADR-002 — Provider ports:** File imports and experimental read-only providers share normalized transactions.
- **ADR-003 — Agent boundary:** Shared services, bounded/masked reads, idempotency/versions; authentication outside stdio.
- **ADR-004 — Model opt-in:** Deterministic operation without models; redacted structured inputs and reviewable suggestions.
- **ADR-005 — Local documents:** Content-hashed originals outside SQLite, heuristic extraction and candidate-gap matching.
- **ADR-006 — Scope:** `AccountScope` throughout; all means all enabled accounts.
- **ADR-007 — Semantics:** Booking state, source revisions and versioned relations are separate; cashflow and reconciliation have distinct sum rules.
- **ADR-008 — Organization:** Primary categories, multiple tags, unique dimension values, normalized merchants and atomically version-bound bulk previews.
- **ADR-009 — Analysis:** `account-eval-analysis-v1`, canonical integer results, dated rates and balance snapshots; incomplete totals remain visible.
- **ADR-010 — Imports:** Full inspection before mutation; hash/mapper/error-free preview bind execution.
- **ADR-011 — Ownership:** Open manifest/checksums, authenticated encryption, verified prepared restore/rollback and separate previewed deletion.
- **ADR-012 — External actions:** Expiring exact payload/artifact previews; changed data invalidates execution. Sending remains local.
- **ADR-013 — Agents:** Complete CLI, optional ignored installation-bound launchers.
- **ADR-014 — Language:** English project with central English/German/Spanish GUI catalogs and locale-aware display; stable technical contracts/user data.

## 13. Tests and release checks

```bash
poetry sync
poetry run pre-commit run --all-files
poetry run python -m pytest
poetry check
poetry build
```

Pre-commit runs Ruff/formatting/Mypy and file hygiene. On Windows, native symbolic-link tests require Developer Mode or a suitably privileged runner; do not exclude them to claim complete acceptance. Install Chromium and set `ACCOUNT_EVAL_BROWSER_TESTS=1` for the separate GUI suite. Standard tests skip only the deliberately opt-in browser tests. Use temporary synthetic workspaces without private overrides, credentials or production files.

[GitHub Actions](GITHUB_ACTIONS.md) describes Quality, staged/full Release validation, Release security and explicit publishing. A normal main push does not establish the full platform/browser matrix. Match results to the candidate SHA and test installed wheel/sdist without source/partner linkage. Verify issue forms, catalogs, stable contracts and German import fixtures.

## 14. Manual release acceptance

Record date, version/commit, OS and sanitized result per applicable item. Never record PINs, TANs, tokens, account IDs, recipients, transaction text or report contents. Test optional integrations only when enabled for the candidate; document inapplicable steps.

- [ ] **Installation:** Sync/install hook, repeated setup preservation, invalid configuration unchanged, config validation, doctor, contracts, self-test/demo and isolated wheel/sdist.
- [ ] **Secrets/model:** Masked config/keyring, deliberately selected endpoints and deterministic operation with LLM disabled.
- [ ] **Preferred intake:** Controlled CSV/XLS/XLSX preview/run, mapping/scope/totals/errors and duplicate-free repetition. CAMT/WISO mappings where applicable; malicious/oversized inputs rejected.
- [ ] **Experimental Comdirect, if enabled:** Narrow known account/period, local CLI/GUI authentication, repeatability, statuses, snapshots and sanitized diagnosis/recovery. Record actual/missing coverage; savings/Visa completeness and other bank APIs are not promised. File-import acceptance does not depend on complete live API coverage.
- [ ] **Documents/classification:** Non-sensitive candidates, versioned accept/reject/unmatch, rules/history/hints, model previews/fingerprint rejection, suggestion review and exact splits without double counting.
- [ ] **Reports/delivery:** Matching scoped JSON/XLSX/HTML and generic profiles. Optional controlled SMTP test: changed artifacts rejected and idempotent repetition sends once.
- [ ] **Organization:** Manual entries, transitions/relations/reconciliation differences, tags/dimensions/merchants/search/inbox and atomic abort on changed bulk targets.
- [ ] **Analyses/portability:** Fixed/relative analyses, rate provenance, null incomplete totals and stale balances. Verify exports/backups. Restore/recovery and deletion checks only on synthetic separate workspaces.
- [ ] **GUI/agents:** Three languages, English first use, saved choice, live switching with drafts/filters/selections/dialogs, amounts/dates/errors and CLI/MCP/skill parity. Missing optional launcher never blocks CLI.
- [ ] **Technical:** Local gates/Chromium, Quality, full platform validation and Release security at the candidate. Inspect source/packages/demo/logs for private contents and test affected private extensions separately.
- [ ] **Final separate publication:** Only after acceptance, clean historical data/commit metadata or prepare independently reviewed new history. Remove internal review material; verify public issue forms, available scanning/protection and required `quality`. Revalidate the clean candidate and explicitly publish tag/assets/SBOM/provenance.

Public beta requires all applicable steps without critical errors and a documented file-import smoke. History cleanup and publication remain the final separate step.

## Multiple user circles and partner repositories

Use the [multiple-workspace procedure](MULTIPLE_WORKSPACES.md) to create ordinary local folders, register existing data, and optionally add Git or independent snapshot-folder exchange later. Git, GitHub and a remote account are not prerequisites for local file-based workflows. `cloud.root` derives `<root>/workspaces/<workspace-id>/documents` and `reports`; `cloud.workspace` retains its legacy meaning and cannot be combined with `cloud.root`. Never copy an active SQLite database through a cloud synchronization service.
