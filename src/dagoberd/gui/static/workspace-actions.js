const field = (key, label, help, extra = {}) => ({ key, label, help, required: true, ...extra });
export const WORKSPACE_FIELDS = {
  workspace: field('name', 'management.target', 'management.target_help', { type: 'workspace' }),
  newName: field('name', 'workspace.name', 'management.name_help', { pattern: '[a-z0-9][a-z0-9\\-]{0,63}' }),
  directory: field('path', 'management.directory', 'management.directory_help'),
  configuration: field('path', 'management.configuration', 'management.configuration_help'),
  cloud: field('cloud_root', 'workspace.cloud_root', 'management.cloud_help', { required: false }),
  destination: field('cloud_root', 'management.cloud_root', 'management.cloud_help'),
  snapshotRoot: field('cloud_root', 'workspace.snapshot_root', 'management.snapshot_help'),
  snapshot: field('snapshot', 'workspace.snapshot', 'management.snapshot_file_help'),
  fingerprint: field('fingerprint', 'workspace.fingerprint', 'management.fingerprint_help'),
  remote: field('remote', 'workspace.remote', 'management.remote_help'),
  files: field('files', 'workspace.files', 'management.files_help', { type: 'textarea' }),
  credentials: field('files', 'workspace.credentials', 'management.credentials_help', { type: 'textarea' }),
  message: field('message', 'workspace.message', 'management.message_help'),
};
export const WORKSPACE_GROUPS = ['local', 'git', 'snapshots', 'cloud', 'credentials'];
const action = (id, group, fields, options = {}) => ({ id, group, fields, ...options });
export const WORKSPACE_ACTIONS = [
  action('create', 'local', ['newName', 'directory', 'cloud'], { creates: true, refresh: true }),
  action('init', 'local', ['newName', 'directory', 'cloud'], { creates: true, refresh: true, reload: true }),
  action('add', 'local', ['newName', 'configuration'], { creates: true, refresh: true }),
  action('status', 'local', ['workspace']),
  action('remove', 'local', ['workspace'], { refresh: true }),
  action('git-init', 'git', ['workspace'], { confirm: 'workspace.confirm_git', refresh: true, reload: true }),
  action('git-status', 'git', ['workspace']),
  action('git-remote', 'git', ['workspace', 'remote'], { confirm: 'workspace.confirm_git', refresh: true, reload: true }),
  action('git-disconnect', 'git', ['workspace'], { confirm: 'workspace.confirm_git', refresh: true, reload: true }),
  action('git-trust', 'git', ['workspace'], { confirm: 'workspace.confirm_private', refresh: true, reload: true }),
  action('git-pull', 'git', ['workspace'], { execution: 'git-run', reload: true }),
  action('git-commit', 'git', ['workspace', 'files', 'message'], { execution: 'git-run' }),
  action('git-push', 'git', ['workspace'], { execution: 'git-run' }),
  action('clone', 'git', ['newName', 'directory', 'remote', 'cloud'], { creates: true, refresh: true }),
  action('sync-status', 'snapshots', ['workspace']),
  action('sync-configure', 'snapshots', ['workspace', 'snapshotRoot'], { confirm: 'workspace.confirm_snapshot', refresh: true, reload: true }),
  action('sync-disconnect', 'snapshots', ['workspace'], { confirm: 'workspace.confirm_snapshot', refresh: true, reload: true }),
  action('sync-publish', 'snapshots', ['workspace'], { execution: 'sync-run' }),
  action('sync-restore', 'snapshots', ['workspace'], { execution: 'sync-run', reload: true }),
  action('snapshot-inspect', 'snapshots', ['snapshot']),
  action('snapshot-adopt', 'snapshots', ['newName', 'directory', 'snapshot', 'fingerprint'], { creates: true, refresh: true, confirm: 'workspace.confirm_snapshot' }),
  action('cloud-preview', 'cloud', ['workspace', 'destination'], { execution: 'cloud-run', reload: true }),
  action('cloud-recover', 'cloud', ['workspace'], { confirm: 'workspace.confirm_recovery', refresh: true, reload: true }),
  action('adopt-credentials', 'credentials', ['workspace', 'credentials'], { confirm: 'workspace.confirm_credentials', reload: true }),
];
export const EXECUTION_CONFIRMATIONS = {
  'git-run': 'workspace.confirm_git',
  'sync-run': 'workspace.confirm_snapshot',
  'cloud-run': 'workspace.confirm_cloud',
};

export function workspacePayload(definition, values) {
  const payload = { action: definition.id };
  for (const id of definition.fields) {
    const field = WORKSPACE_FIELDS[id];
    const value = (values[id] || '').trim();
    if (!value && !field.required) continue;
    payload[field.key] = field.type === 'textarea'
      ? Object.freeze(value.split('\n').map((line) => line.trim()).filter(Boolean)) : value;
  }
  return Object.freeze(payload);
}
