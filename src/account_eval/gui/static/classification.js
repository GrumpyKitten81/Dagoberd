import { date, builtinLabel, msg, bind, number, statusLabel } from './i18n.js';
import { append, clear, dialog, el, notify } from './dom.js';
import { api, state } from './store.js';

const STAGE_LABELS = {
  match: msg('classification.match'),
  no_match: msg("ui.no_match"),
  not_reached: msg("ui.not_reached"),
  disabled: msg('classification.disabled'),
  profile_inactive: msg("ui.profile_is_not_active_for_this_account"),
  unavailable: msg("ui.no_provider_configured"),
  would_suggest: msg("ui.would_create_a_suggestion"),
};

export async function renderClassification(root) {
  clear(root);
  if (!state.accountId) {
    append(root, el('p', {}, msg("ui.select_an_account_above_the_classification_pipeline_is")));
    return;
  }
  const [configuredProfiles, stages, rules, hints, annotations, llm] = await Promise.all([
    api(`/accounts/${state.accountId}/profiles`),
    api(`/accounts/${state.accountId}/stages`),
    api(`/rules?account_id=${encodeURIComponent(state.accountId)}`),
    api(`/hints?account_id=${encodeURIComponent(state.accountId)}`),
    api('/classification/import-annotations'),
    api('/llm'),
  ]);
  const profileCodes = configuredProfiles.map((profile) => profile.code);
  append(root,
    profilePanel(configuredProfiles),
    annotationPanel(annotations),
    stagePanel(1, msg("ui.rules"), 'rule', stages.rule, rules, 'rule', profileCodes),
    historyPanel(stages.history),
    stagePanel(3, msg("ui.hints"), 'hint', stages.hint, hints, 'hint', profileCodes),
    llmPanel(stages.llm, llm, profileCodes),
    explainPanel(profileCodes),
  );
}

function availableProfileCodes(configuredCodes) {
  return configuredCodes.length ? configuredCodes : Object.keys(state.metadata.profiles);
}

function profilePanel(configuredProfiles) {
  const configured = configuredProfiles.length > 0;
  const codes = configured ? configuredProfiles.map((profile) => profile.code) : [];
  const panel = el('section', { className: 'panel' }, [el('h2', {}, msg("ui.profiles_for_this_account"))]);
  const list = el('div', { className: 'option-picker' });
  let custom = configured;
  const render = () => {
    list.replaceChildren();
    if (!custom) {
      append(list, el('p', {}, msg("ui.all_profiles_are_available_default")));
      append(list, actionButton(msg("ui.set_selection"), () => {
        codes.push(...Object.keys(state.metadata.profiles));
        custom = true;
        render();
      }));
      return;
    }
    for (const [index, code] of codes.entries()) {
      append(list, el('div', { className: 'toolbar' }, [
        el('input', { type: 'checkbox', checked: true, onchange: (event) => {
          if (!event.target.checked) codes.splice(index, 1);
          render();
        } }),
        code,
        actionButton('▲', () => {
          [codes[index - 1], codes[index]] = [codes[index], codes[index - 1]];
          render();
        }, { disabled: index === 0 }),
        actionButton('▼', () => {
          [codes[index + 1], codes[index]] = [codes[index], codes[index + 1]];
          render();
        }, { disabled: index === codes.length - 1 }),
      ]));
    }
    for (const code of Object.keys(state.metadata.profiles).filter((item) => !codes.includes(item))) {
      append(list, el('label', {}, [el('input', { type: 'checkbox', onchange: (event) => {
        if (event.target.checked) codes.push(code);
        render();
      } }), ` ${code}`]));
    }
  };
  render();
  append(panel, list, actionButton(msg("ui.save"), async () => {
    if (!custom) return;
    await api(`/accounts/${state.accountId}/profiles`, {
      method: 'PUT', body: JSON.stringify({ profile_codes: codes }),
    });
    await renderClassification(document.querySelector('#app'));
  }), actionButton(msg("ui.restore_defaults"), async () => {
    await api(`/accounts/${state.accountId}/profiles`, {
      method: 'PUT', body: JSON.stringify({ profile_codes: [] }),
    });
    await renderClassification(document.querySelector('#app'));
  }, { hidden: !configured }));
  return panel;
}

function annotationPanel(items) {
  return el('section', { className: 'panel' }, [
    el('h2', {}, msg("ui.0_import_annotations")),
    el('p', {}, msg("ui.read_only_edit_account_eval_local_toml_to")),
    ...items.map((item) => el(
      'p',
      {},
      msg("ui.aliases", { name: item.name, column: item.category_column || msg("ui.no_category_column"), profile: item.category_profile || '—', count: item.aliases }),
    )),
  ]);
}

function historyPanel(enabled) {
  return el('section', { className: 'panel' }, [
    stageSwitch(msg("ui.2_history"), 'history', enabled),
    el(
      'p',
      {},
      msg("ui.uses_the_category_of_a_confirmed_previous_transaction")
        + msg("ui.counterparty_and_purpose_text_no_threshold_or_similarity"),
    ),
  ]);
}

function stagePanel(number, title, stage, enabled, items, kind, activeProfiles) {
  const panel = el('section', { className: 'panel' }, [stageSwitch(msg("classification.stage_title", { number: number, title: title }), stage, enabled)]);
  const description = kind === 'rule'
    ? msg("ui.checks_only_the_purpose_lower_rank_first")
    : msg("ui.checks_purpose_counterparty_and_reference");
  append(panel, el('p', {}, description));
  const profileFilter = el('select', {}, [
    el('option', { value: '' }, msg("ui.all_profiles")),
    ...availableProfileCodes(activeProfiles).map((code) => el('option', { value: code }, code)),
  ]);
  const scopedOnly = el('input', { type: 'checkbox' });
  const body = el('tbody');
  const refresh = () => {
    const visible = items.filter((item) => (
      (!activeProfiles.length || activeProfiles.includes(item.profile_code))
      && (!profileFilter.value || item.profile_code === profileFilter.value)
      && (!scopedOnly.checked || item.account_id === state.accountId)
    ));
    body.replaceChildren(...visible.map((item) => itemRow(item, kind, activeProfiles)));
  };
  profileFilter.addEventListener('change', refresh);
  scopedOnly.addEventListener('change', refresh);
  const label = kind === 'rule' ? msg("ui.name") : msg("ui.text");
  append(panel, el('div', { className: 'toolbar' }, [
    el('label', {}, [msg("ui.profile"), profileFilter]),
    el('label', {}, [scopedOnly, msg("ui.this_account_only")]),
  ]), el('table', { className: 'transactions' }, [
    el(
      'thead',
      {},
      el(
        'tr',
        {},
        [msg("ui.profile"), msg("ui.rank"), label, msg("ui.pattern"), msg("ui.category"), msg("ui.scope"), 'aktiv', ''].map(
          (value) => el('th', {}, value),
        ),
      ),
    ),
    body,
  ]), actionButton(msg("classification.add_item", { kind: kind === 'rule' ? msg("ui.rule") : msg("ui.hint") }), () => editItem(null, kind, activeProfiles)));
  refresh();
  return panel;
}

function stageSwitch(title, stage, enabled) {
  return el('label', {}, [el('input', { type: 'checkbox', checked: enabled, onchange: async (event) => {
    try {
      await api(`/accounts/${state.accountId}/stages/${stage}`, { method: 'PUT', body: JSON.stringify({ enabled: event.target.checked }) });
      notify(msg("ui.saved", { title: title }));
    } catch (error) {
      event.target.checked = !event.target.checked;
      notify(error.message, 'error');
    }
  } }), msg("classification.stage_active", { title: title })]);
}

function itemRow(item, kind, activeProfiles) {
  return el('tr', {}, [
    el('td', {}, item.profile_code), el('td', {}, String(item.priority)), el('td', {}, item.name || item.text),
    el('td', {}, item.pattern || '—'), el('td', {}, item.category_code || '—'),
    el('td', {}, item.account_id ? msg("ui.this_account") : msg("scope.all_accounts")), el('td', {}, item.active ? msg("ui.yes") : msg("ui.no")),
    el('td', {}, [
      actionButton(msg("ui.edit"), () => editItem(item, kind, activeProfiles)),
      actionButton(msg("ui.delete"), async () => {
        if (!confirm(msg("ui.delete_entry"))) return;
        await api(`/${kind}s/${item.id}?expected_version=${item.version}`, { method: 'DELETE' });
        await renderClassification(document.querySelector('#app'));
      }),
    ]),
  ]);
}

function editItem(item, kind, activeProfiles) {
  const profileCodes = availableProfileCodes(activeProfiles);
  const profile = el(
    'select',
    {},
    profileCodes.map((code) => el(
      'option',
      { value: code, selected: code === (item?.profile_code || profileCodes[0]) },
      code,
    )),
  );
  const textKey = kind === 'rule' ? 'name' : 'text';
  const text = el('input', { value: item?.[textKey] || '', placeholder: textKey });
  const pattern = el('input', { value: item?.pattern || '', placeholder: msg("ui.pattern") });
  const category = el('select');
  const priority = el('input', { type: 'number', value: String(item?.priority ?? 100), placeholder: msg("ui.rank") });
  const scope = el('select', {}, [
    el('option', { value: '', selected: !item?.account_id }, msg("scope.all_accounts")),
    el(
      'option',
      { value: state.accountId, selected: item?.account_id === state.accountId },
      msg("ui.this_account"),
    ),
  ]);
  const active = el('input', { type: 'checkbox', checked: item?.active ?? true });
  const refillCategories = () => {
    category.replaceChildren(el('option', { value: '' }, kind === 'hint' ? msg("ui.llm_prompt_only") : msg("ui.select_category")));
    for (const entry of state.metadata.profiles[profile.value] || []) {
      append(category, el('option', { value: entry.code, selected: entry.code === item?.category_code }, builtinLabel(entry.code, entry.name)));
    }
  };
  profile.addEventListener('change', refillCategories);
  refillCategories();
  const testResult = el('p');
  const form = el('div', { className: 'toolbar' }, [
    el('label', {}, [msg("ui.profile"), profile]), el('label', {}, [textKey, text]), el('label', {}, [msg("ui.pattern"), pattern]),
    el('label', {}, [msg("ui.category"), category]),
    el('label', {}, [msg("ui.rank"), priority]),
    el('label', {}, [msg("ui.scope"), scope]),
    el('label', {}, [active, msg("ui.active")]),
  ]);
  const node = dialog(item ? msg("classification.edit_item", { kind: statusLabel(kind) }) : msg("classification.create_item", { kind: statusLabel(kind) }), el('div', {}, [form, testResult]), [
    actionButton(msg("ui.test_pattern"), async () => {
      const result = await api('/classification/rules/test', {
        method: 'POST',
        body: JSON.stringify({
          pattern: pattern.value,
          profile_code: profile.value,
          account_id: scope.value || null,
        }),
      });
      const examples = result.matches.slice(0, 3).map((row) => row.purpose).join(' · ');
      bind(testResult, 'textContent', msg("ui.matches_out_of", { count: result.matches.length, total: result.examined, examples: examples }));
    }),
    actionButton(msg("ui.save"), async () => {
      const payload = {
        profile_code: profile.value,
        [textKey]: text.value.trim(),
        pattern: pattern.value.trim() || null,
        category_code: category.value || null,
        priority: Number(priority.value),
        active: active.checked,
        account_id: scope.value || null,
      };
      if (item) {
        payload.expected_version = item.version;
        await api(`/${kind}s/${item.id}`, { method: 'PATCH', body: JSON.stringify(payload) });
      } else {
        await api(`/${kind}s`, { method: 'POST', body: JSON.stringify(payload) });
      }
      node.close();
      await renderClassification(document.querySelector('#app'));
    }),
  ]);
}

function llmPanel(enabled, llm, activeProfiles) {
  const profile = profileSelect(activeProfiles);
  return el('section', { className: 'panel' }, [
    stageSwitch(msg("ui.4_llm"), 'llm', enabled),
    el('p', {}, msg("ui.key_from", { mode: llm.mode, provider: llm.provider, model: llm.model || msg("ui.no_model"), secret: llm.api_key_secret })),
    el('p', {}, msg("ui.optional_classifier_with_separate_configuration_independent_of_the")),
    actionButton(msg("ui.test_connection"), async () => showConnection(await api('/llm/test', { method: 'POST' }))),
    el('label', {}, [msg("ui.profile"), profile]),
    actionButton(msg("ui.create_preview"), async () => {
      const preview = await api('/classification/llm/preview', {
        method: 'POST',
        body: JSON.stringify({ profile_code: profile.value, account_id: state.accountId }),
      });
      showPreview(preview);
    }),
    el('p', {}, msg("ui.creating_a_preview_first_applies_stages_1_3")),
  ]);
}

function showConnection(result) {
  dialog(msg("ui.llm_connection"), el('pre', {}, JSON.stringify(result, null, 2)));
}

function showPreview(preview) {
  const configuration = el('details', {}, [
    el('summary', {}, msg("ui.request_configuration")),
    el('pre', {}, JSON.stringify(preview.request_context, null, 2)),
  ]);
  const payloads = el('details', {}, [
    el('summary', {}, msg("ui.redacted_payloads", { count: preview.payloads.length })),
    el('pre', {}, JSON.stringify(preview.payloads, null, 2)),
  ]);
  const node = dialog(msg("ui.llm_preview"), el('div', {}, [
    el('p', {}, `${preview.provider} · ${preview.model} · ${preview.mode}`),
    el('p', {}, msg("ui.endpoint", { endpoint: preview.request_context.llm.base_url })),
    el('p', {}, msg("ui.fingerprint", { fingerprint: preview.fingerprint })), el('p', {}, msg("ui.expires", { expires: date(preview.expires_at, {dateStyle: 'medium', timeStyle: 'short'}) })), configuration, payloads,
  ]), [actionButton(msg("ui.run"), async () => {
    await api(`/classification/llm/${preview.preview_id}/run`, {
      method: 'POST',
      body: JSON.stringify({ fingerprint: preview.fingerprint }),
    });
    node.close();
    await renderClassification(document.querySelector('#app'));
  })]);
}

function explainPanel(activeProfiles) {
  const transaction = el('input', { placeholder: msg("ui.transaction_id") });
  const profile = profileSelect(activeProfiles);
  const output = el('div');
  return el('section', { className: 'panel' }, [el('h2', {}, msg("ui.explain_classification")), transaction, profile,
    actionButton(msg("ui.explain"), async () => renderExplanation(output, await explain(transaction.value, profile.value))), output]);
}

export async function explainDialog(transactionId) {
  const profiles = await api(`/accounts/${state.accountId}/profiles`);
  const profile = profileSelect(profiles.map((item) => item.code));
  const output = el('div');
  const node = dialog(
    msg("ui.explain_classification"),
    el('div', {}, [profile, output]),
    [actionButton(msg("ui.explain"), async () => renderExplanation(
      output,
      await explain(transactionId, profile.value),
    ))],
  );
  await renderExplanation(output, await explain(transactionId, profile.value));
  return node;
}

function profileSelect(activeProfiles) {
  return el('select', {}, availableProfileCodes(activeProfiles).map((code) => el('option', { value: code }, code)));
}

async function explain(transactionId, profileCode) {
  return api('/classification/explain', {
    method: 'POST',
    body: JSON.stringify({ transaction_id: transactionId, profile_code: profileCode }),
  });
}

function renderExplanation(root, stages) {
  root.replaceChildren(...stages.map((stage) => el('article', { className: `classification-stage ${stage.status}` }, [
    el('strong', {}, msg("classification.stage_result", { stage: statusLabel(stage.stage), status: STAGE_LABELS[stage.status] || stage.status })),
    stage.category_code ? el('p', {}, msg("classification.category", { category: stage.category_code })) : null,
    stage.rationale ? el('p', {}, stage.rationale) : null,
    stage.detail?.pattern ? el('p', {}, msg("classification.pattern", { pattern: stage.detail.pattern })) : null,
    stage.model ? el('p', {}, msg("ui.model", { model: stage.model })) : null,
    stage.active_profiles ? el('p', {}, msg("ui.active_profiles", { profiles: stage.active_profiles.join(', ') || msg("ui.all_default") })) : null,
    stage.status === 'no_match' ? el('p', {}, msg("ui.checked_no_match")) : null,
  ])));
}

function actionButton(label, operation, options = {}) {
  return el('button', { type: 'button', ...options, onclick: async () => {
    try {
      await operation();
    } catch (error) {
      notify(error.message, 'error');
    }
  } }, label);
}
