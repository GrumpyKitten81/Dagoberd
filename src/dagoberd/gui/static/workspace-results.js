import { append, el } from './dom.js';
import { msg } from './i18n.js';

function fact(list, label, value) {
  if (value === undefined || value === null || value === '') return;
  append(list, el('dt', {}, label), el('dd', {}, value));
}
const yesNo = (value) => msg(value ? 'ui.yes' : 'ui.no');

export function workspaceResult(definition, result, { preview = false, target = '', onOpen = null } = {}) {
  const node = el('section', { className: 'workspace-result', ariaLive: 'polite' });
  append(node, el('h3', {}, msg(preview ? 'management.preview_ready' : 'management.completed')),
    el('p', {}, msg(`workspace.action.${definition.id}`)));
  const facts = el('dl', { className: 'workspace-result-facts' });
  append(node, facts);
  fact(facts, msg('management.target'), target);
  if (result.valid !== undefined) fact(facts, msg('management.valid'), yesNo(result.valid));
  if (result.storage === 'local') fact(facts, msg('management.storage'), msg('management.local_storage'));
  if (result.database_exists !== undefined) fact(facts, msg('management.database'), yesNo(result.database_exists));
  if (result.git) {
    fact(facts, msg('management.git_configured'), yesNo(result.git.configured));
    fact(facts, msg('management.git_available'), yesNo(result.git.available));
  }
  if (result.synchronization) fact(facts, msg('management.sync_configured'), yesNo(result.synchronization.backend));
  if (Object.hasOwn(result, 'backend')) fact(facts, msg('management.sync_configured'), yesNo(result.backend));
  if (result.available !== undefined) fact(facts, msg('management.available'), yesNo(result.available));
  if (result.changes !== undefined) fact(facts, msg('management.git_changes'), yesNo(Boolean(result.changes)));
  fact(facts, msg('management.branch'), result.branch);
  fact(facts, msg('management.revision'), result.remote_revision || result.local_revision);
  fact(facts, msg('management.copied_files'), result.copied_files);
  if (result.recovered !== undefined) fact(facts, msg('management.recovered'), yesNo(result.recovered));
  if (result.cloud_copies_preserved !== undefined) fact(facts, msg('management.cloud_preserved'), yesNo(result.cloud_copies_preserved));
  if (result.adopted === true) fact(facts, msg('management.adopted'), yesNo(true));
  if (Array.isArray(result.adopted)) fact(facts, msg('management.credentials_count'), result.adopted.length);
  if (result.files_preserved !== undefined || result.original_files_preserved !== undefined) {
    fact(facts, msg('management.files_preserved'), yesNo(result.files_preserved ?? result.original_files_preserved));
  }
  if (preview) {
    append(node, el('p', {}, msg(`management.effect.${definition.id}`)));
    for (const [key, label] of [['origin', 'workspace.remote'], ['branch', 'management.branch'],
      ['message', 'workspace.message'], ['cloud_root', 'management.cloud_root'],
      ['destination', 'management.destination'], ['namespace', 'workspace.snapshot_root'],
      ['base', 'management.base_revision'], ['local_digest', 'management.local_state']]) {
      fact(facts, msg(label), result[key]);
    }
    if (result.head) {
      fact(facts, msg('management.revision'), result.head.revision ?? result.head);
      fact(facts, msg('management.checksum'), result.head.sha256);
    }
    if (Array.isArray(result.files)) {
      fact(facts, msg('management.file_count'), result.files.length);
      const files = el('ul', { className: 'workspace-file-list' });
      for (const file of result.files) {
        append(files, el('li', {}, typeof file === 'string' ? file : [
          el('span', {}, file.source), el('span', {}, file.relative), el('code', {}, file.sha256),
        ]));
      }
      append(node, files);
    }
  }
  if (definition.id === 'snapshot-inspect') {
    fact(facts, msg('workspace.snapshot'), result.snapshot);
    fact(facts, msg('management.workspace_identity'), result.workspace_id);
    fact(facts, msg('management.revision'), result.revision);
    fact(facts, msg('workspace.fingerprint'), result.fingerprint);
  }
  if (result.error) append(node, el('p', { className: 'errors' }, result.error));
  if (onOpen) append(node, el('p', {}, msg('management.open_hint')),
    el('button', { type: 'button', onclick: onOpen }, msg('management.open')));
  else if (!preview) append(node, el('p', {}, msg('management.result_hint')));
  append(node, el('details', { className: 'workspace-technical' }, [
    el('summary', {}, msg('dashboard.diagnostics')),
    el('pre', {}, JSON.stringify(result, null, 2)),
  ]));
  return node;
}
