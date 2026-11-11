import { readPreference, writePreference } from './browser-storage.js';
import { msg, joinMessages, number, date } from './i18n.js';
import { append, clear, el, formatAmount, svg } from './dom.js';
import { api, ApiError, state } from './store.js';

const PERIOD_STORAGE_KEY = 'dagoberd.dashboard-period-v1';
const PERIODS = [
  ['3m', msg("ui.3_months")],
  ['ytd', msg("ui.this_year")],
  ['12m', msg("ui.12_months")],
  ['all', msg("ui.all_time")],
];
const storedPeriod = readPreference(PERIOD_STORAGE_KEY);
let selectedPeriod = PERIODS.some(([value]) => value === storedPeriod) ? storedPeriod : '12m';

export async function renderOverview(root) {
  clear(root);
  const shell = el('section', { className: 'overview-page', ariaBusy: 'true' }, [
    overviewHeader(),
    el('div', { className: 'overview-loading' }, msg("ui.calculating_overview")),
  ]);
  append(root, shell);
  try {
    const parameters = new URLSearchParams({ period: selectedPeriod });
    state.accountIds.forEach((id) => parameters.append('account_id', id));
    const data = await api(`/dashboard?${parameters}`);
    if (!shell.isConnected) return false;
    shell.ariaBusy = 'false';
    shell.replaceChildren(
      el('div', { className: 'overview-topline' }, [
        overviewHeader(),
        qualityBanner(data.quality),
      ]),
      kpis(data.summary, data.quality),
      el('div', { className: 'dashboard-workspace' }, [
        accountOverview(data.accounts, data.summary.currency),
        el('div', { className: 'dashboard-analytics' }, [
          chartPanel(
            msg("ui.net_worth"),
            msg("ui.latest_balance_at_each_date"),
            lineChart(data.net_worth_series, data.summary.currency),
            netWorthTable(data.net_worth_series, data.summary.currency),
            'net-worth-panel',
          ),
          el('div', { className: 'dashboard-secondary' }, [
            chartPanel(
              msg("ui.cashflow"),
              msg("ui.monthly_income_and_expenses"),
              cashflowChart(data.cashflow_series),
              cashflowTable(data.cashflow_series),
              'cashflow-panel',
            ),
            chartPanel(
              msg("ui.composition"),
              msg("ui.assets_and_liabilities"),
              compositionChart(data.composition),
              compositionTable(data.composition),
              'composition-panel',
            ),
          ]),
        ]),
      ]),
    );
    return true;
  } catch (error) {
    if (!shell.isConnected) return false;
    shell.ariaBusy = 'false';
    shell.replaceChildren(overviewHeader(), overviewError(error, root));
    return false;
  }
}

function overviewError(error, root) {
  const isApiError = error instanceof ApiError;
  const hint = error.code === 'unauthorized' || error.status === 401
    ? 'dashboard.reopen_hint'
    : ['workspace_required', 'invalid_workspace'].includes(error.code)
      ? 'dashboard.workspace_hint'
      : isApiError && error.kind === 'network'
        ? 'dashboard.network_hint'
        : 'dashboard.retry_hint';
  const panel = el('section', { className: 'ui-card panel overview-error', role: 'alert' }, [
    el('h2', {}, msg('ui.the_overview_could_not_be_loaded')),
    el('p', {}, isApiError ? error.message : msg('dashboard.render_error')),
    el('p', {}, msg(hint)),
    el('button', {
      type: 'button',
      className: 'primary',
      onclick: (event) => {
        event.currentTarget.disabled = true;
        renderOverview(root);
      },
    }, msg('dashboard.retry')),
  ]);
  if (isApiError) {
    append(panel, el('details', {}, [
      el('summary', {}, msg('dashboard.diagnostics')),
      el('p', {}, msg('dashboard.request_details', {
        endpoint: 'GET /api/dashboard', status: error.status ?? '—',
        code: error.code && /^[a-z][a-z0-9_]{0,63}$/.test(error.code) ? error.code : error.kind,
      })),
    ]));
  }
  return panel;
}

function overviewHeader() {
  return el('header', { className: 'overview-header' }, [
    el('div', {}, [
      el('p', { className: 'eyebrow' }, msg("ui.your_finances_at_a_glance")),
      el('h1', {}, msg("ui.overview")),
    ]),
    el('div', { className: 'period-switch', ariaLabel: msg("ui.chart_period") }, PERIODS.map(([value, label]) =>
      el('button', {
        type: 'button',
        className: selectedPeriod === value ? 'active' : '',
        ariaPressed: String(selectedPeriod === value),
        onclick: () => {
          selectedPeriod = value;
          writePreference(PERIOD_STORAGE_KEY, value);
          renderOverview(document.querySelector('#app'));
        },
      }, label)
    )),
  ]);
}

function qualityBanner(quality) {
  const issues = [];
  if (quality.missing_display_currency) issues.push(msg("ui.display_currency_is_missing"));
  if (quality.missing_balance_count) issues.push(msg("ui.without_a_balance", { count: quality.missing_balance_count }));
  if (quality.stale_balance_count) issues.push(msg("ui.stale_balances", { count: quality.stale_balance_count }));
  if (quality.missing_conversion_count) issues.push(msg("ui.missing_exchange_rates", { count: quality.missing_conversion_count }));
  if (quality.open_review_count) issues.push(msg("ui.open_reviews", { count: number(quality.open_review_count) }));
  if (!issues.length) return null;
  return el('div', { className: 'quality-banner', role: 'status' }, [
    el('strong', {}, msg("ui.data_quality")),
    el('span', {}, joinMessages(issues, ' · ')),
  ]);
}

function kpis(summary, quality) {
  const currency = summary.currency;
  const cashflowNote = summary.current_month.complete
    ? msg("ui.in_out", { income: formatOptional(summary.current_month.income_minor, currency), expenses: formatOptional(Math.abs(summary.current_month.expense_minor), currency) })
    : '';
  const pendingNote = quality.pending_transaction_count
    ? msg("ui.pending", { count: quality.pending_transaction_count })
    : '';
  return el('section', { className: 'ui-card kpi-grid', ariaLabel: msg("ui.financial_indicators") }, [
    kpi(msg("ui.net_worth"), summary.net_worth_minor, currency, summary.complete),
    kpi(msg("ui.assets"), summary.assets_minor, currency, summary.complete, 'positive'),
    kpi(msg("ui.liabilities"), summary.liabilities_minor, currency, summary.complete, 'negative'),
    kpi(
      msg("ui.current_month_cashflow"),
      summary.current_month.net_minor,
      currency,
      summary.current_month.complete,
      summary.current_month.net_minor !== null && summary.current_month.net_minor < 0 ? 'negative' : 'positive',
      joinMessages([cashflowNote, pendingNote].filter(Boolean), ' · '),
    ),
  ]);
}

function kpi(label, value, currency, complete, tone = '', note = '') {
  return el('article', { className: `kpi ${tone}` }, [
    el('span', {}, label),
    el('strong', {}, complete ? formatOptional(value, currency) : '—'),
    !complete || note ? el('small', {}, complete ? note : msg("ui.incomplete")) : null,
  ]);
}

function chartPanel(title, description, chart, table, className = '') {
  return el('section', { className: `ui-card chart-panel panel ${className}` }, [
    el('header', {}, [el('h2', {}, title), el('p', {}, description)]),
    chart,
    el('details', { className: 'chart-data' }, [
      el('summary', {}, msg("ui.values_as_a_table")),
      table,
    ]),
  ]);
}

function lineChart(series, currency) {
  const values = series.filter((item) => item.net_worth_minor !== null);
  if (values.length < 2) return chartEmpty(msg("ui.not_enough_complete_balances_for_this_scope"));
  const width = 760;
  const height = 160;
  const padding = 18;
  const min = Math.min(...values.map((item) => item.net_worth_minor));
  const max = Math.max(...values.map((item) => item.net_worth_minor));
  const spread = Math.max(1, max - min);
  const x = (index) => padding + index * ((width - padding * 2) / Math.max(1, values.length - 1));
  const y = (value) => height - padding - ((value - min) / spread) * (height - padding * 2);
  const points = values.map((item, index) => [x(index), y(item.net_worth_minor)]);
  const path = points.map(([px, py], index) => `${index ? 'L' : 'M'} ${px} ${py}`).join(' ');
  const area = `${path} L ${points.at(-1)[0]} ${height - padding} L ${points[0][0]} ${height - padding} Z`;
  return svg('svg', { className: 'chart', viewBox: `0 0 ${width} ${height}`, role: 'img', ariaLabel: msg("ui.net_worth_over_time") }, [
    svg('path', { className: 'chart-area', d: area }),
    svg('path', { className: 'chart-line', d: path }),
    ...values.map((item, index) => svg('circle', {
      className: 'chart-point',
      cx: points[index][0],
      cy: points[index][1],
      r: 4,
      tabIndex: 0,
      role: 'button',
      ariaLabel: msg("dashboard.point_value", { date: date(item.date), amount: formatOptional(item.net_worth_minor, currency) }),
      onclick: () => drillIntoDate(item.date),
      onkeydown: (event) => {
        if (event.key === 'Enter' || event.key === ' ') drillIntoDate(item.date);
      },
    }, svg('title', {}, msg("dashboard.point_value", { date: date(item.date), amount: formatOptional(item.net_worth_minor, currency) })))),
  ]);
}

function cashflowChart(series) {
  if (!series.length) return chartEmpty(msg("ui.no_transactions_in_the_selected_period"));
  if (new Set(series.map((item) => item.currency)).size > 1) {
    return chartEmpty(msg("ui.multiple_currencies_cannot_be_charted_together_without_a"));
  }
  const usable = series.filter((item) => item.complete !== false);
  if (!usable.length) return chartEmpty(msg("ui.cashflow_cannot_be_aggregated_because_exchange_rates_are"));
  const width = 760;
  const height = 140;
  const padding = 18;
  const max = Math.max(1, ...usable.flatMap((item) => [item.income_minor, Math.abs(item.expense_minor)]));
  const groupWidth = (width - padding * 2) / usable.length;
  const barWidth = Math.max(2, Math.min(18, groupWidth * 0.32));
  return svg('svg', { className: 'chart', viewBox: `0 0 ${width} ${height}`, role: 'img', ariaLabel: msg("ui.monthly_income_and_expenses") }, [
    svg('line', { className: 'chart-axis', x1: padding, x2: width - padding, y1: height - padding, y2: height - padding }),
    ...usable.flatMap((item, index) => {
      const center = padding + groupWidth * index + groupWidth / 2;
      const incomeHeight = item.income_minor / max * (height - padding * 2);
      const expenseHeight = Math.abs(item.expense_minor) / max * (height - padding * 2);
      const open = () => drillIntoMonth(item.month);
      return [
        chartBar(center - barWidth - 1, height - padding - incomeHeight, barWidth, incomeHeight, 'income', msg("dashboard.income_point", { month: item.month, amount: formatOptional(item.income_minor, item.currency) }), open),
        chartBar(center + 1, height - padding - expenseHeight, barWidth, expenseHeight, 'expense', msg("dashboard.expense_point", { month: item.month, amount: formatOptional(Math.abs(item.expense_minor), item.currency) }), open),
      ];
    }),
  ]);
}

function chartBar(x, y, width, height, tone, label, open) {
  return svg('rect', {
    className: `chart-bar ${tone}`,
    x,
    y,
    width,
    height: Math.max(1, height),
    tabIndex: 0,
    role: 'button',
    ariaLabel: label,
    onclick: open,
    onkeydown: (event) => {
      if (event.key === 'Enter' || event.key === ' ') open();
    },
  }, svg('title', {}, label));
}

function compositionChart(items) {
  if (!items.length) return chartEmpty(msg("ui.no_account_balances_available_for_valuation"));
  if (new Set(items.map((item) => item.currency)).size > 1) {
    return chartEmpty(msg("ui.multiple_currencies_cannot_be_compared_without_a_display"));
  }
  const maximum = Math.max(1, ...items.map((item) => item.amount_minor));
  return el('div', { className: 'composition-chart', role: 'img', ariaLabel: msg("ui.composition_by_account") }, items.map((item) =>
    el('button', {
      type: 'button',
      className: `composition-row ${item.section}`,
      onclick: () => openAccount(item.account_id),
    }, [
      el('span', { className: 'composition-label' }, item.label),
      el('span', { className: 'composition-track' }, el('span', { style: `width:${Math.max(2, item.amount_minor / maximum * 100)}%` })),
      el('strong', {}, formatOptional(item.amount_minor, item.currency)),
    ])
  ));
}

function accountOverview(accounts, displayCurrency) {
  const labels = { assets: msg("ui.assets"), liabilities: msg("ui.liabilities"), missing: msg("ui.no_current_balance") };
  return el('section', { className: 'ui-card accounts-panel panel' }, [
    el('header', {}, [el('h2', {}, msg("ui.accounts")), el('p', {}, msg("ui.in_scope", { count: accounts.length }))]),
    el('div', { className: 'account-groups' }, Object.entries(labels).map(([section, label]) => {
      const rows = accounts.filter((item) => item.section === section);
      if (!rows.length) return null;
      return el('section', { className: 'account-section' }, [
        el('h3', {}, [label, el('span', {}, String(rows.length))]),
        el('table', { className: 'ui-table account-table' }, [
          el('thead', {}, el('tr', {}, [
            el('th', {}, msg("ui.account")), el('th', {}, msg("ui.balance")), el('th', {}, msg("ui.balance_date")), el('th', {}, msg("ui.period_change")),
          ])),
          el('tbody', {}, rows.map((item) => el('tr', {}, [
            el('td', {}, el('button', {
              type: 'button', className: 'link-button', title: item.provider_name,
              onclick: () => openAccount(item.account_id),
            }, item.account_name)),
            el('td', { className: item.section === 'liabilities' ? 'negative amount' : 'amount' }, item.balance_minor === null ? '—' : formatOptional(Math.abs(item.balance_minor), item.currency)),
            el('td', {}, item.balance_date ? [date(item.balance_date), item.stale ? el('span', { className: 'ui-badge warning stale', title: msg("ui.days_old", { count: item.age_days }) }, msg("ui.stale")) : null] : '—'),
            el('td', { className: 'amount' }, item.period_change_minor === null ? '—' : formatOptional(item.period_change_minor, displayCurrency)),
          ]))),
        ]),
      ]);
    })),
  ]);
}

function chartEmpty(message) {
  return el('div', { className: 'chart-empty' }, message);
}

function netWorthTable(series, currency) {
  return simpleDataTable([msg("ui.date"), msg("ui.net_worth"), msg("ui.complete")], series.map((item) => [
    date(item.date),
    item.net_worth_minor === null ? '—' : formatOptional(item.net_worth_minor, currency),
    item.complete ? msg("ui.yes") : msg("ui.no"),
  ]));
}

function cashflowTable(series) {
  return simpleDataTable([msg("ui.month"), msg("ui.income"), msg("ui.expenses"), msg("ui.net")], series.map((item) => [
    item.month,
    formatOptional(item.income_minor, item.currency),
    formatOptional(Math.abs(item.expense_minor), item.currency),
    formatOptional(item.net_minor, item.currency),
  ]));
}

function compositionTable(items) {
  return simpleDataTable([msg("ui.account"), msg("ui.section"), msg("ui.amount")], items.map((item) => [
    item.label,
    item.section === 'liabilities' ? msg("ui.liability") : msg("ui.assets"),
    formatOptional(item.amount_minor, item.currency),
  ]));
}

function simpleDataTable(headers, rows) {
  return el('table', { className: 'ui-table data-table' }, [
    el('thead', {}, el('tr', {}, headers.map((header) => el('th', {}, header)))),
    el('tbody', {}, rows.map((row) => el('tr', {}, row.map((value) => el('td', {}, value))))),
  ]);
}

function formatOptional(value, currency) {
  return value === null || value === undefined || !currency ? '—' : formatAmount(value, currency);
}

function openAccount(accountId) {
  window.dispatchEvent(new CustomEvent('dagoberd:navigate', { detail: {
    tab: 'transactions', accountIds: [accountId], filters: {},
  } }));
}

function drillIntoMonth(month) {
  const [year, number] = month.split('-').map(Number);
  const last = new Date(year, number, 0).getDate();
  window.dispatchEvent(new CustomEvent('dagoberd:navigate', { detail: {
    tab: 'transactions', filters: {
      booking_from: `${month}-01`, booking_to: `${month}-${String(last).padStart(2, '0')}`,
    },
  } }));
}

function drillIntoDate(value) {
  drillIntoMonth(value.slice(0, 7));
}
