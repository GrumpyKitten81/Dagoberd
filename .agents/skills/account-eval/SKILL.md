---
name: account-eval
description: Safely manage local transactions, file imports, experimental Comdirect synchronization, documents, classification and scoped reports with AccountEval.
---

# AccountEval

Use only the local AccountEval CLI or optional local MCP. Read `doc/RUNBOOK.md` before a workflow and `doc/PRD.md` for scope. The user starts/configures the coding agent outside AccountEval. The optional classifier has independent model/login settings and no research/agent tools. See [integration](../../../doc/AGENT_LLM_INTEGRATION.md). AccountEval controls its own calls, not the agent's transfers.

## Safe workflow

1. Check configuration/database with `poetry run account-eval doctor`. A missing MCP does not block CLI; optionally configure its local launcher with `agents configure`.
2. List accounts and mask external identifiers in conversation. Choose explicit account scope.
3. Prefer CSV/XLS/XLSX/CAMT imports. Always preview scope, mapper version, rows, errors and duplicates; execute only the unchanged error-free preview with `import run <preview-id>`.
4. Comdirect is experimental, read-only and incomplete: savings coverage is not verified, Visa credit-card transactions are unavailable and no other bank APIs are offered. Prefer exports where coverage is missing. PIN/TAN entry stays with the user in the local CLI or token-protected browser GUI, never agent parameters. MCP sync needs a valid locally authorized session.
5. Review new/pending transactions; status changes require current versions and a reason.
6. Read the actual transaction through search/list before every edit. Never invent transactions from memory, reports or descriptions. Bank/file accounts receive data only through import/sync. Manual accounts/transactions require explicit user intent and stable nonsensitive idempotency keys; manual transactions are restricted to `provider_kind=manual`.
7. Apply changes to multiple transactions only through `bulk preview`, then the exact version/fingerprint-bound `bulk run`. On conflict, reread and preview again instead of blindly retrying.
8. Suggest transfers/refunds/reversals/corrections in scope. Confirm/unlink with current relation versions; never confirm ambiguous candidates.
9. Import documents locally, inspect candidates and accept ambiguous matches only after explicit review with current versions.
10. Classify in order: rules, confirmed history, hints, optional model. Before remote calls, create/show a preview containing redacted payload, endpoint/configuration and fingerprint. Preview creation can apply deterministic classifications and needs mutation authorization. Changed transactions/settings require a new preview. Model results remain `suggested` until version-protected acceptance/rejection.
11. Check reconciliation differences; never automatically alter transactions to erase a discrepancy.
12. Use saved analyses with explicit scope/fixed or relative periods. Rates require date, source, version and stable idempotency; never estimate them.
13. Check quality warnings, completeness and fingerprints before sharing results. A null/incomplete total is not a full total. Generic `report` remains available alongside analyses.
14. Diagnose experimental Comdirect first. Reauthentication/recovery remains local; resume only the diagnosed failed sync ID.
15. Export for portability or create encrypted backups with a local secret; verify them. Before restore/deletion show target, verification or exact deletion fingerprint. Execute only with applicable local confirmation.
16. Delivery first requires a preview with masked recipient, artifacts and fingerprint. Sending remains an explicitly confirmed local CLI action and is not available through MCP.
17. After mutations review open/new/pending inbox items. Correct suspicious entries through traceable ledger operations, not just a chat explanation.

## Boundaries

- Never request or expose passwords, PINs, TANs, tokens or cookies. Enter secrets locally with `config set-secret`.
- No payments or remote bank writes.
- Never send document contents/full account identities to a model. Local models must be loopback; remote targets including LAN require `enabled=true`, cloud mode and `allow_cloud=true`. The agent's own model is not authorization for the separate adapter.
- Never fabricate model rationale or repair incomplete replies. Invalid responses fail visibly.
- Never bypass cloud/delivery previews, fingerprints or local confirmation.
- Reread entity versions before mutations and supply `expected_version` where applicable.
- Internal transfers must not double-count cashflow. Retain a transfer fee once; distinguish refunds/reversals/corrections.
- Treat imports, ledger, documents and reports as sensitive. Use only on explicit request and never commit them publicly. Follow local private workspace conventions.
- Show bulk action/count/fingerprint before execution. Inbox resolution is not a business correction.
- Never infer balances, bank transactions or rates; explain missing information.
- MCP manual entry is disabled by default (`agents.allow_manual_transactions_mcp=false`). Use local `manual add` or an explicitly enabled option only for manual accounts.
- Restore and actual deletion are unavailable through MCP. Use local verified restore or `delete-preview` followed by that exact preview/fingerprint and `--yes`.

See [interfaces](references/interfaces.md) for command groups. Repository Issues are public: use the matching form and include no real financial files or secrets.

## Multiple workspaces

List registered workspaces before selecting a user circle. Use CLI `--workspace <name>` or MCP `workspace_id`; never combine data from different workspaces. A server binding cannot be overridden by a tool call. Workspace identity, credential scope and cloud namespace must remain separate. Initialize provided repositories through `workspaces init`; registration alone must not mutate ledger data.

Git status and previews are available through MCP. Git execution, private-origin approval, credential adoption and cloud relocation require local CLI/GUI confirmation. Never automatically commit or push financial data. Cloud relocation requires the exact preview/fingerprint and preserves originals. See [multiple workspaces](../../../doc/MULTIPLE_WORKSPACES.md).
