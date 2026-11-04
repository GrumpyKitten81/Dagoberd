import { date, diagnostic, builtinLabel, statusLabel, msg, joinMessages } from './i18n.js';
import { append, clear, el, formatAmount, notify } from './dom.js';
import { api } from './store.js';

export async function renderBulkInspector(root, transactionIds, callbacks) {
  let dirty = false;
  let draft = {};

  const close = (force = false) => {
    if (!force && dirty && !window.confirm(msg("ui.discard_unsaved_bulk_changes"))) return false;
    callbacks.close();
    return true;
  };
  callbacks.registerClose(close);

  const loadEditor = async () => {
    append(clear(root), el('p', { className: 'inspector-loading' }, msg("ui.checking_selection")));
    root.hidden = false;
    try {
      const context = await api('/bulk/context', {
        method: 'POST',
        body: JSON.stringify({ transaction_ids: transactionIds }),
      });
      renderEditor(context);
    } catch (error) {
      notify(error.message, 'error');
      close(true);
    }
  };

  const renderEditor = (context) => {
    const collectors = [];
    const form = el('div', { className: 'bulk-form' }, [selectionSummary(context)]);
    append(form, categoryField(context, draft, collectors));
    append(form, tagsField(context, draft, collectors));
    append(form, merchantField(context, draft, collectors));
    const dimensions = Object.entries(context.options.dimensions);
    for (const [code, values] of dimensions) {
      append(form, dimensionField(code, values, context, draft, collectors));
    }
    if (!dimensions.length) append(form, disabledField(msg("ui.dimensions"), context.disabled.dimensions));
    append(form, noteField(context, draft, collectors));
    append(form, statusField(context, draft, collectors));
    form.addEventListener('input', () => { dirty = true; });
    form.addEventListener('change', () => { dirty = true; });

    const previewButton = el('button', {
      type: 'button',
      className: 'primary',
      onclick: async () => {
        try {
          const parts = collectors.map((collect) => collect()).filter(Boolean);
          const patch = mergePatchParts(parts);
          if (!Object.keys(patch).length) {
            notify(msg("ui.enable_at_least_one_field_to_edit"), 'warning');
            return;
          }
          draft = patch;
          const preview = await api('/bulk/preview', {
            method: 'POST',
            body: JSON.stringify({
              action: 'edit_fields', transaction_ids: transactionIds, payload: patch,
            }),
          });
          renderPreview(context, preview);
        } catch (error) {
          notify(error.message, 'error');
        }
      },
    }, msg("ui.preview"));
    append(clear(root),
      inspectorHeader(msg("ui.edit_transactions", { count: context.transaction_count }), () => close()),
      form,
      el('footer', { className: 'bulk-inspector-actions' }, [previewButton]),
    );
  };

  const renderPreview = (context, preview) => {
    const changes = preview.field_changes.length
      ? el('ul', { className: 'bulk-change-list' }, preview.field_changes.map((change) =>
        el('li', {}, [
          el('strong', {}, fieldLabel(change.field)),
          msg("ui.changed_unchanged", { changed: change.affected_count, unchanged: change.unchanged_count }),
        ])))
      : el('p', {}, msg("ui.no_effective_changes"));
    const warnings = preview.warnings.length
      ? el('ul', { className: 'bulk-warnings' }, preview.warnings.map((warning) => el('li', {}, diagnostic(warning))))
      : null;
    const apply = el('button', {
      type: 'button',
      className: 'primary',
      disabled: preview.changed_transaction_count === 0,
      onclick: async () => {
        try {
          const result = await api(`/bulk/${preview.preview_id}/run`, {
            method: 'POST',
            body: JSON.stringify({ fingerprint: preview.version_fingerprint }),
          });
          dirty = false;
          await callbacks.applied(result.updated_rows);
          close(true);
          notify(msg("ui.transactions_updated", { count: result.changed }));
        } catch (error) {
          if (error.code === 'version_conflict') {
            await callbacks.conflict(error.details?.transaction_ids || []);
            notify(msg("ui.changed_transactions_were_reloaded_review_the_preview_again"), 'warning');
            dirty = true;
            await loadEditor();
          } else {
            notify(error.message, 'error');
          }
        }
      },
    }, msg("ui.apply_changes"));
    append(clear(root),
      inspectorHeader(msg("ui.confirm_bulk_changes"), () => close()),
      el('div', { className: 'bulk-preview' }, [
        el('p', {}, msg("ui.of_transactions_will_change", { count: preview.changed_transaction_count, total: context.transaction_count }, context.transaction_count)),
        changes,
        warnings,
        el('p', { className: 'fingerprint', title: preview.version_fingerprint },
          `Fingerprint ${preview.version_fingerprint.slice(0, 16)}…`),
      ]),
      el('footer', { className: 'bulk-inspector-actions' }, [
        el('button', { type: 'button', onclick: () => renderEditor(context) }, msg("ui.back")),
        apply,
      ]),
    );
  };

  await loadEditor();
}

function mergePatchParts(parts) {
  const patch = {};
  for (const part of parts) {
    if (part.dimensions) patch.dimensions = { ...(patch.dimensions || {}), ...part.dimensions };
    else Object.assign(patch, part);
  }
  return patch;
}

function inspectorHeader(title, close) {
  return el('header', {}, [
    el('div', {}, [el('p', { className: 'eyebrow' }, msg("ui.bulk_inspector")), el('h2', {}, title)]),
    el('button', { type: 'button', className: 'icon-button', title: msg("ui.close_inspector"), onclick: close }, '×'),
  ]);
}

function selectionSummary(context) {
  return el('dl', { className: 'bulk-summary' }, [
    el('dt', {}, msg("ui.accounts")), el('dd', {}, context.accounts.map((account) => account.name).join(', ')),
    el('dt', {}, msg("ui.period")), el('dd', {}, joinMessages([date(context.booking_from), date(context.booking_to)], ' – ')),
    el('dt', {}, msg("ui.total")), el('dd', {}, joinMessages(context.amount_totals.map((total) =>
      formatAmount(total.amount_minor, total.currency)), ' · ')),
  ]);
}

function categoryField(context, draft, collectors) {
  const profile = choice(Object.keys(context.options.category).map((code) => [code, code]), draft.category?.profile || '', msg("ui.select_profile"));
  const category = el('select');
  const fill = () => {
    append(clear(category), el('option', { value: '' }, msg("ui.select_category")));
    for (const item of context.options.category[profile.value] || []) {
      append(category, el('option', { value: item.code }, builtinLabel(item.code, item.name)));
    }
    if (draft.category?.profile === profile.value) category.value = draft.category.category;
  };
  profile.addEventListener('change', fill);
  fill();
  const field = editableField(msg("ui.category"), currentText(context.current.category), [profile, category], Boolean(draft.category));
  disableUnavailable(field, context.disabled.category);
  collectors.push(() => field.enabled.checked && profile.value && category.value
    ? { category: { profile: profile.value, category: category.value } } : null);
  return field.node;
}

function tagsField(context, draft, collectors) {
  const add = multiSelect(context.options.tags, draft.tags?.add || []);
  const remove = multiSelect(context.options.tags, draft.tags?.remove || []);
  const currentParts = [
    context.current.tags.common.length ? msg("ui.common", { tags: context.current.tags.common.join(', ') }) : '',
    context.current.tags.partial.length ? msg("ui.partial", { tags: context.current.tags.partial.join(', ') }) : '',
  ].filter(Boolean);
  const current = currentParts.length ? joinMessages(currentParts, ' · ') : msg("ui.empty");
  const field = editableField(msg("ui.tags"), current, [labelled(msg("ui.add"), add), labelled(msg("ui.remove"), remove)], Boolean(draft.tags));
  disableUnavailable(field, context.disabled.tags);
  collectors.push(() => field.enabled.checked
    ? { tags: { add: selected(add), remove: selected(remove) } } : null);
  return field.node;
}

function merchantField(context, draft, collectors) {
  const mode = choice([['set', msg("ui.set")], ['clear', msg("ui.remove")]], draft.merchant?.mode || 'set');
  const value = choice(context.options.merchants.map((name) => [name, name]), draft.merchant?.value || '', msg("ui.select_merchant"));
  const field = editableField(msg("ui.merchant"), currentText(context.current.merchant), [mode, value], Boolean(draft.merchant));
  const sync = () => { value.disabled = !field.enabled.checked || mode.value === 'clear'; };
  mode.addEventListener('change', sync);
  field.enabled.addEventListener('change', sync);
  sync();
  collectors.push(() => field.enabled.checked
    ? { merchant: { mode: mode.value, ...(mode.value === 'set' ? { value: value.value } : {}) } } : null);
  return field.node;
}

function dimensionField(code, values, context, draft, collectors) {
  const saved = draft.dimensions?.[code];
  const mode = choice([['set', msg("ui.set")], ['clear', msg("ui.remove")]], saved?.mode || 'set');
  const value = choice(values.map((item) => [item.code, item.name]), saved?.value || '', msg("ui.select_value"));
  const field = editableField(code, currentText(context.current.dimensions[code]), [mode, value], Boolean(saved));
  const sync = () => { value.disabled = !field.enabled.checked || mode.value === 'clear'; };
  mode.addEventListener('change', sync);
  field.enabled.addEventListener('change', sync);
  sync();
  collectors.push(() => field.enabled.checked
    ? { dimensions: { [code]: { mode: mode.value, ...(mode.value === 'set' ? { value: value.value } : {}) } } } : null);
  return field.node;
}

function noteField(context, draft, collectors) {
  const mode = choice([['replace', msg("ui.replace")], ['append', msg("ui.append")], ['clear', msg("ui.delete")]], draft.note?.mode || 'replace');
  const text = el('textarea', { maxLength: 2000, rows: 3, value: draft.note?.text || '' });
  const field = editableField(msg("ui.note"), currentText(context.current.note), [mode, text], Boolean(draft.note));
  const sync = () => { text.disabled = !field.enabled.checked || mode.value === 'clear'; };
  mode.addEventListener('change', sync);
  field.enabled.addEventListener('change', sync);
  sync();
  collectors.push(() => field.enabled.checked
    ? { note: { mode: mode.value, ...(mode.value !== 'clear' ? { text: text.value } : {}) } } : null);
  return field.node;
}

function statusField(context, draft, collectors) {
  const value = choice(context.options.statuses.map((status) => [status, statusLabel(status)]), draft.status?.value || '', msg("ui.target_status"));
  const field = editableField(msg("ui.booking_status"), currentText(context.current.status, true), [value], Boolean(draft.status));
  if (!context.options.statuses.length) {
    disableUnavailable(field, context.disabled.status);
  }
  collectors.push(() => field.enabled.checked && value.value ? { status: { value: value.value } } : null);
  return field.node;
}

function editableField(title, current, controls, checked) {
  const body = el('div', { className: 'bulk-field-controls' }, controls);
  const enabled = el('input', { type: 'checkbox', checked, ariaLabel: msg("bulk.edit_field", { title: title }) });
  const sync = () => body.querySelectorAll('input,select,textarea').forEach((control) => { control.disabled = !enabled.checked; });
  enabled.addEventListener('change', sync);
  const node = el('section', { className: 'bulk-field' }, [
    el('header', {}, [el('label', {}, [enabled, el('strong', {}, title)]), el('small', {}, current)]),
    body,
  ]);
  sync();
  return { node, enabled };
}

function disableUnavailable(field, reason) {
  if (!reason) return;
  field.enabled.checked = false;
  field.enabled.disabled = true;
  field.node.querySelectorAll('.bulk-field-controls input, .bulk-field-controls select, .bulk-field-controls textarea')
    .forEach((control) => { control.disabled = true; });
  append(field.node, el('p', { className: 'field-hint' }, diagnostic(reason)));
}

function disabledField(title, reason) {
  const field = editableField(title, msg("ui.unavailable"), [], false);
  disableUnavailable(field, reason);
  return field.node;
}

function currentText(field, isStatus = false) {
  if (!field || field.state === 'empty') return msg("ui.empty");
  if (field.state === 'mixed') return msg("ui.mixed");
  if (typeof field.value === 'object') return `${field.value.profile}: ${field.value.category}`;
  return isStatus ? statusLabel(field.value) : field.value;
}

function multiSelect(values, current) {
  return el('select', { multiple: true, size: Math.min(4, Math.max(2, values.length)) }, values.map((value) =>
    el('option', { value, selected: current.includes(value) }, value)));
}

function selected(control) {
  return [...control.selectedOptions].map((option) => option.value);
}

function choice(values, current, placeholder = '') {
  const control = el('select');
  if (placeholder) append(control, el('option', { value: '' }, placeholder));
  for (const [value, label] of values) append(control, el('option', { value, selected: value === current }, label));
  control.value = current;
  return control;
}

function labelled(title, control) {
  return el('label', {}, [el('span', {}, title), control]);
}

function fieldLabel(field) {
  if (field.startsWith('dimension:')) return field.slice('dimension:'.length);
  return { category: msg("ui.category"), tags: msg("ui.tags"), merchant: msg("ui.merchant"), note: msg("ui.note"), status: msg("ui.status") }[field] || field;
}
