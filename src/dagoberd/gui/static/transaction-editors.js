import { builtinLabel, msg, statusLabel } from './i18n.js';
import { el, notify, select, append } from './dom.js';
import { api, state } from './store.js';

// DESIGN: one place applies a mutation, tracks the row version and resolves conflicts.
export async function mutate(row, request, onReload) {
  try {
    const result = await api(request.path, {
      method: request.method || 'POST',
      body: JSON.stringify({ ...request.body, expected_version: Number(row.dataset.version) }),
    });
    if (result.version) {
      row.dataset.version = result.version;
    }
    row.classList.remove('conflict');
    row.classList.add('saved');
    setTimeout(() => row.classList.remove('saved'), 1500);
    return result;
  } catch (error) {
    if (error.code === 'version_conflict') {
      row.classList.add('conflict');
      notify(msg("ui.the_transaction_changed_and_is_being_reloaded"), 'warning');
      await onReload(error.details?.current);
    } else {
      row.classList.add('failed');
      notify(error.message, 'error');
    }
    return null;
  }
}

export function categoryEditor(item, row, reload) {
  const entries = Object.entries(state.options.category).map(([profile, categories]) => ({
    group: profile,
    items: categories.map((category) => ({
      value: `${profile}:${category.code}`,
      label: builtinLabel(category.code, category.name),
    })),
  }));
  const current = item.classification_profile
    ? `${item.classification_profile}:${item.category_code}`
    : '';
  return select(entries, current, async (event) => {
    const [profile_code, category_code] = event.target.value.split(':');
    if (!category_code) {
      event.target.value = current;
      return;
    }
    await mutate(
      row,
      { path: `/transactions/${item.id}/category`, body: { profile_code, category_code } },
      reload,
    );
  });
}

export function tagEditor(item, row, reload) {
  const cell = el('div', { className: 'tags' });
  for (const tag of item.tags) {
    append(cell,
      el('span', { className: 'chip' }, [
        tag,
        el(
          'button',
          {
            type: 'button',
            title: msg("ui.remove_tag"),
            onclick: async () => {
              await mutate(
                row,
                { path: `/transactions/${item.id}/tags`, body: { tag, remove: true } },
                reload,
              );
              await reload();
            },
          },
          '×',
        ),
      ]),
    );
  }
  const input = el('input', {
    list: 'tag-options',
    placeholder: msg("tags.add"),
    className: 'tag-input',
    onchange: async (event) => {
      const tag = event.target.value.trim();
      if (!tag) {
        return;
      }
      if (!state.metadata.tags.some((entry) => entry.name.toLowerCase() === tag.toLowerCase())) {
        await api('/tags', { method: 'POST', body: JSON.stringify({ name: tag }) });
        state.metadata.tags = await api('/meta').then((meta) => meta.tags);
      }
      await mutate(row, { path: `/transactions/${item.id}/tags`, body: { tag } }, reload);
      await reload();
    },
  });
  append(cell, input);
  return cell;
}

export function merchantEditor(item, row, reload) {
  return el('input', {
    list: 'merchant-options',
    value: item.merchant || '',
    className: 'merchant-input',
    onchange: async (event) => {
      const merchant = event.target.value.trim();
      if (!merchant || merchant === item.merchant) {
        return;
      }
      if (!state.metadata.merchants.some((entry) => entry.canonical_name === merchant)) {
        await api('/merchants', { method: 'POST', body: JSON.stringify({ name: merchant }) });
        state.metadata.merchants = await api('/meta').then((meta) => meta.merchants);
      }
      await mutate(row, { path: `/transactions/${item.id}/merchant`, body: { merchant } }, reload);
    },
  });
}

export function dimensionEditor(item, row, dimensionCode, reload) {
  const values = state.options.dimension_value[dimensionCode] || [];
  const entries = values.map((value) => ({ value: value.code, label: value.name }));
  return select(entries, item.dimensions[dimensionCode] || '', async (event) => {
    await mutate(
      row,
      {
        path: `/transactions/${item.id}/dimensions`,
        body: { dimension_code: dimensionCode, value_code: event.target.value || null },
      },
      reload,
    );
  });
}

export function noteEditor(item, row, reload) {
  return el('input', {
    value: item.note || '',
    className: 'note-input',
    maxLength: 2000,
    onchange: async (event) => {
      await mutate(row, { path: `/transactions/${item.id}/note`, body: { note: event.target.value } }, reload);
    },
  });
}

export function statusEditor(item, row, reload) {
  const targets = state.metadata.status_transitions[item.booking_status] || [];
  if (!targets.length) {
    return el('span', {}, statusLabel(item.booking_status));
  }
  const entries = targets.map((status) => ({ value: status, label: statusLabel(status) }));
  return select(entries, '', async (event) => {
    if (!event.target.value) {
      return;
    }
    await mutate(
      row,
      { path: `/transactions/${item.id}/status`, body: { status: event.target.value } },
      reload,
    );
    await reload();
  }, { placeholder: statusLabel(item.booking_status) });
}
