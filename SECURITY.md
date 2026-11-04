# Security policy

Report security concerns through the repository's public GitHub Issues using the **Security report** form. There is no separate email, private advisory or other reporting channel. All reports are public: use synthetic examples and sanitized diagnostics. Never include credentials, PINs, TANs, tokens, account identifiers, bank exports, receipts, databases or reports.

The current beta/release line is supported. Critical reports are prioritized, but no response time is guaranteed. Disable affected banking, model or delivery adapters while investigating a suspected problem.

Dagoberd performs no payments. The experimental Comdirect adapter is read-only and has incomplete product coverage. File imports are recommended. Authentication takes place locally, either in the CLI or the token-protected loopback GUI. PIN/TAN input is held in process memory and is not persisted or returned by GUI endpoints. An explicitly configured timed session may store session tokens in the OS keyring; it never stores the PIN. MCP does not accept PINs or TANs.

See the [threat model](doc/THREAT_MODEL.md) and [Runbook](doc/RUNBOOK.md) for boundaries and recovery procedures. Local financial files must never be attached to an issue.
