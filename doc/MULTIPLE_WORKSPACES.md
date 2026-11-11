# Multiple workspaces

One local user can maintain separate private, business or association accounts. A workspace is a complete configuration and ledger boundary, rather than an account filter. Accounts, previews, classifications, receipts, reports, secrets and bank sessions stay within it. There are no combined reports across workspaces.

## Create or register local folders

Run the installed `dagoberd` command, or prefix it with `poetry run` in a development checkout:

```sh
dagoberd workspaces create private /path/to/new-local-folder
dagoberd workspaces init business /path/to/existing-local-folder
dagoberd workspaces list
dagoberd workspaces status private
dagoberd --workspace private doctor
dagoberd --workspace business accounts list
dagoberd gui
```

`init` preserves existing files and configuration, adds missing workspace identity and ignore rules, and initializes the ledger if necessary. Repeating it preserves the same identity and data. `create <name> <directory>` additionally requires a new or empty directory. Neither command runs Git, creates a repository, needs GitHub credentials or contacts a network service. Optional bank/model integrations have their own separate requirements. The workspace is a local directory, not a repository property; its UUID and credentials remain stable if versioning or synchronization is added later.

Register an existing configuration without initializing its ledger:

```sh
dagoberd workspaces add private /path/to/repository/dagoberd.toml
dagoberd workspaces clone business /path/to/new-checkout --remote ssh://git@example.org/team/private-ledger.git
dagoberd workspaces doctor business
```

Registration adds a missing stable UUID to the repository configuration but does not migrate or write ledger data. At first ledger initialization, existing pending legacy previews are invalidated and must be recreated in the selected workspace; executed history remains available. Cloning initializes a repository without Dagoberd configuration, or preserves the configuration already present. Repository scripts are not executed and new report extensions are not activated by initialization. Configure trusted extensions explicitly afterward.

The UUID travels with repository configuration. Registering the same UUID twice locally is rejected. `remove <name>` removes only the registry entry; it preserves files, secrets and repository history. A ledger already bound to a different UUID cannot be opened or restored into the selected workspace.

The registry is stored in the operating system's user configuration directory under `dagoberd/workspaces.toml`. Override it with `--workspace-registry <file>` or `DAGOBERD_WORKSPACE_REGISTRY`. Keep it private: it contains local paths, optional snapshot-folder settings and remote approval metadata.

## Selection and credentials

With multiple entries, CLI operations require `--workspace <name-or-uuid>` or an explicit `--config <file>`. These selectors are mutually exclusive. Registered workspaces reject `--db`, `DAGOBERD_DATA_ROOT` and `DAGOBERD_CLOUD_WORKSPACE` overrides. Put workspace-specific paths in their configuration instead. Unregistered standalone configurations retain their existing behavior.

The GUI header shows the active workspace in one control next to the Dagoberd name. Open it to switch workspaces or choose **Manage workspaces**. The menu marks the current entry; identical display names include their registration names. Keyboard users can open it with Enter, Space or the arrow keys, move with Up/Down or Home/End, and close it with Escape. Browsers without popover support use a selection dialog.

Selection stays in each tab's URL. With no selection, choose a workspace explicitly, even if only one is registered; there is no remembered or automatic selection. An unknown workspace link shows the selection page rather than loading another workspace. Unregistered standalone configurations remain usable without a workspace selection and show **Local configuration** when management is available. A standalone GUI without management support has no workspace control.

Switching reloads the page after confirmation and discards unsaved input; cancelling preserves the current workspace and input. Running jobs remain in their original workspace. The workspace management dialog provides initialization, registration, cloning, credential adoption, cloud relocation and Git previews. Workspace lifecycle actions belong in the header menu; **Administration** manages accounts and reference data within the active workspace. Existing previews and confirmations still apply.

MCP `list_workspaces` returns local entries. Domain tools accept `workspace_id`; selection is required when multiple workspaces are registered. Bind one server explicitly with `DAGOBERD_WORKSPACE` or `DAGOBERD_CONFIG`. Tool selectors cannot override that binding. Data URLs use `/api/workspaces/<uuid>/...`; unscoped data URLs are available only in standalone mode. Public contracts are CLI v3, GUI v5 and MCP v2.

Keyring services use `dagoberd.workspace.<uuid>`. Workspace-local `.env` files are read without modifying process environment. Registered workspaces do not fall back to global environment credentials or the legacy keyring. Store secrets through the selected workspace:

```sh
dagoberd --workspace private config set-secret comdirect.primary.client-id
dagoberd workspaces adopt-credentials private --secret comdirect.primary.client-id --yes
```

Adoption copies explicitly named legacy keyring entries and never copies bank sessions. Authenticate each workspace locally again. PINs, TANs and credentials must not appear in chat or Git.

## Separate cloud files

For new workspaces, initialization accepts `--cloud-root /path/to/synchronized-files`. For configured workspaces, store the machine-specific root in ignored `dagoberd.local.toml`:

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
dagoberd workspaces cloud private preview --root /path/to/new-cloud-root
dagoberd workspaces cloud private run <preview-id> --fingerprint <sha256> --yes
```

The preview lists sources, destination, hashes and configuration fingerprints. Execution requires an unchanged workspace and an unused destination. It copies and verifies documents and recorded report artifacts, updates their ledger references and configuration, preserves original files, and never relocates the active ledger. A failed copy leaves the old configuration active; inspect the incomplete destination before retrying with a new destination. Remove old copies only through a separate explicitly reviewed operation.

A local journal and verified ledger backup protect the configuration/reference update. An interrupted update blocks further workspace operations. Use `dagoberd workspaces cloud <name> recover --yes` to restore the previous configuration and ledger; copied cloud files remain available for inspection. The GUI management dialog provides the same locally confirmed recovery.

## Optional Git versioning and remotes

Add Git later without changing workspace identity, paths, previews, data or credential scope:

```sh
dagoberd workspaces git private init --yes
dagoberd workspaces git private status
dagoberd workspaces git private preview commit --file dagoberd.toml --file .gitignore --message "Local version"
dagoberd workspaces git private run <preview-id> --fingerprint <sha256> --yes
```

`init` adds repository metadata and ignore rules only; it creates no commit or remote.
It is repeatable and preserves an existing repository. Configure a Git author locally
before committing. Local commits work without any remote, GitHub account or network.
A missing Git executable affects Git actions only, not workspace operations or diagnostics.

Connect any supported private Git host later, or disconnect origin without removing
local data/history:

```sh
dagoberd workspaces git private remote --url ssh://git@example.org/team/private-ledger.git --yes
dagoberd workspaces git private remote --remove --yes
```

Changing/disconnecting origin clears its previous privacy approval. No contact or
upload occurs when setting its URL. GitHub is not required.

### Explicit private Git synchronization

Git must be installed; existing SSH configuration or credential helpers handle authentication. Confirm that `origin` is a private destination before integrated commits or pushes when a remote is configured:

```sh
dagoberd workspaces git private status
dagoberd workspaces git private trust --yes
dagoberd workspaces git private preview commit --file dagoberd.toml --file output/dagoberd.db --message "Update private ledger"
dagoberd workspaces git private run <preview-id> --fingerprint <sha256> --yes
dagoberd workspaces git private preview push
dagoberd workspaces git private run <push-preview-id> --fingerprint <sha256> --yes
```

Only selected files are committed. Secrets, machine-local settings, locks and SQLite sidecars are excluded. All staged files must appear in the preview. Ledger commits require an exclusive workspace lock, SQLite checkpoint and integrity check. All participating GUI, CLI and MCP operations honor the same lock.

Pull uses `preview pull` followed by `run`; it requires a clean tree and permits only fast-forward updates. Configure the branch's upstream locally before pulling. Changed files, HEAD, index, tracking state or remote destination invalidate previews. A changed configuration or overlapping storage after pull prevents further workspace use until repaired.

There are no automatic commits, merges, rebases, resets or force-pushes. Resolve divergence and conflicts with local Git tools after stopping workspace writers. Git can version files inside the repository only; external cloud files require a separate explicit storage arrangement. Git does not replace encrypted backups. External tools do not honor Dagoberd workspace locks.

MCP can inspect Git status and prepare previews. Executing Git changes or transfers remains a locally confirmed CLI/GUI action.

## Optional snapshot-folder synchronization without Git

The `snapshot-folder` backend exchanges immutable verified portable snapshots through
a user-managed private folder. A cloud client or another replication mechanism can
transport that folder independently of Dagoberd. No cloud SDK, hosting account or Git
repository is required by Dagoberd. The existing cloud-file relocation feature is
separate: it relocates documents/reports, whereas snapshots also contain a consistent
ledger backup and registered report artifacts.

```sh
dagoberd workspaces sync private configure --root /path/to/private-synchronized-folder --yes
dagoberd workspaces sync private status
dagoberd workspaces sync private preview publish
dagoberd workspaces sync private run <preview-id> --fingerprint <sha256> --yes
```

Use private access-controlled storage: `.aex` snapshots contain sensitive financial
data and source-path metadata and are not encrypted by this backend. No `.env`,
keyring contents, local overrides, agent scripts, executable extensions or raw project
configuration are uploaded. For encrypted standalone transfer, use the existing
password-protected portability backup workflow instead. The live SQLite database,
WAL/SHM sidecars, machine registry and workspace runtime state stay outside the
synchronized root. Never place an active workspace in that root.
Checksums detect corruption; they do not authenticate an untrusted transport.

Each publication writes `workspaces/<uuid>/<revision>.aex` and a corresponding `.json`
revision record with a checksum and parent revision. Incomplete unreferenced archives
are ignored; a referenced missing or invalid archive blocks exchange until recovered.
Nothing overwrites a previous snapshot. Folder replication may deliver files out of
order: wait for complete replication before retrying. This is explicit single-writer
handover, not multi-user collaboration or automatic two-way merging. A local file lock
is not a distributed cloud lock. Concurrent publications can produce sibling revisions;
exchange detects that divergence after replication and preserves both branches.

### Adopt on another machine

Wait for both files and their ancestors to replicate. Inspect the received `.json`
record, then use its returned fingerprint to adopt into a **new or empty local folder**:

```sh
dagoberd workspaces snapshot-inspect /path/to/private-synchronized-folder/workspaces/<uuid>/<revision>.json
dagoberd workspaces snapshot-adopt private /path/to/new-local-folder --snapshot /path/to/private-synchronized-folder/workspaces/<uuid>/<revision>.json --fingerprint <inspection-fingerprint> --yes
```

Adoption verifies archive integrity and the ledger's workspace identity, preserves the
UUID and data, rebases document/report references and creates safe default local
configuration. It does not restore credentials, private extensions or machine-specific
options. Configure these locally and authenticate bank connections again as needed.
A UUID already registered on the same machine cannot be adopted twice. The source
snapshot and other registered workspaces are preserved.

### Subsequent updates, conflicts and recovery

After finishing work on one machine, publish and wait for replication before using
the next. On a previously adopted machine with no unshared changes:

```sh
dagoberd workspaces sync private preview restore
dagoberd workspaces sync private run <preview-id> --fingerprint <sha256> --yes
```

Previews bind workspace/configuration, local ledger/files and the remote head. Local
changes block restore; a newer remote revision blocks publication until reviewed and
restored. An altered preview, archive or target state aborts exchange. Restore uses
the existing verified portability rollback/recovery machinery. If its journal remains,
follow the runbook's restore recovery procedure before further mutations.

Do not delete revisions or change parent links to hide a conflict. Preserve both
branches and the local data, make separate verified exports/backups, then review the
versions in separate local registries/workspaces before deliberately selecting and
adopting a resolved snapshot in a new directory. There is no automatic reconciliation
of competing ledger versions. Missing/corrupt cloud files or branch conflicts affect
snapshot actions only; ordinary local operations remain available unless an actual
local restore recovery is pending.

```sh
dagoberd workspaces sync private disconnect --yes
dagoberd workspaces status private
```

Disconnection preserves local data and existing snapshots; it stops integration, not
the external cloud client. CLI and GUI expose the same configure/status/inspect/adopt/
preview/run operations. MCP exposes `workspace_status`, `workspace_sync_status` and
`workspace_sync_preview`; confirmation and actual transfers remain in the local
CLI/GUI. The snapshot backend is independent of `WorkspaceGit`; additional transports
can use the same verified portability and revision boundary without becoming a
prerequisite for local workspaces.
