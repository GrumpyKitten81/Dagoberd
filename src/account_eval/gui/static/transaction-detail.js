import { statusLabel, msg, number, date } from './i18n.js';
import { append, clear, el, formatAmount, notify } from './dom.js';
import { api, state } from './store.js';

export function renderDetail(root, detail, { manual, reload, close }) {
  clear(root);
  append(root,
    el('section', { className: 'detail' }, [
      el('header', {}, [
        el('div', {}, [
          el('p', { className: 'eyebrow' }, msg("ui.transaction_details")),
          el('h2', {}, detail.purpose),
        ]),
        el('button', { type: 'button', className: 'icon-button', title: msg("ui.close_details"), onclick: close }, '×'),
      ]),
      manual ? manualForm(detail, reload) : readOnlyFacts(detail),
      splitSection(detail, reload),
      listSection(msg("ui.documents"), detail.documents, (item) => msg('detail.document', {filename: item.filename, status: statusLabel(item.match_status)})),
      listSection(
        msg("ui.classifications"),
        detail.classifications,
        (item) => msg('detail.classification', {profile: item.profile_code, category: item.category_code || '—', source: statusLabel(item.source), status: statusLabel(item.status)}),
      ),
      listSection(
        msg("ui.status_history"),
        detail.status_history,
        (item) => msg('detail.history', {date: date(item.created_at, {dateStyle: 'medium', timeStyle: 'short'}), before: statusLabel(item.from_status || '—'), after: statusLabel(item.to_status)}),
      ),
      listSection(
        msg("ui.relations"),
        detail.relations,
        (item) => msg('detail.relation', {type: statusLabel(item.relation_type), status: statusLabel(item.status)}),
      ),
    ]),
  );
}

function readOnlyFacts(detail) {
  return el('dl', { className: 'facts' }, [
    el('dt', {}, msg("ui.date")),
    el('dd', {}, date(detail.booking_date)),
    el('dt', {}, msg("ui.amount")),
    el('dd', {}, formatAmount(detail.amount_minor, detail.currency)),
    el('dt', {}, msg("ui.counterparty")),
    el('dd', {}, detail.counterparty || '—'),
    el('dt', {}, msg("ui.reference")),
    el('dd', {}, detail.reference || '—'),
    el('dt', {}, msg("ui.note")),
    el('dd', {}, detail.note || '—'),
  ]);
}

function manualForm(detail, reload) {
  const fields = {
    booking_date: el('input', { type: 'date', value: detail.booking_date }),
    amount: el('input', { type: 'number', step: '0.01', value: (detail.amount_minor / 100).toFixed(2) }),
    purpose: el('input', { value: detail.purpose }),
    counterparty: el('input', { value: detail.counterparty || '' }),
    reference: el('input', { value: detail.reference || '' }),
  };
  const save = el('button', {
    type: 'button',
    onclick: async () => {
      try {
        await api(`/transactions/${detail.id}`, {
          method: 'PATCH',
          body: JSON.stringify({
            expected_version: detail.version,
            booking_date: fields.booking_date.value,
            amount: fields.amount.value,
            purpose: fields.purpose.value,
            counterparty: fields.counterparty.value,
            reference: fields.reference.value,
          }),
        });
        notify(msg("ui.transaction_saved"));
        await reload();
      } catch (error) {
        notify(error.message, 'error');
      }
    },
  }, msg("ui.save_transaction_fields"));
  return el('div', { className: 'manual-form' }, [
    labelled(msg("ui.date"), fields.booking_date),
    labelled(msg("ui.amount"), fields.amount),
    labelled(msg("ui.purpose"), fields.purpose),
    labelled(msg("ui.counterparty"), fields.counterparty),
    labelled(msg("ui.reference"), fields.reference),
    save,
  ]);
}

function labelled(text, control) {
  return el('label', {}, [text, control]);
}

function splitSection(detail, reload) {
  const profiles = Object.keys(state.options.category);
  const profileSelect = el('select', {}, profiles.map((code) => el('option', { value: code }, code)));
  const parts = el('div', { className: 'split-parts' });
  const addPart = () => append(parts, splitPart(profileSelect.value));
  const existing = detail.splits.map((split) =>
    el('li', {}, msg('detail.split', {amount: formatAmount(split.amount_minor, detail.currency), comment: split.comment || ''})),
  );
  const submit = el('button', {
    type: 'button',
    onclick: async () => {
      const payload = [...parts.querySelectorAll('.split-part')].map((part) => ({
        amount: part.querySelector('.split-amount').value,
        category_code: part.querySelector('.split-category').value || null,
        comment: part.querySelector('.split-comment').value || null,
      }));
      if (!payload.length) {
        notify(msg("ui.provide_at_least_one_split"), 'error');
        return;
      }
      try {
        await api(`/transactions/${detail.id}/splits`, {
          method: detail.splits.length ? 'PUT' : 'POST',
          body: JSON.stringify({
            expected_version: detail.version,
            profile_code: profileSelect.value,
            parts: payload,
          }),
        });
        notify(msg("ui.split_saved"));
        await reload();
      } catch (error) {
        notify(error.message, 'error');
      }
    },
  }, detail.splits.length ? msg("ui.replace_split") : msg("ui.create_split"));
  const revert = detail.splits.length
    ? el('button', {
        type: 'button',
        onclick: async () => {
          try {
            await api(`/transactions/${detail.id}/splits?expected_version=${detail.version}`, {
              method: 'DELETE',
            });
            notify(msg("ui.split_reverted"));
            await reload();
          } catch (error) {
            notify(error.message, 'error');
          }
        },
      }, msg("ui.revert_split"))
    : null;
  return el('section', { className: 'splits' }, [
    el('h3', {}, msg("ui.splits")),
    existing.length ? el('ul', {}, existing) : el('p', {}, msg("ui.no_splits")),
    labelled(msg("ui.profile"), profileSelect),
    parts,
    el('button', { type: 'button', onclick: addPart }, msg("ui.split")),
    submit,
    revert,
  ]);
}

function splitPart(profile) {
  const categories = state.options.category[profile] || [];
  const category = el('select', { className: 'split-category' }, [
    el('option', { value: '' }, '—'),
    ...categories.map((item) => el('option', { value: item.code }, item.name)),
  ]);
  return el('div', { className: 'split-part' }, [
    el('input', { className: 'split-amount', type: 'number', step: '0.01', placeholder: msg("ui.amount") }),
    category,
    el('input', { className: 'split-comment', placeholder: msg("ui.comment") }),
  ]);
}

function listSection(title, items, format) {
  if (!items || !items.length) {
    return el('section', {}, [el('h3', {}, title), el('p', {}, '—')]);
  }
  return el('section', {}, [
    el('h3', {}, title),
    el('ul', {}, items.map((item) => el('li', {}, format(item)))),
  ]);
}
