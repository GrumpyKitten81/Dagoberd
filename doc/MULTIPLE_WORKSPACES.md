# Multiple workspaces

One local user can maintain separate private, business or association accounts. A workspace is a complete configuration and ledger boundary, rather than an account filter. Accounts, previews, classifications, receipts, reports, secrets and bank sessions stay within it. There are no combined reports across workspaces.

## Initialize or register repositories

Run the installed `account-eval` command, or prefix it with `poetry run` in a development checkout:

```sh
account-eval workspaces init private /path/to/provided-repository
account-eval workspaces init business /path/to/other-repository
account-eval workspaces list
account-eval --workspace private doctor
account-eval --workspace business accounts list
account-eval gui
```

`init` preserves existing files and configuration, adds missing workspace identity and ignore rules, and initializes the ledger if necessary. Repeating it preserves the same identity and data. `create <name> <directory>` additionally requires a new or empty directory. Git itself is optional for local workspaces.

Register an existing configuration without initializing its ledger:

```sh
account-eval workspaces add private /path/to/repository/account-eval.toml
account-eval workspaces clone business /path/to/new-checkout --remote ssh://git@example.org/team/private-ledger.git
account-eval workspaces doctor business
```

Registration adds a missing stable UUID to the repository configuration but does not migrate or write ledger data. At first ledger initialization, existing pending legacy previews are invalidated and must be recreated in the selected workspace; executed history remains available. Cloning initializes a repository without AccountEval configuration, or preserves the configuration already present. Repository scripts are not executed and new report extensions are not activated by initialization. Configure trusted extensions explicitly afterward.

The UUID travels with repository configuration. Registering the same UUID twice locally is rejected. `remove <name>` removes only the registry entry; it preserves files, secrets and repository history. A ledger already bound to a different UUID cannot be opened or restored into the selected workspace.

The registry is stored in the operating system's user configuration directory under `account-eval/workspaces.toml`. Override it with `--workspace-registry <file>` or `ACCOUNT_EVAL_WORKSPACE_REGISTRY`. Keep it private: it contains local paths and remote approval metadata.

## Selection and credentials

With multiple entries, CLI operations require `--workspace <name-or-uuid>` or an explicit `--config <file>`. These selectors are mutually exclusive. Registered workspaces reject `--db`, `ACCOUNT_EVAL_DATA_ROOT` and `ACCOUNT_EVAL_CLOUD_WORKSPACE` overrides. Put workspace-specific paths in their configuration instead. Unregistered standalone configurations retain their existing behavior.

The GUI starts with a picker and keeps selection in each tab's URL. Switching reloads the page after confirmation and discards unsaved input; it does not redirect running jobs into the new workspace. The workspace management dialog provides initialization, registration, cloning, credential adoption, cloud relocation and Git previews.

MCP `list_workspaces` returns local entries. Domain tools accept `workspace_id`; selection is required when multiple workspaces are registered. Bind one server explicitly with `ACCOUNT_EVAL_WORKSPACE` or `ACCOUNT_EVAL_CONFIG`. Tool selectors cannot override that binding. Data URLs use `/api/workspaces/<uuid>/...`; unscoped data URLs are available only in standalone mode. Public contracts are CLI v3, GUI v5 and MCP v2.

Keyring services use `account-eval.workspace.<uuid>`. Workspace-local `.env` files are read without modifying process environment. Registered workspaces do not fall back to global environment credentials or the legacy keyring. Store secrets through the selected workspace:

```sh
account-eval --workspace private config set-secret comdirect.primary.client-id
account-eval workspaces adopt-credentials private --secret comdirect.primary.client-id --yes
```

Adoption copies explicitly named legacy keyring entries and never copies bank sessions. Authenticate each workspace locally again. PINs, TANs and credentials must not appear in chat or Git.

## Separate cloud files

For new workspaces, initialization accepts `--cloud-root /path/to/synchronized-files`. For configured workspaces, store the machine-specific root in ignored `account-eval.local.toml`:

```toml
[cloud]
root = "/path/to/synchronized-files"
```

The effective folders are:

```text
<root>/workspaces/<private-uuid>/documents/
<root>/workspaces/<private-uuid>/reports/
<root>/workspaces/<business-uuid>/documents/
<root>/workspaces/<business-uuid>/reports/
```

Display-name changes do not change these paths. Different machines using the same repository UUID use the same logical cloud namespace. The database stays local by default. Never concurrently edit a cloud-synchronized database from different machines.

`cloud.workspace` continues to name one explicit legacy workspace directory. It cannot coexist with `cloud.root`. Canonical paths, including links, are checked for overlap with other registered workspaces; explicit document/report overrides cannot escape a configured shared-root namespace.

To relocate existing files, use a preview and local confirmation:

```sh
account-eval workspaces cloud private preview --root /path/to/new-cloud-root
account-eval workspaces cloud private run <preview-id> --fingerprint <sha256> --yes
```

The preview lists sources, destination, hashes and configuration fingerprints. Execution requires an unchanged workspace and an unused destination. It copies and verifies documents and recorded report artifacts, updates their ledger references and configuration, preserves original files, and never relocates the active ledger. A failed copy leaves the old configuration active; inspect the incomplete destination before retrying with a new destination. Remove old copies only through a separate explicitly reviewed operation.

A local journal and verified ledger backup protect the configuration/reference update. An interrupted update blocks further workspace operations. Use `account-eval workspaces cloud <name> recover --yes` to restore the previous configuration and ledger; copied cloud files remain available for inspection. The GUI management dialog provides the same locally confirmed recovery.

## Explicit private Git synchronization

Git must be installed; existing SSH configuration or credential helpers handle authentication. Confirm that `origin` is a private destination before integrated commits or pushes:

```sh
account-eval workspaces git private status
account-eval workspaces git private trust --yes
account-eval workspaces git private preview commit --file account-eval.toml --file output/account_eval.db --message "Update private ledger"
account-eval workspaces git private run <preview-id> --fingerprint <sha256> --yes
account-eval workspaces git private preview push
account-eval workspaces git private run <push-preview-id> --fingerprint <sha256> --yes
```

Only selected files are committed. Secrets, machine-local settings, locks and SQLite sidecars are excluded. All staged files must appear in the preview. Ledger commits require an exclusive workspace lock, SQLite checkpoint and integrity check. All participating GUI, CLI and MCP operations honor the same lock.

Pull uses `preview pull` followed by `run`; it requires a clean tree and permits only fast-forward updates. Configure the branch's upstream locally before pulling. Changed files, HEAD, index, tracking state or remote destination invalidate previews. A changed configuration or overlapping storage after pull prevents further workspace use until repaired.

There are no automatic commits, merges, rebases, resets or force-pushes. Resolve divergence and conflicts with local Git tools after stopping workspace writers. Git can version files inside the repository only; external cloud files require a separate explicit storage arrangement. Git does not replace encrypted backups. External tools do not honor AccountEval workspace locks.

MCP can inspect Git status and prepare previews. Executing Git changes or transfers remains a locally confirmed CLI/GUI action.
