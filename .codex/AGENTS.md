# Codex harness

Read the shared `AGENTS.md` and its routed guidance first.

## Integration

The optional local stdio-MCP is configured in `.codex/config.toml` with `required = false`.
Create its installation-bound launcher with `dagoberd agents configure` and inspect it with `dagoberd agents doctor`.
A missing MCP must not block CLI development.

## Project subagents

The model-neutral explorer traces code without changing files; the executor implements small, decision-complete tasks; the reviewer checks correctness and data integrity without edits.
The primary agent owns requirements, architecture and final acceptance. Assign bounded file ownership and use concise summaries.
Machine-specific model choices and providers belong in local, ignored settings; project agents use the configured default model.

Never delegate real ledger writes, bank state changes, deliveries or secrets. Do not return local configuration, transaction details, document content or account identifiers in summaries.

The calling agent's model and credentials are independent of Dagoberd's optional classifier. Dagoberd does not launch agents or perform web research through that adapter. Read `doc/AGENT_LLM_INTEGRATION.md` for the division of work and privacy boundaries.

## Financial workflows

Load the dagoberd skill before data workflows. Preserve preview, fingerprint, version and local-confirmation gates.
Read `AGENTS.override.md` if present for private workspace conventions after loading the shared guidance.
