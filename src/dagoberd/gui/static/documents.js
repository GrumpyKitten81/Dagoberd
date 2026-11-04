import { diagnostic, statusLabel, msg, joinMessages, number, date } from './i18n.js';
import { append, clear, dialog, el, formatAmount, notify } from './dom.js';
import { api, apiBlob, apiUpload } from './store.js';

export async function assignDocumentDialog(transaction, reload) {
  const documents = await api('/documents?status=open');
  const list = el('ul', { className: 'document-picker' });
  const modal = dialog(msg("documents.match_title", { purpose: transaction.purpose }), list);
  if (!documents.length) {
    append(list, el('li', {}, msg("ui.no_open_documents")));
    return;
  }
  for (const document of documents) {
    append(list,
      el('li', {}, [
        msg("documents.option", { filename: document.filename, date: document.document_date ? date(document.document_date) : msg("ui.no_date") }),
        document.amount_minor === null
          ? ''
          : formatAmount(document.amount_minor, document.currency),
        el('button', {
          type: 'button',
          onclick: async () => {
            try {
              await api(`/documents/${document.id}/assign`, {
                method: 'POST',
                body: JSON.stringify({ transaction_id: transaction.id }),
              });
              notify(msg("ui.document_matched"));
              modal.close();
              await reload();
            } catch (error) {
              notify(error.message, 'error');
            }
          },
        }, msg('documents.assign')),
      ]),
    );
  }
}

export async function renderDocuments(root) {
  clear(root);
  const upload = el('input', { type: 'file', accept: '.pdf,application/pdf' });
  const inbox = el('div', { className: 'inbox' });
  append(root,
    el('h2', {}, msg("ui.documents")),
    el('div', { className: 'toolbar' }, [
      upload,
      el('button', {
        type: 'button',
        onclick: async () => {
          const file = upload.files[0];
          if (!file) {
            notify(msg("ui.select_a_file_first"), 'error');
            return;
          }
          try {
            await apiUpload('/documents/upload', file);
            notify(msg("ui.document_imported"));
            await refresh(inbox);
          } catch (error) {
            notify(error.message, 'error');
          }
        },
      }, msg("ui.upload_document")),
    ]),
    inbox,
  );
  await refresh(inbox);
}

async function refresh(inbox) {
  const documents = await api('/documents');
  clear(inbox);
  if (!documents.length) {
    append(inbox, el('p', {}, msg("ui.no_documents_available")));
    return;
  }
  for (const document of documents) {
    append(inbox, documentCard(document, inbox));
  }
}

function documentCard(document, inbox) {
  const candidates = el('div', { className: 'candidates' });
  return el('article', { className: 'document-card' }, [
    el('h3', {}, document.filename),
    el('p', {}, [
      msg("documents.status", { status: statusLabel(document.status) }),
      document.amount_minor === null
        ? ''
        : msg('documents.amount', {amount: formatAmount(document.amount_minor, document.currency)}),
      document.document_date ? msg('documents.date', {date: date(document.document_date)}) : '',
    ]),
    el('div', { className: 'toolbar' }, [
      el('button', {
        type: 'button',
        onclick: async () => {
          try {
            const matches = await api(`/documents/${document.id}/candidates`);
            renderCandidates(candidates, matches, inbox);
          } catch (error) {
            notify(error.message, 'error');
          }
        },
      }, msg("ui.find_candidates")),
      el('button', { type: 'button', onclick: () => preview(document) }, msg("ui.view")),
    ]),
    candidates,
  ]);
}

function renderCandidates(target, matches, inbox) {
  clear(target);
  if (!matches.length) {
    append(target, el('p', {}, msg("ui.no_candidates_found")));
    return;
  }
  for (const match of matches) {
    append(target,
      el('div', { className: 'candidate' }, [
        msg("ui.score_status", { score: number(match.score, {minimumFractionDigits: 2, maximumFractionDigits: 2}), reasons: joinMessages(match.reasons.map(diagnostic), ', '), status: statusLabel(match.status) }),
        ...['accept', 'reject'].map((decision) =>
          el('button', {
            type: 'button',
            onclick: async () => {
              try {
                await api(`/documents/matches/${match.match_id}/review`, {
                  method: 'POST',
                  body: JSON.stringify({ decision, expected_version: match.version }),
                });
                notify(msg("ui.match", { decision: decision === 'accept' ? msg("ui.accepted") : msg("ui.rejected") }));
                await refresh(inbox);
              } catch (error) {
                notify(error.message, 'error');
              }
            },
          }, decision === 'accept' ? msg("ui.accept") : msg("ui.reject")),
        ),
      ]),
    );
  }
}

// DESIGN: fetched as a blob because an <embed src> cannot send the GUI token header.
async function preview(document) {
  try {
    const blob = await apiBlob(`/documents/${document.id}/file`);
    const url = URL.createObjectURL(blob);
    const modal = dialog(
      document.filename,
      el('embed', { src: url, type: document.mime_type || 'application/pdf', className: 'preview' }),
    );
    modal.addEventListener('close', () => URL.revokeObjectURL(url));
  } catch (error) {
    notify(error.message, 'error');
  }
}
