# AccountEval interfaces

The calling agent and optional classifier have independent models/credentials. AccountEval launches no agent; its classifier has no research tools. See [integration](../../../../doc/AGENT_LLM_INTEGRATION.md).

## CLI

- Setup/diagnosis: `setup --yes`, `self-test`, `agents configure|doctor`; MCP is optional.
- Preferred intake: `import preview --source tabular --account <id> <file>` then `import run <preview-id>`. CAMT uses `--source camt053`; profiles use `--profile <name>`.
- Experimental Comdirect: local authentication, `sync comdirect`, `comdirect diagnose|recover`. Incomplete coverage; use file imports when unsupported.
- Documents: `documents import|candidates|accept|reject|unmatch`.
- Classification: `classifications preview`, exact `run --preview ... --fingerprint ...`, then version-protected review/accept/reject. Preview can mutate deterministic results and binds endpoint/configuration. Remote endpoints need cloud mode/opt-in; local endpoints must be loopback. `deterministic` never calls a model. Agent-owned CLI suggestions use `set ... --suggest --version ...` as manual; MCP `classify_transaction` applies directly.
- Splits: `splits create|revert`; statuses: `transactions --status`, `transaction-history`, `transaction-status`.
- Relations: `relations suggest|list|create|confirm|reject|resolve`.
- Reconciliation: `balances add|list|reconcile|reconciliations`.
- Manual: `accounts create-manual`, `manual add` with stable `--idempotency-key`; manual accounts only, MCP disabled by default.
- Organization: `tags create|list|add|remove`, `dimensions create|value-add|list|set|clear`, `merchants create|alias-add|list|set`.
- Search/inbox: combined `search` filters and `review list|resolve|dismiss`.
- Bulk: `bulk preview <action> <transaction-ids>` then that exact `bulk run <preview-id>`.
- Rates: `rates add <source-currency> <target-currency> <rate> --date ... --source ... --source-version ... --idempotency-key ...`.
- Analyses: `analyses create|list|show|update|run`; canonical cashflow/fixed-cost/trend/net-worth results and quality warnings.
- Reports: `report --profile <profile> --accounts <ids> --from YYYY-MM-DD --to YYYY-MM-DD`.
- Delivery: `deliveries list|preview|send`; send requires fingerprint, idempotency and local `--yes`.
- Portability: `portability integrity|export|backup|verify|restore|delete-preview|delete-run`; restore/deletion remain expressly confirmed local operations.

## MCP

The optional stdio server uses the installation-bound launcher from `agents configure`. It covers bounded/masked reads, accounts/transactions/status history, manual entry when enabled, search/organization/inbox/bulk, analyses/rates, relations/balances/reconciliation, structured imports, experimental Comdirect diagnosis/sync, documents, classification/previews, rules/hints, splits/profiles/reports, delivery previews and portability/export/backup/verification/integrity/deletion previews. Secret/PIN/TAN arguments, actual sending, restore and deletion execution are unavailable.

## Workspace management

Use `workspaces list|add|create|init|clone|remove|doctor`, then select `--workspace <name>` for every ledger workflow. `--workspace-registry <file>` selects an alternate local registry. Cloud relocation uses `workspaces cloud <name> preview|run`; Git uses `workspaces git <name> status|trust|preview|run`. Both execution flows require the exact fingerprint and local confirmation. MCP supports `list_workspaces`, workspace-scoped domain tools and Git status/previews; it cannot execute Git or cloud relocation.
