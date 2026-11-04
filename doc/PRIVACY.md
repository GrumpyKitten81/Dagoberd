# Privacy

AccountEval stores its SQLite ledger, documents, reports, backups and audit data in the configured local workspace. It has no telemetry or AccountEval cloud service. A user-configured synchronized workspace is subject to that storage provider's policies.

AccountEval transfers data externally only through explicitly enabled adapters:

- The experimental Comdirect adapter makes read-only banking API calls. File imports are the preferred workflow.
- A cloud LLM receives exactly the redacted payload shown in an approved preview.
- SMTP sends exactly the hashed report artifacts to the previewed recipient after local confirmation.

The optional classifier is separate from the calling coding agent. Local model endpoints must be loopback; remote endpoints require cloud mode and opt-in. Previews bind the endpoint and request configuration. Model requests do not follow redirects, and local model calls ignore environment proxies. Redaction is not complete anonymization. AccountEval cannot control what the calling agent transfers from files or CLI/MCP output to its own model or research tools. See [Agent and LLM integration](AGENT_LLM_INTEGRATION.md).

Secrets use the OS keyring, with environment variables and an ignored `.env` as fallbacks where supported. Comdirect credentials use the keyring. PIN/TAN entry remains local to the CLI or token-protected browser GUI and is not persisted; explicitly enabled timed sessions may retain session tokens in the keyring. Portable exports are unencrypted; `.aebak` backups are encrypted.

The user controls device security, backups, deletion and external provider choices. GitHub Issues, including security reports, are public and must contain only synthetic examples and sanitized diagnostics. Never submit financial files or credentials.

## Workspace boundaries

Registered workspaces isolate ledgers, file stores, keyring entries and bank sessions by stable workspace identity. A shared cloud root uses separate UUID directories. The local registry contains sensitive machine paths and belongs outside public repositories. Local `.env` files are read without exporting their values into the process environment; global credential fallbacks are unavailable in registered workspaces.

Git is an explicit external transfer. Confirm the private destination and inspect the exact files before commit/push. Git authentication uses existing SSH or credential helpers. Secrets and runtime sidecars cannot be selected through the integrated commit workflow. Cloud relocation preserves originals; deleting old copies is a separate explicit operation. See [workspace operations](MULTIPLE_WORKSPACES.md).
