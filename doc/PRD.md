# PRD: Dagoberd local finance ledger

**Status:** Public-beta candidate `0.3.0b1`. Release gates and local acceptance remain required; results belong in the acceptance record.

## 1. Product goal

Dagoberd is a local, agent-first finance ledger for individuals managing household, rental and self-employment finances. It preserves source records, normalizes transactions, matches documents and produces explainable classifications and reproducible reports. Every transaction belongs to exactly one account and provider. It performs no payments and provides no financial, investment or tax advice.

File imports from CSV, XLS/XLSX and CAMT.053 are the preferred data source. The read-only Comdirect API is experimental, with incomplete product coverage. Savings-account coverage is not verified; Visa credit-card transactions are unavailable. Other bank APIs are not currently offered. Broader API support is future work, not a promise of this beta.

The user starts and configures a coding agent separately. It operates Dagoberd through the repository skill, CLI or optional local MCP. Dagoberd neither launches the agent nor controls its model, login or data transfers. The optional OpenAI-compatible classifier has separate configuration and no research or agent tools. See [Agent and LLM integration](AGENT_LLM_INTEGRATION.md).

## 2. Release scope

| Release | Requirements | Scope |
| --- | --- | --- |
| V1 | REQ-01 through REQ-15, including REQ-09a/09b | Ledger, file imports, experimental read-only provider, matching, classification, splits, reports, CLI/MCP and privacy |
| V2-P0 | REQ-16 through REQ-28 | Delivery, organization, analyses, recovery, portability, deletion, onboarding, GUI and beta governance |

V2-P0 includes V1. One user and multiple isolated local workspaces are supported, with no mandatory account or cloud service. Germany and Spain supply supported import examples; this does not guarantee every bank's export layout. Payments, portfolio holdings/tax lots, multiple users, budgets, recurring contract management and OFX/QIF/MT940 are outside this release.

The public core supplies generic reports and explicit versioned extensions. Personal datasets, mappings, rules, skills and reports belong outside it. The core needs no private partner repository or personal model provider.

Project text is English. The GUI additionally supports German and Spanish through central catalogs. First use is English; the user's selection is stored locally. Technical identifiers, external import formats and user-authored data are language-independent and remain unchanged.

## 3. Requirements and acceptance criteria

The following capabilities are implemented; this table describes product behavior, not proof that the current candidate has passed release acceptance.

| ID | Requirement and acceptance criterion |
| --- | --- |
| REQ-01 | A local migration-capable ledger models providers, accounts, raw sources, normalized transactions, categories, rules, hints, documents, matches, classification and audit. Legacy transactions migrate without loss; amounts use integer minor units and each transaction has one account. |
| REQ-02 | Source data is immutable. Stable source identities and idempotency keys prevent duplicate transactions on repeated import. |
| REQ-03 | CSV and XLSX normalize dates, amounts, currencies, account identities, purpose and references through the same `NormalizedTransaction` contract. |
| REQ-04 | Read-only providers are replaceable adapters. Experimental Comdirect implements OAuth2, 2FA, sessions and paging without remote writes; coverage limitations remain explicit. |
| REQ-05 | Documents are stored locally by content hash. Heuristic extraction and amount/date/merchant/reference scores explain candidates. Automatic matching requires both a minimum score and a gap to the runner-up; ambiguous matches require review. |
| REQ-06 | Hierarchical categories and local profiles are extensible. Rental, Spanish tax, freelance and household profiles, categories, rules and hints are available through audited CLI operations. |
| REQ-07 | Classification uses rules, confirmed history, user hints and optional LLM in that order. Deterministic stages may apply decisions; model output remains suggested. Cloud execution requires an expiring persisted preview of the exact redacted payload, provider, model, endpoint and request configuration, bound by fingerprint. Record model, prompt and taxonomy versions. |
| REQ-08 | Splits must sum exactly to their parent. Reports use active splits instead of the parent and never count both. |
| REQ-09 | Local JSON/XLSX/HTML reports share transaction data and category/month/account/currency cashflow totals. Built-in profiles use generic reports; successful runs and verified artifacts are registered. |
| REQ-09a | Every report supports one account, an explicit selection, a group or all enabled accounts. The chosen scope is visible in headers and machine-readable metadata. |
| REQ-09b | Personal reporting uses a versioned extension API. CLI/MCP select configured names only. API v1 passes copied scoped data, metadata, formats and an isolated output directory; optional history retains account/profile/currency boundaries. Register only complete validated artifacts. |
| REQ-10 | CLI operations cover imports, experimental sync, profiles, rules, hints, classification, documents, matching and reports without requiring the GUI. |
| REQ-11 | Optional local stdio MCP provides bounded, filtered, masked reads and audited version-protected mutations. PINs, TANs and secrets are never tool arguments. |
| REQ-12 | Codex, OpenCode, Cursor and Claude Code share the repository skill and MCP services. Harness-specific guidance and launch configuration contain no duplicate business logic. |
| REQ-13 | Local secrets prefer the OS keyring, with environment and ignored `.env` fallbacks where supported. Comdirect credentials use the keyring exclusively. PIN/TAN input is interactive and never persisted. |
| REQ-14 | Ledger mutations write audit events. MCP mutations require persistent idempotency keys and applicable `expected_version` checks. |
| REQ-15 | Unit, migration, provider, import, split, LLM, matching, MCP and report tests run through Poetry and Quality. Real bank/model/SMTP operations remain deliberate local acceptance steps. |
| REQ-16 | A provider-neutral service layer processes completed report artifacts without recalculating data. SMTP uses an expiring masked preview, artifact hashes, local confirmation and idempotency. MCP cannot send. Further adapters use the same service boundary. |
| REQ-17 | Manual account types/transactions, multiple tags, dimensions, normalized merchants and combined search are available through CLI/MCP. Search combines account/date/amount/currency/text/status/category/source/tag/dimension/merchant/document/review filters. Writes retain audit, idempotency and versions. |
| REQ-18 | A persistent inbox covers new, pending, ambiguous, duplicate and failed items. Bulk previews cover at most 500 transactions. Any changed version aborts before all writes; repeated completed runs are idempotent and individual changes share audit correlation. |
| REQ-19 | Versioned analyses store scope, fixed/relative period, profile, organization filters, base currency, analysis types and formats. Runs retain the definition snapshot/version, resolved period, data fingerprint, rates, result hash and paths. Every renderer uses the same result. |
| REQ-20 | Cashflow, income/expenses, fixed costs, trends and net worth share canonical semantics. Group by category/month/account/merchant/tag or `dimension:<code>`. Rates carry currencies/date/source/version. Missing rates leave separate currencies and a null incomplete total; net worth uses snapshots and exposes missing/stale balances. |
| REQ-21 | CAMT.053, CSV, XLS and XLSX share structured previews and configurable mapping/encoding/delimiter/sheet/date/decimal options. Extra columns can provide annotations. File hashes and mapper versions bind execution; any error blocks it. Reject DTD/entities and oversized CAMT. Unknown annotations produce structured warnings. |
| REQ-22 | Experimental Comdirect diagnostics show sanitized secret references, session state, failed syncs and next actions. Local recovery reauthenticates one connection and can resume a stored cursor; no PIN/TAN passes through MCP. |
| REQ-23 | Portable export includes ledger, documents, registered reports, non-secret configuration and a versioned SHA-256 manifest. AES-256-GCM/Scrypt backups use a local secret. Prepare and verify outputs before atomic non-overwriting creation. Restore validates platform-independent source references and all targets, prepares new/previous contents, creates rollback backup and restores prior state on handled failures. Failed rollback retains recovery material and blocks further restores. |
| REQ-24 | Deletion requires exact unchanged preview paths, fingerprint and local `--yes`. Scopes cover database/documents/reports/backups/exports. Changed content aborts before deletion; a restrictive receipt remains outside the deleted data. MCP may preview but cannot execute deletion. |
| REQ-25 | Setup, synthetic demo, offline self-test, completion and installation-bound `agents configure/doctor` support a new user without personal data. Self-test starts a real local stdio MCP and checks portability. Missing MCP never blocks CLI operation. |
| REQ-26 | Apache-2.0, security/privacy/support/threat documentation, public issue forms, dependency/secrets checks and SBOM/provenance workflows support the beta. Quality runs without path filters. Repository settings and Linux/macOS/Windows artifact acceptance require real verification. Security reports use public Issues. |
| REQ-27 | A token-protected loopback GUI supplies scoped overview, incremental 200-row transaction pages, keyboard editing, documents, imports and experimental sync through the shared services. English/German/Spanish catalogs update the existing view without losing drafts, selections or filters. Incomplete financial totals remain visible. |
| REQ-28 | Account-specific rule/history/hint/LLM stages are visible, configurable and explainable. Explain matches execution; LLM results remain suggestions. |

## 4. Functional rules

Import/provider adapters deliver normalized transactions and raw source identities. The ledger assigns one account, stores immutable provenance and applies idempotency. Profiles add classification metadata; each decision records source, confidence, rationale and review state. Documents expose explainable candidates. Reports select scope first and then apply split and cashflow rules.

Report extensions are explicitly trusted local Python code running with process privileges, not a sandbox. They receive copies rather than a ledger service. Full history is a separate local opt-in, and reporting never authorizes delivery. Machine paths and extension choices belong in ignored local settings.

The public CLI contract is `account-eval-cli-v3`. Use `classifications set|apply-rules|deterministic`, `profiles create`, `rules add`, `hints add`, `documents register|candidates` and `import preview|run`. Removed top-level commands have no aliases. Deterministic classification never calls a model; document registration records metadata only.

WISO category/tax tree imports need no free-field mapping. Each supplied free-field tree needs complete explicit TOML mapping through `imports.wiso_trees.mapping_file`. Mapper version, normalized mapping, source field identities and hashes bind the preview; missing mappings and collisions fail before writes.

External services process only artifacts of completed report runs. Activation, destination and idempotency are explicit; results identify service, run, masked target, artifacts, timestamp and status. Failures are visible without removing the local report.

## 5. Acceptance journey

1. Install and run setup/self-test. Repeated setup changes only managed connection/model/base-currency values; it preserves data paths and other local settings. Invalid configuration is neither replaced nor used to initialize a database.
2. Configure optional adapters and secrets locally. A coding agent's own model does not enable the classifier. Local model endpoints must be loopback; remote targets require cloud mode and opt-in.
3. Register a file account and import a controlled bank export through preview/run. Show scope, period, totals, errors and duplicates before execution. Repeating the import creates no duplicates.
4. Optionally exercise experimental Comdirect on a narrow verified scope. Authenticate in the local CLI or GUI; disclose no PIN/TAN to agents. Document missing products and incomplete coverage rather than claiming full bank support.
5. Import documents and review ambiguous matches with current versions. Apply deterministic classification before optional model suggestions. Approve the exact cloud preview and review model results explicitly.
6. Add manual accounts/transactions only when requested. Organize through tags/dimensions/merchants, review the inbox and use a version-bound preview for bulk changes.
7. Save scoped analyses and produce consistent JSON/XLSX/HTML. Explain missing rates/balances rather than estimating totals. Review classifications and report results before relying on them.
8. Optionally preview delivery; send a non-sensitive test report only with local confirmation and an idempotency key.
9. Verify exports/backups and restore only into a separate test workspace. Test recovery and fingerprint-bound deletion on synthetic data.
10. Exercise all three GUI languages, editing and draft retention. Verify agent/CLI parity and audit trails. Record all applicable release gates before history cleanup and publication.

## 6. Deliberately local acceptance

Credentials, model endpoints and SMTP destinations are local choices and never repository content. Keyring, optional model, experimental bank and optional delivery checks require deliberate local execution. CI uses synthetic data and fake providers only. The PDF extractor is heuristic; missing metadata remains reviewable. See [Runbook release acceptance](RUNBOOK.md#14-manual-release-acceptance) for the operational checklist.

## Isolated workspaces

A single user can maintain independent user circles through registered workspaces. Each workspace owns its accounts, ledger, documents, classifications, rules, analyses, reports, secrets and bank sessions. Multiple workspaces require explicit selection in CLI/MCP and a per-tab GUI selection. There are no cross-workspace reports or automatic Git synchronization.

A provided local or cloned repository can be initialized without overwriting existing configuration or data. Stable repository UUIDs define cloud namespaces and credential scopes. Storage overlap, foreign ledger identities and foreign previews must fail before financial changes. Git commits and pushes require a confirmed private origin, selected files, an unchanged preview and local confirmation. See [workspace behavior and operations](MULTIPLE_WORKSPACES.md).
