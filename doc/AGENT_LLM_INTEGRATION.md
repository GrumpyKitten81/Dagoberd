# Coding agents and optional LLM classification

Dagoberd `0.3.0b1` is operated by a user-started coding agent through the repository skill, CLI or optional local MCP. Its optional OpenAI-compatible classifier is a separate model role with separate configuration.

## Roles and capabilities

| Component | Responsibility |
| --- | --- |
| Calling agent | Understand the user request, load the skill, choose scope, invoke tools, explain results and optionally research with its own tools. Model/login settings belong outside Dagoberd. |
| Dagoberd | Manage transactions, apply rules/history/hints, create previews, check versions and audit changes. |
| Optional classifier | Evaluate prepared redacted transactions against allowed categories. Endpoint/model/secret are configured separately. |
| Review | Explicitly accept/reject suggestions with current transaction versions; confidence alone never applies a model suggestion. |

Dagoberd launches no agent CLI and inherits no login, API key or model choice. The classifier is disabled by default; deterministic workflows need no model. It is an outgoing client for `chat.completions.create`, not a general proxy or OpenAI server. `llm test` uses `models.list`. The adapter has no research, file, banking or agent tools and no autonomous workflow control.

## CLI workflow

Repository coding standards live under [`.agents/guidance/`](../.agents/guidance/). Global agent discipline (for example Claude `CLAUDE.md` in the user home) is not duplicated in the repository.

Load the [skill](../.agents/skills/dagoberd/SKILL.md), check configuration/scope and act on explicit user instructions. Secrets and PIN/TAN remain local: terminal entry for the CLI or the token-protected local browser GUI, never agent tool arguments. File import is preferred; Comdirect is experimental and incomplete. In a checkout prefix commands with `poetry run`; global `--config <file>` precedes the command.

1. `classifications deterministic <transaction-id> <profile>` checks rules, confirmed history and patterned hints without calling a model.
2. `classifications preview <profile> --transaction <transaction-id>` persists redacted inputs, endpoint/model/configuration, fingerprint and expiry.
3. Show the preview and obtain applicable authorization. Preview creation itself can apply deterministic classifications and is not read-only.
4. `classifications run <profile> --preview <preview-id> --fingerprint <sha256>` calls the separately configured classifier. Valid responses initially remain `suggested`.
5. Read `classifications review --profile <profile> --status suggested`; reread current versions before `accept` or `reject`.

Direct `classifications run <profile> --transaction <transaction-id>` is allowed for local models and deterministic operation when disabled. Remote models require the stored preview. The agent must not enable a classifier merely because its own model is available.

## MCP and GUI

MCP uses the same services:

| Step | Tool |
| --- | --- |
| Local/deterministic run | `run_classification`: profile, optional transaction/account IDs and limit |
| Preview | `preview_llm_classification`: profile, optional transaction/account IDs and limit |
| Execute | `execute_llm_classification_preview`: preview ID and fingerprint |
| Read suggestions | `list_classification_suggestions`: optional profile/limit |
| Review | `review_classification`: classification ID, accept/reject and current transaction `expected_version` |

Mutations additionally require a stable `idempotency_key` of at least eight characters. Keep the key for retries of the same action; use a new key for a different action. Preview creation/review are included.

GUI routes `/api/classification/llm/preview`, `/api/classification/llm/<preview-id>/run` and `/api/classifications/<id>/review` use the same checks. They require the GUI token and are not a public model API.

## Separate configuration

Configure the classifier in ignored `account-eval.local.toml`. Personal agent providers/templates belong in local settings or a private partner repository and are not automatically installed.

```toml
[llm]
enabled = true
mode = "local"
provider = "openai-compatible"
base_url = "http://127.0.0.1:11434/v1"
model = "local-model"
allow_cloud = false
```

Local endpoints must be IPv4/IPv6 loopback or `localhost` resolving exclusively to loopback. LAN servers are remote and need cloud mode, opt-in and classification previews:

```toml
[llm]
enabled = true
mode = "cloud"
provider = "openai-compatible"
base_url = "https://llm.example.invalid/v1"
model = "provider-model"
allow_cloud = true
api_key_secret = "ACCOUNT_EVAL_LLM_API_KEY"
```

Store the key locally with `config set-secret llm.api-key`. `llm show` displays configuration; `llm test` checks model listing without sending transactions. Endpoint validation/opt-in apply to testing but require no transaction preview. Requests do not follow redirects; local requests ignore environment proxies. Enabled endpoints reject embedded credentials, queries and fragments.

## Preview contract and model responses

`account-eval-llm-preview-v2` binds profile, redacted payloads, transaction versions and a non-secret configuration snapshot. It includes endpoint/request parameters, privacy settings, prompt version and secret reference names, never secret values. Changes require a new preview; normal expiry is 30 minutes. Upgrade invalidates legacy unbound open previews and preserves history/audit. Failed/partially processed runs are not automatically resent; existing suggestions remain reviewable.

Requests contain date/amount/currency, redacted purpose/counterparty/reference, profile, allowed categories, hints, taxonomy and prompt/model parameters, never documents or bank secrets. A complete JSON object or complete JSON code block is required:

```json
{"category": "expense.business", "confidence": 0.7, "rationale": "A justified suggestion"}
```

Category must be allowed or null. Confidence must be 0–1 and rationale nonempty, at most 1,000 characters. Extra/missing fields, alternative names and truncated responses fail visibly without producing a suggestion. Retry transport/temporary server errors only within configured limits. Suggestions retain source, confidence, rationale, provider/model, prompt hash, taxonomy version and applicable preview ID.

## Privacy and agent-owned suggestions

Redaction removes recognized IBANs, long digit sequences and email addresses and limits text. It is not complete anonymization: names, amounts and other information can remain sensitive. Authorized transfers follow provider terms. Dagoberd's gates govern its own calls; the calling agent's own file/output transfers depend on its settings and user authorization.

A dedicated versioned interface for researched agent suggestions is not implemented. `classifications set ... --suggest --version ...` stores a manual suggestion with CLI rationale; MCP `classify_transaction` directly sets a category with versions. These are not a separate researched-suggestion API. Use existing classifier metadata/review where applicable.

## Workspace selection

Use `workspaces list` or MCP `list_workspaces`, then select the intended user circle before accessing accounts. CLI accepts `--workspace <name>`; MCP tools accept `workspace_id` or a server binding through `ACCOUNT_EVAL_WORKSPACE`. `ACCOUNT_EVAL_WORKSPACE_REGISTRY` selects an alternate local registry. A tool selector cannot override a bound server. Agent access does not authorize cross-workspace data combination or Git execution. MCP exposes Git status/previews only; transfers, credential adoption and cloud relocation require local CLI/GUI actions. See [multiple workspaces](MULTIPLE_WORKSPACES.md).
