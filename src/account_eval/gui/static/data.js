import { diagnostic, msg, joinMessages, date, number } from './i18n.js';
import { append, clear, dialog, el, formatAmount, notify } from './dom.js';
import { renderDocuments } from './documents.js';
import { api, apiUpload, state } from './store.js';
import { readSession, writeSession } from './browser-storage.js';

const JOB_STORAGE_KEY = `account-eval.sync-job.${state.workspaceId || 'legacy'}`;

export async function renderData(root) {
  clear(root);
  const documents = el('section', { className: 'panel' });
  append(root, importPanel(), syncPanel(), classificationPanel(), documents);
  await renderDocuments(documents);
}

function importPanel() {
  const file = el('input', { type: 'file' });
  const sources = [...state.metadata.import_sources].sort((left) => (left === 'tabular' ? -1 : 1));
  const source = el('select', {}, sources.map((item) => el('option', { value: item }, item)));
  const profile = el('select', {}, [
    el('option', { value: '' }, msg("ui.no_profile")),
    ...state.metadata.import_profiles.map((item) => el('option', { value: item }, item)),
  ]);
  const result = el('div', { className: 'preview' });
  const start = el('button', {
    type: 'button',
    onclick: async () => {
      if (!file.files[0]) {
        notify(msg("ui.select_a_file_first"), 'error');
        return;
      }
      try {
        const upload = await apiUpload('/imports/upload', file.files[0]);
        const preview = await api('/imports/preview', {
          method: 'POST',
          body: JSON.stringify({
            source: source.value,
            profile: profile.value || null,
            account_id: state.accountId || null,
            path: upload.path,
          }),
        });
        renderPreview(result, preview);
      } catch (error) {
        notify(error.message, 'error');
      }
    },
  }, msg("ui.upload_and_preview"));
  return el('section', { className: 'panel' }, [
    el('h2', {}, msg("ui.file_import")),
    el('p', { className: 'bank-support-note' }, msg('bank.experimental')),
    el('p', {}, msg("ui.the_account_selected_above_is_the_import_target")),
    el('div', { className: 'toolbar' }, [file, source, profile, start]),
    result,
  ]);
}

function renderPreview(target, preview) {
  clear(target);
  const totals = Object.entries(preview.totals)
    .map(([currency, amount]) => formatAmount(Number(amount) * 100, currency));
  const facts = [
    [msg("ui.transactions"), preview.count],
    [msg("ui.total_rows"), preview.rows_total],
    [msg("ui.period"), msg("common.date_range", { from: date(preview.booking_from), to: date(preview.booking_to) })],
    [msg("ui.totals"), totals.length ? joinMessages(totals, ', ') : '—'],
    [msg("ui.duplicates"), preview.duplicate_candidates],
    [msg("ui.errors"), preview.error_count],
  ];
  const run = el('button', {
    type: 'button',
    disabled: preview.error_count > 0,
    onclick: async () => {
      try {
        const executed = await api(`/imports/${preview.preview_id}/run`, { method: 'POST' });
        notify(
          joinMessages([msg("ui.import_complete_new_skipped", { imported: executed.imported, skipped: executed.skipped }), msg("ui.updated_annotations", { updated: executed.updated, annotations: executed.annotations_applied })]),
        );
        renderPreview(target, { ...preview, executed });
      } catch (error) {
        notify(error.message, 'error');
      }
    },
  }, msg("ui.run_import"));
  append(target,
    el('dl', { className: 'facts' }, facts.flatMap(([term, value]) => [
      el('dt', {}, term),
      el('dd', {}, typeof value === 'number' ? number(value) : value),
    ])),
    preview.warnings.length
      ? el('ul', { className: 'warnings' }, preview.warnings.map((item) => el('li', {}, diagnostic(item))))
      : null,
    preview.errors.length
      ? el('ul', { className: 'errors' }, preview.errors.slice(0, 20).map((item) =>
          el('li', {}, JSON.stringify(item)),
        ))
      : null,
    preview.executed ? el('p', {}, msg("ui.import_completed")) : run,
  );
}

function syncPanel() {
  if (!state.metadata.comdirect_connections.length) {
    return el('section', { className: 'panel' }, [
      el('h2', {}, msg("ui.comdirect_sync_experimental")),
      el('p', { className: 'bank-support-note' }, msg('bank.experimental')),
      el('p', {}, msg("ui.no_comdirect_connection_configured")),
    ]);
  }
  const connection = el('select', {}, state.metadata.comdirect_connections.map((item) =>
    el('option', { value: item }, item),
  ));
  const from = el('input', { type: 'date' });
  const to = el('input', { type: 'date' });
  const status = el('div', { className: 'sync-status' });
  const existingJob = readSession(JOB_STORAGE_KEY);
  if (existingJob) pollSync(existingJob, status);
  const start = el('button', {
    type: 'button',
    onclick: () => startSync(connection.value, from.value, to.value, status),
  }, msg("ui.synchronize"));
  return el('section', { className: 'panel' }, [
    el('h2', {}, msg("ui.comdirect_sync_experimental")),
      el('p', { className: 'bank-support-note' }, msg('bank.experimental')),
    el('div', { className: 'toolbar' }, [
      connection,
      el('label', {}, ['ab', from]),
      el('label', {}, ['bis', to]),
      start,
    ]),
    status,
  ]);
}

function startSync(connection, bookingFrom, bookingTo, status) {
  const pin = el('input', { type: 'password', autocomplete: 'off' });
  const submit = el('button', {
    type: 'button',
    onclick: async () => {
      modal.close();
      try {
        const job = await api('/sync/comdirect', {
          method: 'POST',
          body: JSON.stringify({
            connection,
            pin: pin.value,
            booking_from: bookingFrom || null,
            booking_to: bookingTo || null,
          }),
        });
        writeSession(JOB_STORAGE_KEY, job.job_id);
        pollSync(job.job_id, status);
      } catch (error) {
        notify(error.message, 'error');
      }
    },
  }, msg("ui.start"));
  const modal = dialog(msg("ui.comdirect_pin"), el('label', {}, [msg("ui.pin"), pin]), [submit]);
}

async function pollSync(jobId, status) {
  clear(status);
  append(status, msg("ui.synchronization_running"));
  let tanOpen = false;
  const timer = setInterval(async () => {
    if (!status.isConnected) {
      clearInterval(timer);
      return;
    }
    let job;
    try {
      job = await api(`/jobs/${jobId}`);
    } catch (error) {
      clearInterval(timer);
      notify(error.message, 'error');
      return;
    }
    if (!status.isConnected) return;
    if (job.status === 'awaiting_tan' && !tanOpen) {
      tanOpen = true;
      requestTan(jobId, job.challenge, () => {
        tanOpen = false;
      });
    }
    if (job.status === 'succeeded' || job.status === 'failed') {
      writeSession(JOB_STORAGE_KEY, '');
      clearInterval(timer);
      clear(status);
      append(status,
        job.status === 'succeeded'
          ? joinMessages([msg("ui.done_imported_skipped", { imported: job.result.imported, skipped: job.result.skipped }), msg("ui.updated_accounts", { updated: job.result.updated, accounts: job.result.synchronized_accounts })])
          : msg("ui.failed", { details: job.error }),
      );
    }
  }, 1000);
}

function requestTan(jobId, challenge, done) {
  const tan = el('input', { autocomplete: 'off', inputMode: 'numeric' });
  const image = challenge.image_base64
    ? el('img', { src: `data:image/png;base64,${challenge.image_base64}`, alt: msg("ui.phototan") })
    : el('p', {}, msg("ui.confirm_the_request_in_the_comdirect_phototan_app"));
  const submit = el('button', {
    type: 'button',
    onclick: async () => {
      try {
        await api(`/jobs/${jobId}/tan`, {
          method: 'POST',
          body: JSON.stringify({ tan: tan.value }),
        });
        modal.close();
        done();
      } catch (error) {
        notify(error.message, 'error');
      }
    },
  }, msg("ui.send_tan"));
  const modal = dialog(msg("ui.tan_confirmation", { kind: challenge.kind }), el('div', {}, [image, el('label', {}, [msg("ui.tan"), tan])]), [submit]);
}

function classificationPanel() {
  const profile = el('select', {}, Object.keys(state.metadata.profiles).map((code) =>
    el('option', { value: code }, code),
  ));
  const output = el('div', { className: 'classification-result' });
  const runner = (path, label) =>
    el('button', {
      type: 'button',
      onclick: async () => {
        try {
          const result = await api(path, {
            method: 'POST',
            body: JSON.stringify({
              profile_code: profile.value,
              account_id: state.accountId || null,
            }),
          });
          clear(output);
          append(output, msg("operation.result", { label: label, details: JSON.stringify(result) }));
        } catch (error) {
          notify(error.message, 'error');
        }
      },
    }, label);
  return el('section', { className: 'panel' }, [
    el('h2', {}, msg("ui.classification")),
    el('div', { className: 'toolbar' }, [
      profile,
      runner('/classification/rules', msg("ui.apply_rules")),
      runner('/classification/auto', msg("ui.classify_automatically")),
    ]),
    output,
  ]);
}
