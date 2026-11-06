# Agent-guided account imports

This journey uses a user-started coding agent and the local CLI or optional MCP. The
separately configured optional classifier is not needed; it has no agent or research
tools. See [agent integration](AGENT_LLM_INTEGRATION.md). Work only with an export
the user authorized for local inspection. Never send credentials, full account
identifiers or transaction contents to a remote service without applicable consent.

## First account

1. Ask the user to export CSV, XLS, XLSX or CAMT.053 locally and identify the intended
   workspace and account. List workspaces and accounts in that scope. Mask account
   identifiers in conversation. If the export contains multiple or unclear identities,
   stop and resolve the routing with the user. For tabular data, select a specific
   registered account; do not use the generic `file-import` identity for onboarding.
2. Inspect the authorized file locally. Identify the format, sheet/header, mapping,
   account, currency, date range and stable transaction identifier. Ask the user about
   missing identity, currency or ambiguous fields; do not guess. Unsupported formats
   need a new export or an explicitly reviewed mapping, not an improvised import.
3. Propose account registration and a profile in the private workspace configuration.
   Obtain authorization before registration or configuration changes. Give the profile
   an account-specific name, document export instructions and expected layout in a
   private workspace note, and keep credentials and personal scripts out of the core.
   A private folder is sufficient; Git is optional and is never committed automatically.
4. Obtain authorization for a preview: it persists a preview and may create review
   items. Use the selected workspace on every command. Explain account actions, mapper
   version, period, totals, currency, errors, duplicates and coverage. Resolve errors,
   identity or currency warnings and obtain explicit confirmation of a missing source
   account identity before proceeding. Check scoped history only when authorized.
5. After explicit approval, run the exact error-free preview. A changed file or mapping
   requires a new preview and review. Explain imported, skipped and updated counts and
   inspect the review inbox. Never import real data during a demonstration or test.

## Synthetic demonstration

Create a disposable workspace and registry with `workspaces create`; use the global
`--workspace-registry` option to keep the demonstration isolated. The following data is
synthetic. Run these commands in a checkout with `poetry run` before each `dagoberd`.

```text
dagoberd --workspace-registry ./demo-registry.toml workspaces create demo ./demo-workspace
dagoberd --workspace-registry ./demo-registry.toml --workspace demo accounts register file SyntheticBank SYN-A "Synthetic account" --currency EUR
dagoberd --workspace-registry ./demo-registry.toml --workspace demo accounts list
```

In `demo-workspace/dagoberd.toml`, add this synthetic profile:

```toml
[imports.profiles.synthetic_account]
source = "tabular"
delimiter = ";"
default_currency = "EUR"
[imports.profiles.synthetic_account.mapping]
external_id = ["ID"]
booking_date = ["Date"]
amount = ["Amount"]
purpose = ["Purpose"]
account = ["Account"]
```

Create `demo-workspace/export.csv` with:

```csv
ID;Date;Amount;Purpose;Account
one;2026-01-01;10.00;Synthetic income;SYN-A
```

Replace `<account-id>` with the ID returned by `accounts list`. Review and approve
the preview, then replace `<preview-id>` with its returned ID:

```text
dagoberd --workspace-registry ./demo-registry.toml --workspace demo import preview --source tabular --account <account-id> --profile synthetic_account ./demo-workspace/export.csv
dagoberd --workspace-registry ./demo-registry.toml --workspace demo import run <preview-id>
dagoberd --workspace-registry ./demo-registry.toml --workspace demo import history --account <account-id>
```

Append `two;2026-01-02;-2.00;Synthetic purchase;SYN-A` to the export and repeat
preview and run. The overlapping first row is a duplicate and the second is new.
Repeating the same file with a fresh preview skips both. History counts and dates for a
multi-account import describe the entire import, not just the selected account.

Try changing `SYN-A` to `SYN-B`: the preview reports an account mismatch and cannot
run. Remove the account column: obtain explicit confirmation of the selected account.
Rename `Amount`: the preview reports missing fields; update the private profile only
after reviewing the new layout, then make a new preview. An unsupported file suffix
reports an error. Review a currency mismatch before any execution.

## Later updates and limitations

Find the intended workspace, account, private note and profile; request a fresh manual
export and inspect its identity and layout again. Compare the authorized scoped history
and preview, including overlapping dates and coverage gaps, then obtain approval for
the exact update. Do not change a mapping merely to suppress errors. Stable unique bank
transaction IDs are preferred. References and content hashes can collide for distinct
identical transactions or change when exports change; repeated identities require user
review and cannot prove either a duplicate or complete coverage. Never silently merge
accounts or repair data based on guesses.

Comdirect sync is experimental, read-only and incomplete. When applicable, the user
performs local authentication and approvals; PINs, TANs, tokens and passwords never
become agent tool parameters. No payments, remote bank writes or unattended login are
provided. MCP offers `list_account_import_history`, `preview_account_import` and
`import_account_file`; its mutating tools require an idempotency key. Use a new key
for each newly requested preview and reuse a key only to retry that exact request.
