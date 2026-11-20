// 页面通用的小组件和格式化函数。

import { LineChart, clock } from './chart.js';
import { tr, locale } from './i18n.js';

export const GB = 1024 ** 3;

export function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  // 字符串会作为文本节点插入，不会被当成 HTML
  node.append(...children.filter((c) => c != null && c !== false));
  return node;
}

export const store = {
  get(key) {
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch {
      /* 隐私模式等情况下不保存 */
    }
  },
};

// 改名前（AI Board）记的键以 aiboard- 开头，搬到新名字下，之前记住的主题、时间范围、关掉的提示都还在
try {
  for (const old of Object.keys(localStorage)) {
    if (!old.startsWith('aiboard-')) continue;
    const key = `aiinferpeek-${old.slice('aiboard-'.length)}`;
    if (localStorage.getItem(key) == null) localStorage.setItem(key, localStorage.getItem(old));
    localStorage.removeItem(old);
  }
} catch {
  /* 隐私模式等情况下用不了，跳过 */
}

// ---------- 格式化 ----------

export const num = (v, d = 0) =>
  v == null || Number.isNaN(v) ? '—' : v.toLocaleString(locale, { minimumFractionDigits: d, maximumFractionDigits: d });
export const gb = (bytes, d = 1) => (bytes == null ? '—' : num(bytes / GB, d));

export function bytes(b) {
  if (b == null) return '—';
  if (b >= GB) return `${num(b / GB, 1)} GB`;
  return `${num(b / 1024 ** 2)} MB`;
}

export function duration(ms) {
  if (ms == null) return '—';
  const s = ms / 1000;
  // 先按要显示的位数取整再分段：直接拿原数分段的话，59.96 秒会写成“60.0 秒”，119.6 秒会写成“1 分 60 秒”
  if (+s.toFixed(2) < 10) return tr('{n} 秒', { n: num(s, 2) });
  if (+s.toFixed(1) < 60) return tr('{n} 秒', { n: num(s, 1) });
  const total = Math.round(s);
  const m = Math.floor(total / 60);
  if (m < 60) return tr('{m} 分 {s} 秒', { m, s: String(total % 60).padStart(2, '0') });
  return tr('{h} 小时 {m} 分', { h: Math.floor(m / 60), m: m % 60 });
}

// 已经过了多久：58 秒、3 分 05 秒、1 小时 12 分。向下取整到秒，走着的计时不提前跳到下一秒
export function elapsed(ms) {
  if (ms == null) return '—';
  return ms < 60000 ? tr('{n} 秒', { n: Math.max(0, Math.floor(ms / 1000)) }) : duration(Math.floor(ms / 1000) * 1000);
}

// token 数：1234 → 1.2k，1234567 → 1.23M。和 duration 一样先进位再选单位和小数位，9960 是 10k 不是“10.0k”，999600 是 1.00M 不是“1,000k”
export function tokens(n) {
  if (n == null) return '—';
  if (+n.toFixed(0) < 1000) return num(n);
  const k = n / 1e3;
  if (+k.toFixed(1) < 10) return `${num(k, 1)}k`;
  if (+k.toFixed(0) < 1000) return `${num(k)}k`;
  const m = n / 1e6;
  return `${num(m, +m.toFixed(2) < 10 ? 2 : 1)}M`;
}

export function when(t) {
  if (t == null) return '—';
  const d = new Date(t);
  const today = new Date();
  if (d.toDateString() === today.toDateString()) return clock(t);
  return `${d.getMonth() + 1}/${d.getDate()} ${d.toLocaleTimeString(locale, { hour12: false, hour: '2-digit', minute: '2-digit' })}`;
}

// 模型多久后自动卸载：“4 分钟后”“即将卸载”“常驻”；unload 为 true 时是“4 分钟后卸载”
export function until(t, { unload = false } = {}) {
  if (t == null) return tr('常驻');
  // 先向上取整到秒再分段：直接拿原数分段的话，剩 59.2 秒会写成“60 秒后”
  const s = Math.ceil((t - Date.now()) / 1000);
  if (s <= 0) return tr('即将卸载');
  if (s < 60) return unload ? tr('{n} 秒后卸载', { n: s }) : tr('{n} 秒后', { n: s });
  return unload ? tr('{n} 分钟后卸载', { n: Math.floor(s / 60) }) : tr('{n} 分钟后', { n: Math.floor(s / 60) });
}

export const rate = (tokens, ms) => (tokens != null && ms ? tokens / (ms / 1000) : null);

// 占用比例：两个数都读到了、总量大于 0 才算，否则返回 null。
// 显卡读不到的字段是 null，直接相除会当成 0，得到 0、Infinity 或 NaN
export const ratioOf = (part, whole) => (Number.isFinite(part) && Number.isFinite(whole) && whole > 0 ? part / whole : null);

// 占用比例 ≥90% 提示接近满载（warning），≥98% 提示已满（critical）；比例算不出来（null）时不提示。看板和悬浮窗共用
export const usageLevel = (ratio) => (!Number.isFinite(ratio) ? null : ratio >= 0.98 ? 'critical' : ratio >= 0.9 ? 'warning' : null);

// 比例写成百分比：不到 10% 留一位小数（Ollama 的上下文常有几十万，用了几百 token 写成 0% 看不出来），更小的写 <0.1%
export function percent(ratio) {
  if (!Number.isFinite(ratio)) return '—';
  const p = ratio * 100;
  if (p > 0 && p < 0.1) return '<0.1%';
  return `${num(p, p > 0 && p < 10 ? 1 : 0)}%`;
}

export const PERCENT_SCALE = { top: 100, ticks: [0, 25, 50, 75, 100], tickFormat: (v) => `${v}%` };

// ---------- 历史 ----------

// 收到新样本时，上一条换成后端带来的精简版本（slim，只有画图和判断活跃要用的字段），
// 历史里只有最新一条是完整的；再去掉 keepMs 以前的
export function pushSample(history, s, keepMs) {
  const prev = history[history.length - 1];
  if (prev?.slim) history[history.length - 1] = prev.slim;
  history.push(s);
  while (history.length && history[0].t < s.t - keepMs) history.shift();
}

// ---------- 状态图标 ----------

const ICONS = {
  warning:
    '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M8 1.5 15 14H1L8 1.5Z" fill="var(--warning)"/><path d="M8 6v3.5M8 11.5v.5" stroke="#0b0b0b" stroke-width="1.6" stroke-linecap="round"/></svg>',
  critical:
    '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="var(--critical)"/><path d="M8 4.5v4.5M8 11.2v.4" stroke="#fff" stroke-width="1.6" stroke-linecap="round"/></svg>',
  done:
    '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="var(--good)"/><path d="M4.8 8.2l2.2 2.2 4.2-4.6" fill="none" stroke="#fff" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
};

export function icon(kind) {
  const span = h('span', { class: 'status-icon' });
  span.innerHTML = ICONS[kind];
  return span;
}

// 表里一个进程的显存（p 是 procs.rows 里的一行）。性能计数器偶尔给某个进程报出比显卡总显存还大的数（本机见过 800 多 GB），
// 后台已经标出来（vram_bad）、不计入排序和合计，这里灰掉并说明。显卡卡片的按进程表和 Ollama 的模型表共用
export const vramCell = (p) =>
  p.vram_bad
    ? h('span', { class: 'vram-bad', title: tr('计数器报告的数值超过显卡总显存，属读数异常，不计入排序和合计') }, icon('warning'), h('span', { class: 'muted', text: ` ${bytes(p.vram)}` }))
    : bytes(p.vram);

// ---------- 组件 ----------

// 数值小卡片；meter 为 true 时下面带一条占用比例条
export function tile(label, { meter = false } = {}) {
  const numEl = h('span', { class: 'num', text: '—' });
  const unitEl = h('span', { class: 'unit' });
  const fill = meter ? h('div', { class: 'meter-fill' }) : null;
  const note = h('div', { class: 'tile-note' });
  const el = h(
    'div',
    { class: 'tile' },
    h('div', { class: 'tile-label', text: label }),
    h('div', { class: 'tile-value' }, numEl, unitEl),
    meter ? h('div', { class: 'meter', role: 'presentation' }, fill) : null,
    note,
  );
  return {
    el,
    set(value, unit = '', noteText = '') {
      numEl.textContent = value;
      unitEl.textContent = unit;
      if (typeof noteText === 'string') note.textContent = noteText;
      else note.replaceChildren(...noteText);
    },
    // 比例接近满载、已满时提示（见 usageLevel），状态同时用图标和文字表达。
    // 比例算不出来（null）时比例条清空，不提示
    level(ratio, noteText = '') {
      if (!fill) return;
      const pct = Number.isFinite(ratio) ? Math.min(Math.max(ratio * 100, 0), 100) : null;
      fill.style.width = `${pct ?? 0}%`;
      const status = usageLevel(ratio);
      el.classList.toggle('is-warning', status === 'warning');
      el.classList.toggle('is-critical', status === 'critical');
      note.replaceChildren();
      if (status) {
        note.append(icon(status), h('span', { text: status === 'critical' ? tr('已满') : tr('接近满载') }));
        if (noteText) note.append(h('span', { class: 'sep', text: ` · ${noteText}` }));
      } else {
        note.textContent = noteText;
      }
    },
    hide(hidden) {
      el.hidden = hidden;
    },
  };
}

// 带“表格”切换的折线图；表格是图表的无障碍替代，列出可见范围内的抽样数值。
// hideWhenEmpty：整段历史里一个数都没有时（核显没有功耗、温度，没开 LibreHardwareMonitor 时没有 CPU 温度）整张图收起来，不画空坐标轴
export function chartFigure({ title, get, scale, format, gapMs = 3000, hideWhenEmpty = false }) {
  const plot = h('div', { class: 'plot' });
  const btn = h('button', { class: 'table-toggle', type: 'button', 'aria-pressed': 'false', text: tr('表格') });
  const tbody = h('tbody');
  const table = h(
    'table',
    { class: 'data-table', hidden: true },
    h('thead', {}, h('tr', {}, h('th', { text: tr('时间') }), h('th', { text: title }))),
    tbody,
  );
  const fig = h('figure', { class: 'chart' }, h('figcaption', {}, h('span', { class: 'chart-title', text: title }), btn), plot, table);
  const chart = new LineChart(plot, { label: title, format, scale: typeof scale === 'function' ? null : scale, gapMs });
  let points = [];
  let range = [0, 0];

  function renderTable() {
    const vis = points.filter((p) => p.t >= range[0] && p.v != null);
    const step = Math.max(1, Math.floor(vis.length / 10));
    const rows = [];
    for (let i = vis.length - 1; i >= 0; i -= step) {
      const p = vis[i];
      rows.push(h('tr', {}, h('td', { text: clock(p.t) }), h('td', { text: format(p.v) })));
    }
    if (!rows.length) rows.push(h('tr', {}, h('td', { colspan: '2', text: tr('该时段无数据') })));
    tbody.replaceChildren(...rows);
  }

  btn.addEventListener('click', () => {
    const show = table.hidden;
    table.hidden = !show;
    btn.setAttribute('aria-pressed', String(show));
    if (show) renderTable();
  });

  return {
    el: fig,
    update(history, t0, t1) {
      points = history.map((s) => ({ t: s.t, v: get(s) }));
      range = [t0, t1];
      if (hideWhenEmpty) {
        const empty = !points.some((p) => p.v != null);
        fig.hidden = empty;
        if (empty) return;
      }
      if (typeof scale === 'function') chart.setScale(scale(points));
      chart.update(points, t0, t1);
      if (!table.hidden) renderTable();
    },
    redraw() {
      chart.draw();
    },
    destroy() {
      chart.destroy();
      points = [];
    },
  };
}

// 普通数据表；单元格可以是字符串或节点
export function dataTable(columns, { empty = tr('暂无数据') } = {}) {
  const tbody = h('tbody');
  const table = h(
    'table',
    { class: 'grid-table' },
    h('thead', {}, h('tr', {}, ...columns.map((c) => h('th', { class: c.cls, text: c.label })))),
    tbody,
  );
  return {
    el: h('div', { class: 'table-wrap' }, table),
    set(rows) {
      if (!rows.length) {
        tbody.replaceChildren(h('tr', {}, h('td', { class: 'empty-cell', colspan: String(columns.length), text: empty })));
        return;
      }
      tbody.replaceChildren(
        ...rows.map((cells) => h('tr', {}, ...cells.map((c, i) => h('td', { class: columns[i].cls }, ...(Array.isArray(c) ? c : [c]))))),
      );
    },
  };
}

// 折叠按钮上的箭头：展开时朝下，折叠时朝右（由 CSS 旋转）
export function chevron() {
  const span = h('span', { class: 'fold-icon' });
  span.innerHTML =
    '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  return span;
}

// foldable 为 true 时标题栏右侧有“收起 / 展开”按钮；按钮只通知 onToggle，折不折由调用方决定（fold()）
export function card(title, sub, { wide = false, foldable = false } = {}) {
  const subEl = h('span', { class: 'card-sub', text: sub });
  const extra = h('span', { class: 'card-extra' });
  const body = h('div', { class: 'card-body' });
  const head = h('header', { class: 'card-head' }, h('h2', { text: title }), subEl, extra);
  const el = h('section', { class: `card${wide ? ' wide' : ''}` }, head, body);
  const c = { el, head, body, subEl, extra, folded: false, onToggle: null };
  if (foldable) {
    const label = h('span');
    const btn = h('button', { class: 'fold-btn', type: 'button' }, chevron(), label);
    btn.addEventListener('click', () => c.onToggle?.(!c.folded));
    head.append(btn);
    c.fold = (folded) => {
      c.folded = folded;
      el.classList.toggle('is-folded', folded);
      body.hidden = folded;
      btn.setAttribute('aria-expanded', String(!folded));
      btn.setAttribute('aria-label', folded ? tr('展开 {title}', { title }) : tr('收起 {title}', { title }));
      label.textContent = folded ? tr('展开') : tr('收起');
    };
    c.fold(false);
  }
  return c;
}

export const sectionTitle = (text) => h('h3', { class: 'section-title', text });

// 卡片底部的数据来源说明：很长、读过一次就不用再看，默认收起，点“数据来源”展开。返回 [按钮, 说明]
export function sourceNote(text) {
  const note = h('p', { class: 'footnote source-note', text, hidden: true });
  const btn = h('button', { class: 'more-btn source-btn', type: 'button', 'aria-expanded': 'false' }, chevron(), h('span', { text: tr('数据来源') }));
  btn.addEventListener('click', () => {
    note.hidden = !note.hidden;
    btn.setAttribute('aria-expanded', String(!note.hidden));
  });
  return [btn, note];
}
