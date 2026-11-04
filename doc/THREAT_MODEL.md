# Threat model

## Assets

Bank credentials and sessions, financial transactions, account identities, documents, reports, categories, audit records and encrypted backups.

## Trust boundaries

- The local operating system and workspace are trusted and require appropriate access controls.
- Banks, optional remote models, SMTP and user-configured synchronized storage have their own security and privacy policies.
- A coding agent or MCP client can invoke business operations but must not receive PINs, TANs or secrets. Its own data transfers are outside AccountEval's control.
- The GUI binds only to loopback and uses an ephemeral token. It displays unmasked local data. PIN/TAN input exists only in the running authentication process; it is not persisted or returned by endpoints.
- Import files and PDFs are untrusted input. Size limits, XML restrictions and local extraction limit exposure.
- Public GitHub Issues are not a confidential channel. Reports must use synthetic examples and sanitized diagnostics.

## Controls

- File imports are the preferred workflow. The experimental Comdirect API is strictly read-only and exposes no payment operations. Product coverage is incomplete; savings support is not verified and Visa credit-card transactions are unavailable.
- Keyring-first secrets, masked diagnostic output and no PIN persistence. Timed sessions are an explicit opt-in for storing session tokens.
- Idempotency, version checks, preview fingerprints and audit for mutations and external actions.
- Cloud LLM and SMTP disabled by default; exact previews bind outgoing data.
- Local models restricted to loopback; no redirects or environment proxies for local calls. Remote models require cloud opt-in. Model responses are strictly validated and remain suggestions.
- Optional local stdio MCP accepts no secret or TAN parameters. Sending email, restore and deletion remain local CLI operations.
- Portability uses hash manifests, path validation, AES-256-GCM/Scrypt and SQLite integrity checks. Restore prepares changes before adoption and retains recovery material after rollback failures.

## Residual risks

A compromised user account or operating system can read local files and keyring values. Broad agent instructions can expose data or create incorrect manual transactions. Bank and file accounts therefore receive transactions only through import or sync; MCP manual entry is disabled by default and restricted to manual accounts.

Heuristic PDF extraction and model classification can be wrong. Uncertain results require review. Missing balances or exchange rates must remain visible rather than being replaced with apparently complete totals. Experimental bank API behavior is not a guarantee of account coverage or financial completeness.

A hard process interruption or power failure during restore is not a transaction across filesystems; manual recovery may be necessary. External adapters process expressly authorized data under their providers' conditions.

## Multiple workspace isolation

The main additional failure modes are accidental workspace selection, identical bank connection names, stale browser requests, copied ledgers, overlapping storage paths and Git transfers to the wrong repository. Mitigations are explicit request-scoped selection, UUID-scoped secrets and database identity, per-tab navigation, separate job registries, canonical path checks and fingerprint-bound Git previews. Workspace locks coordinate participating local application processes; external Git clients, cloud clients and database tools do not honor these locks. The trusted local OS, repository configuration and operator remain part of the trust boundary. Git conflicts and divergent binary ledgers require manual recovery; AccountEval never merges them automatically.
