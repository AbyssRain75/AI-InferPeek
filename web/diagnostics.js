// “为什么没有发现某个服务或工具”：后台每次发现时按工具记下的检测情况（/api/diagnostics）。
// 看板（“正在运行的模型”卡片底部的“没看到某个工具？”、一个都没发现时的“模型服务”卡片）和设置页面共用这里的写法。

import { h, num, icon, chevron, card } from './ui.js';
import { tr } from './i18n.js';

export const DIAG_REFRESH_MS = 5000; // 后台每 5 秒发现一次，页面也每 5 秒取一次
export const DIAG_STATE = { ok: tr('已发现'), warning: tr('有问题'), probing: tr('正在连接'), missing: tr('没有发现') };
export const DIAG_SCOPE = tr('仅检测进程运行状态、端口监听及数据目录是否存在，不读取任何对话内容。');
const PATH_TEXT = {
  home: [tr('数据目录不存在'), tr('无法打开数据目录')],
  data: [tr('数据目录中不存在会话文件夹'), tr('无法打开会话文件夹')],
  db: [tr('数据目录中不存在会话数据库'), tr('无法打开会话数据库')],
  log: [tr('未找到日志文件'), tr('无法打开日志文件')],
};
export const hostOf = (url) => (url || '').replace(/^https?:\/\//, '');
const pathEl = (path) => h('span', { class: 'diag-path', text: path });

// ---------- 检测情况的写法 ----------

// 一条检测情况：{ text: 主要的一句（字符串或节点）, detail: 补充说明, addrs: 探测过的地址 }
export function diagnosticLine(c, tool) {
  switch (c.code) {
    case 'found':
      if (c.configured) return { text: tr('配置文件中的地址 {url}', { url: hostOf(c.url) }) };
      return { text: c.url ? `${hostOf(c.url)} · ${tr('进程 {pid}', { pid: c.pid })}` : tr('{n} 个进程', { n: c.processes }) };
    case 'offline':
      return {
        text: c.configured ? tr('无法连接配置文件中添加的地址：{url}', { url: hostOf(c.url) }) : tr('进程 {pid} 的 {url} 当前无法连接', { pid: c.pid, url: hostOf(c.url) }),
        detail: c.error,
      };
    case 'poll_error':
      return { text: tr('已发现，但读取出错'), detail: c.error };
    case 'find_error':
      return { text: tr('查找进程时出错'), detail: c.error };
    case 'no_process':
      return { text: c.names?.length ? tr('未找到进程（可识别 {names}）', { names: c.names.join(tr('、')) }) : tr('未找到进程'), detail: c.hint };
    case 'no_port':
      return { text: tr('进程 {pid} 正在运行，但尚未监听端口', { pid: c.pid }), detail: tr('可能仍在启动') };
    case 'ports_unknown':
      return { text: tr('进程 {pid} 正在运行，但无法获取其监听的端口', { pid: c.pid }), detail: c.error };
    case 'probing':
      return { text: tr('进程 {pid} 正在运行，正在探测其监听的端口', { pid: c.pid }), addrs: c.addrs };
    case 'probe_failed':
      return { text: tr('进程 {pid} 正在运行，但监听的端口均探测失败', { pid: c.pid }), addrs: c.addrs };
    case 'missing_path': {
      const [missing, broken] = PATH_TEXT[c.role] || PATH_TEXT.home;
      const where = c.key ? tr('可在 aiinferpeek.json 的 {key} 中改为实际位置', { key: c.key }) : '';
      // 工具没在运行时多半只是没装
      const unused = tr('未运行或未使用 {tool} 时可忽略此项', { tool: tool.label });
      const detail = c.level === 'info' && c.role !== 'log' ? (where ? tr('{unused}；安装在其他位置时{where}', { unused, where }) : unused) : where;
      return {
        text: [tr('{what}：', { what: c.error ? broken : missing }), pathEl(c.path), c.role === 'log' ? tr('，无法统计每次请求') : ''],
        detail: [c.error, detail].filter(Boolean).join(tr('。')),
      };
    }
    default:
      return { text: c.code };
  }
}

export function diagMark(status) {
  if (status === 'ok') return icon('done');
  if (status === 'warning') return icon('warning');
  return h('span', { class: 'diag-dot', 'aria-hidden': 'true' });
}

// 主要的一句，后面跟灰色的补充说明
const lineParts = (line) => [...[line.text].flat(), line.detail ? h('span', { class: 'diag-detail', text: ` · ${line.detail}` }) : null];

// 一条检测情况 → 列表里的一行：主要的一句、补充说明，下面列出探测过的地址
export function diagLineEl(c, tool) {
  const line = diagnosticLine(c, tool);
  return h(
    'li',
    { 'data-level': c.level },
    ...lineParts(line),
    line.addrs?.length
      ? h(
          'ul',
          { class: 'diag-addrs' },
          ...line.addrs.map((a) => h('li', {}, hostOf(a.url), h('span', { class: 'diag-detail', text: ` · ${a.error || tr('待探测')}` }))),
        )
      : null,
  );
}

// 一条检测情况写成一句（不列地址），设置页面放在输入框、地址下面
export const diagSummaryEl = (c, tool) => h('span', {}, ...lineParts(diagnosticLine(c, tool)));

// [“3 秒前完成检测”, “每 5 秒检测一次”]；还没检测过时是“正在检测…”
export function diagTiming(d) {
  const ago = d?.checked ? Math.max(0, Math.round((Date.now() - d.checked) / 1000)) : null;
  // 检测周期是轮询间隔的整数倍，不一定是整数（--interval 1.1 时是 5.5 秒）：不是整数时留一位小数，不四舍五入成“每 6 秒”
  const every = Math.round((d?.every || 5) * 10) / 10;
  return [
    ago == null ? tr('正在检测…') : tr('{ago}完成检测', { ago: ago < 2 ? tr('刚刚') : tr('{n} 秒前', { n: ago }) }),
    tr('每 {n} 秒检测一次', { n: num(every, Number.isInteger(every) ? 0 : 1) }),
  ];
}

// ---------- 看板上的检测情况 ----------
// 只在页面上看得到这些情况时才取：空状态出现、或者展开“没看到某个工具？”时取一次，之后每 5 秒刷新；
// 不随每次渲染请求，看不到了（卡片被换掉、收起）就停

const diag = { data: null, error: null, at: 0, loading: false, timer: 0 };
const diagViews = new Set(); // { shown(): 现在看不看得到, render() }

const isShown = (el) => el.isConnected && el.getClientRects().length > 0;

function loadDiagnostics() {
  // 留 1 秒余量：定时器每 5 秒一次，不能因为差几毫秒被跳过
  if (diag.loading || Date.now() - diag.at < DIAG_REFRESH_MS - 1000) return;
  diag.loading = true;
  diag.at = Date.now();
  fetch('/api/diagnostics', { cache: 'no-store' })
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
    .then((d) => {
      diag.data = d;
      diag.error = null;
    })
    .catch((e) => {
      diag.error = e.message || String(e);
    })
    .finally(() => {
      diag.loading = false;
      for (const v of diagViews) if (v.shown()) v.render();
    });
}

function watchDiagnostics() {
  loadDiagnostics();
  if (diag.timer) return;
  diag.timer = setInterval(() => {
    if (![...diagViews].some((v) => v.shown())) {
      clearInterval(diag.timer);
      diag.timer = 0;
    } else if (!document.hidden) loadDiagnostics();
  }, DIAG_REFRESH_MS);
}

function diagItem(tool) {
  return h(
    'li',
    { class: 'diag-item', 'data-status': tool.status },
    h(
      'div',
      { class: 'diag-head' },
      h('span', { class: 'diag-mark' }, diagMark(tool.status)),
      h('strong', { text: tool.label }),
      h('span', { class: 'diag-state', text: DIAG_STATE[tool.status] || tool.status }),
    ),
    h('ul', { class: 'diag-lines' }, ...tool.checks.map((c) => diagLineEl(c, tool))),
  );
}

// 每个工具一项：状态 + 检测情况；还没取到时说明在检测，取不到时列出能识别哪些工具
function diagList(supported = () => []) {
  const el = h('div', { class: 'diag' });
  return {
    el,
    render() {
      const d = diag.data;
      if (!d?.checked) {
        const names = supported().join(tr('、'));
        const text = [diag.error && !d ? tr('无法获取检测情况（{error}）。', { error: diag.error }) : tr('正在检测…'), names ? tr('当前可自动识别：{names}。', { names }) : ''];
        el.replaceChildren(h('p', { class: 'footnote', text: text.join('') }));
        return;
      }
      const note = [...diagTiming(d), diag.error ? tr('刷新失败：{error}', { error: diag.error }) : null].filter(Boolean);
      el.replaceChildren(
        h('ul', { class: 'diag-list' }, ...d.tools.map(diagItem)),
        h('p', { class: 'footnote', text: `${note.join(' · ')}${tr('。')}${DIAG_SCOPE}` }),
      );
    },
  };
}

// “正在运行的模型”卡片底部的入口，默认收起，展开后才取检测情况
export function diagToggle() {
  const list = diagList();
  list.el.hidden = true;
  list.el.id = 'diag-panel';
  const btn = h('button', { class: 'more-btn diag-btn', type: 'button', 'aria-expanded': 'false', 'aria-controls': list.el.id }, chevron(), h('span', { text: tr('没看到某个工具？') }));
  btn.addEventListener('click', () => {
    const open = list.el.hidden;
    list.el.hidden = !open;
    btn.setAttribute('aria-expanded', String(open));
    if (open) {
      list.render();
      watchDiagnostics();
    }
  });
  diagViews.add({ shown: () => !list.el.hidden && isShown(list.el), render: list.render });
  return [btn, list.el];
}

// 一个都没发现时的“模型服务”卡片。只建一次：每次渲染都会调用，返回同一个元素，页面不用每秒重建它
let emptyView = null;

export function noRuntimesView(supported) {
  if (!emptyView) {
    const c = card(tr('模型服务'), '', { wide: true });
    const list = diagList(() => emptyView.supported || []);
    c.body.append(h('p', { class: 'empty', text: tr('未检测到正在运行的模型服务或命令行工具，启动后数秒内将在此展示。以下为各工具的检测情况：') }), list.el);
    emptyView = { el: c.el, list, supported };
    diagViews.add({ shown: () => isShown(c.el), render: list.render });
    list.render();
  }
  emptyView.supported = supported;
  watchDiagnostics();
  return emptyView.el;
}
