// 模型服务面板：每个检测到的部署系统、命令行工具一张卡片。

import { niceScale } from './chart.js';
import { h, store, num, gb, bytes, duration, elapsed, tokens, when, until, rate, ratioOf, usageLevel, percent, icon, vramCell, chevron, tile, chartFigure, dataTable, card, sectionTitle, sourceNote } from './ui.js';
import { diagToggle } from './diagnostics.js';
import { tr, trc } from './i18n.js';

// 最近 30 秒里有过非空闲状态（工作中、等你操作、加载中、出错…）的算活跃：排在前面、展开；其余折叠。
// 留 30 秒余量，免得任务之间的空隙让卡片来回折叠、换位置
const ACTIVE_HOLD_MS = 30000;
// 会话刚结束的这段时间里还算活跃，能看到它做完
const SESSION_HOLD_MS = 120000;

export function activeRuntimeIds(history) {
  const ids = new Set();
  const last = history[history.length - 1];
  if (!last) return ids;
  for (let i = history.length - 1; i >= 0 && history[i].t >= last.t - ACTIVE_HOLD_MS; i--) {
    for (const r of history[i].runtimes || []) if (r.status && r.status !== 'idle') ids.add(r.id);
  }
  return ids;
}

// 后台给每个实例带着最后一次成功轮询的时间（polled）。和样本时间差超过轮询间隔的 5 倍、至少 10 秒（轮询很慢或卡住了）
// 才算过期，返回已经多久没更新；正常时返回 0。连不上、读取出错的已经有提示，不再重复
let staleMs = 10000;

// 轮询间隔跟着采样间隔（--interval，秒）走，但不比每秒一次更快（见 collectors/__init__.py）。看板和悬浮窗连上时设置
export function setSampleInterval(seconds) {
  staleMs = Math.max(10, Math.max(1, seconds ?? 1) * 5) * 1000;
}

export function staleAge(rt, now) {
  if (rt.polled == null || rt.status === 'offline' || rt.status === 'error') return 0;
  const age = now - rt.polled;
  return age > staleMs ? age : 0;
}

// 活跃的在前，其次是刚完成过任务的，各自保持原来的顺序（模型服务在命令行工具前面）
export function activeFirst(list, active, recent = new Set()) {
  const tier = (r) => (active.has(r.id) ? 0 : recent.has(r.id) ? 1 : 2);
  return [...list].sort((a, b) => tier(a) - tier(b));
}

// ---------- 模型运行在哪块显卡上 ----------
// 后台按进程标出模型服务用的显卡（procs.devices[pid]、procs.owners[id].devices），是 info.gpus.devices 里的序号。
// 显卡列表在看板和悬浮窗连上时设置（setGpuDevices）

let gpuDevices = [];

export function setGpuDevices(devices) {
  gpuDevices = devices || [];
}

// 名字去掉商标符号、“NVIDIA GeForce”“AMD”前缀和结尾的 GPU：NVIDIA GeForce RTX 5070 Ti Laptop GPU → RTX 5070 Ti Laptop
const shortGpuName = (name) =>
  name
    .replace(/\((R|TM)\)/gi, '')
    .replace(/^\s*(NVIDIA GeForce|NVIDIA|AMD)\s+/i, '')
    .replace(/\s+GPU\s*$/i, '')
    .replace(/\s+/g, ' ')
    .trim();

// 序号 → 显卡名字，用了几块就用“+”连起来；一块都没有时是 null。full：完整名字（鼠标停上去看）
export function gpuText(indices, { full = false } = {}) {
  const names = (indices || []).map((i) => gpuDevices[i]?.name).filter(Boolean);
  return names.length ? names.map(full ? (n) => n : shortGpuName).join(' + ') : null;
}

// 主显卡的序号：有独显时是所有独显，否则是核显，和后台按进程统计的口径一致
export function mainGpus(devices) {
  const all = (devices || []).map((d, i) => ({ d, i }));
  const discrete = all.filter((x) => !x.d.integrated);
  return (discrete.length ? discrete : all).map((x) => x.i);
}

// 模型服务（整个服务，或者给了 pid 时其中一个进程）用的显卡序号
export function usedGpus(procs, r, pid) {
  return pid != null ? procs?.devices?.[pid] : procs?.owners?.[r.id]?.devices;
}

// ---------- “xxx 已完成”提示 ----------
// 后台保留最近 5 分钟结束的任务（rt.done）。点“已知晓”后记下这一刻之前的都看过了，
// 存在浏览器里，看板和悬浮窗是同一个地址，关掉一边另一边也不再显示

const DONE_SEEN_KEY = 'aiinferpeek-done-seen';

// 任务怎么结束的（后台给的 result）
const RESULTS = {
  ok: { text: tr('已完成'), icon: 'done' },
  failed: { text: tr('失败'), icon: 'critical' },
  interrupted: { text: tr('已中断'), icon: 'warning' },
  lost: { text: tr('可能已中断'), icon: 'warning' },
};

export const resultOf = (e) => RESULTS[e.result] || RESULTS.ok;

function seenMap() {
  try {
    return JSON.parse(store.get(DONE_SEEN_KEY) || '{}') || {};
  } catch {
    return {};
  }
}

// 同一个会话（或同一类任务）只留最新一条，带 5 分钟内同样结果的次数；最新的在前。
// 会话又开始工作了，之前的完成提示已经过时，先不显示，这一轮做完再一起提示
export function recentDone(rt) {
  const seen = seenMap()[rt.id] || 0;
  const working = new Set((rt.sessions || []).filter((s) => s.status !== 'idle').map((s) => s.id));
  const groups = new Map();
  for (const e of rt.done || []) {
    if (e.t <= seen || working.has(e.key)) continue;
    if (!groups.has(e.key)) groups.set(e.key, []);
    groups.get(e.key).push(e);
  }
  return [...groups.values()]
    .map((list) => {
      const latest = list.reduce((a, b) => (b.t > a.t ? b : a));
      return { ...latest, count: list.filter((e) => resultOf(e) === resultOf(latest)).length };
    })
    .sort((a, b) => b.t - a.t);
}

export function dismissDone(rt) {
  const map = seenMap();
  map[rt.id] = Math.max(map[rt.id] || 0, ...(rt.done || []).map((e) => e.t));
  store.set(DONE_SEEN_KEY, JSON.stringify(map));
}

// “重写缓存层 已完成 · 2 分钟前 · 用时 12 分 09 秒”
export function doneText(e, now, { short = false } = {}) {
  const ago = now - e.t < 60000 ? tr('刚刚') : tr('{n} 分钟前', { n: Math.floor((now - e.t) / 60000) });
  const count = e.count > 1 ? tr('（5 分钟内 {n} 次）', { n: e.count }) : '';
  const parts = [`${e.name} ${resultOf(e).text}${count}`, ago];
  if (!short && e.duration) parts.push(tr('用时 {time}', { time: duration(e.duration) }));
  if (!short && e.detail) parts.push(e.detail);
  return parts.join(' · ');
}

// 卡片标题下的提示：每条一行，右边一个“已知晓”
function doneList() {
  const items = h('div', { class: 'done-items' });
  const ack = h('button', { class: 'more-btn ack-btn', type: 'button', text: tr('已知晓') });
  const el = h('div', { class: 'done-list', role: 'status', hidden: true }, items, ack);
  let current = null;
  ack.addEventListener('click', () => {
    if (current) dismissDone(current);
    el.hidden = true;
  });
  return {
    el,
    update(rt, now) {
      current = rt;
      const list = recentDone(rt).slice(0, 3);
      el.hidden = !list.length;
      items.replaceChildren(
        ...list.map((e) => h('div', { class: 'done-item', 'data-result': e.result }, icon(resultOf(e).icon), h('span', { text: doneText(e, now) }))),
      );
    },
  };
}

const BUSY_TEXT = { ollama: tr('生成中'), comfyui: tr('执行中') };
const STATUS_TEXT = { loading: tr('加载模型中'), idle: tr('空闲'), starting: tr('启动中'), offline: tr('无法连接'), error: tr('读取出错') };

// 服务或工具的状态文字：工作中的 Ollama 是“生成中”、ComfyUI 是“执行中”，其余见 STATUS_TEXT。卡片右上角和悬浮窗共用
export const statusText = (kind, status) => (status === 'busy' ? BUSY_TEXT[kind] || tr('运行中') : STATUS_TEXT[status] || status || '');

function statusChip() {
  const dot = h('span', { class: 'chip-dot' });
  const label = h('span');
  const el = h('span', { class: 'chip', role: 'status' }, dot, label);
  return {
    el,
    set(kind, status, text) {
      el.dataset.status = status || 'idle';
      const bad = status === 'offline' || status === 'error';
      dot.replaceChildren(...(bad ? [icon('critical')] : []));
      label.textContent = text || statusText(kind, status);
    },
  };
}

// 每张卡片顶部都有的资源合计：显存、共享显存、内存、CPU（命令行工具只显示内存和 CPU）
function resourceTiles(keys = ['vram', 'shared', 'mem', 'cpu']) {
  const all = { vram: tile(tr('显存')), shared: tile(tr('共享显存')), mem: tile(tr('内存')), cpu: tile('CPU') };
  const t = Object.fromEntries(keys.map((k) => [k, all[k]]));
  return {
    tiles: Object.values(t).map((x) => x.el),
    update(total) {
      t.vram?.set(total ? gb(total.vram) : '—', ' GB', total?.gpu ? tr('GPU 利用率 {v}%', { v: num(total.gpu) }) : '');
      t.shared?.set(total ? gb(total.shared) : '—', ' GB', total?.shared > 512 * 1024 ** 2 ? tr('借用的内存，慢于显存') : '');
      t.mem?.set(total ? gb(total.mem) : '—', ' GB');
      t.cpu?.set(total ? num(total.cpu) : '—', '%');
    },
  };
}

function shell(r, title, resKeys) {
  const c = card(title, '', { wide: true, foldable: true });
  const chip = statusChip();
  // 数据过期提示放在状态旁边，卡片折叠时也看得到
  const stale = h('span', { class: 'stale-note', hidden: true });
  c.extra.append(stale, chip.el);
  // 完成提示放在标题栏里，卡片折叠时也看得到
  const done = doneList();
  c.head.append(done.el);
  // 连不上、读取出错时的原因，放在卡片最上面（出错的卡片算活跃，是展开的）
  const error = h('p', { class: 'notice card-error', hidden: true });
  c.body.append(error);
  const res = resourceTiles(resKeys);
  return {
    c,
    res,
    // chipText：卡片右上角的状态文字，不给时按状态写（见 statusText）
    head(rt, sample, chipText) {
      const parts = [rt.version ? tr('版本 {v}', { v: rt.version }) : null, rt.url ? rt.url.replace(/^https?:\/\//, '') : rt.home];
      if (rt.pytorch) parts.push(`PyTorch ${rt.pytorch}`);
      if (!rt.local) parts.push(tr('远程（进程资源不可见）'));
      // 模型服务用的显卡；命令行工具的模型在云端，它的进程用显卡只是画界面，不标
      const gpus = rt.category === 'cli' ? null : usedGpus(sample.procs, rt);
      if (gpuText(gpus)) parts.push(tr('显卡 {name}', { name: gpuText(gpus) }));
      c.subEl.textContent = parts.filter(Boolean).join(' · ');
      c.subEl.title = gpuText(gpus, { full: true }) || '';
      chip.set(rt.kind, rt.status, chipText);
      const failed = (rt.status === 'offline' || rt.status === 'error') && rt.error;
      error.hidden = !failed;
      if (failed) error.replaceChildren(icon('critical'), h('span', { text: tr('{state}：{error}', { state: statusText(rt.kind, rt.status), error: rt.error }) }));
      const age = staleAge(rt, sample.t);
      stale.hidden = !age;
      if (age) stale.replaceChildren(icon('warning'), tr('数据已过期 · {time}前', { time: elapsed(age) }));
      c.el.classList.toggle('is-stale', age > 0);
      done.update(rt, sample.t);
      res.update(sample.procs?.owners?.[rt.id]);
    },
  };
}

// GPU / CPU 分配条：两段，中间留 2px 间隙
function splitBar(ratio) {
  if (ratio == null) return '—';
  const gpu = Math.round(ratio * 100);
  const text = gpu >= 100 ? '100% GPU' : gpu <= 0 ? '100% CPU' : `${gpu}% GPU · ${100 - gpu}% CPU`;
  const bar = h('span', { class: 'split-bar', 'aria-hidden': 'true' });
  if (gpu > 0) bar.append(h('span', { class: 'split-gpu', style: `flex-grow:${gpu}` }));
  if (gpu < 100) bar.append(h('span', { class: 'split-cpu', style: `flex-grow:${100 - gpu}` }));
  return h('span', { class: 'split' }, bar, h('span', { text }));
}

// 并行多路（OLLAMA_NUM_PARALLEL）时说明显示的是哪一路：空闲时是占得最满的；busy（模型在处理请求）时是正在处理的那一路，
// 几路同时在处理时取其中最满的（见 ollama.py 的 _live）
function slotsNote(slots, busy) {
  if (!(slots > 1)) return null;
  return busy ? tr('并行 {n} 路，显示正在处理的一路', { n: slots }) : tr('并行 {n} 路，显示占用最高的一路', { n: slots });
}

const modelBusy = (m) => m.state === 'generating' || m.state === 'prompt';

// Ollama 的上下文占用：“1.2k / 32k · 3.8%”，占到 90% 以上前面带提醒图标（见 usageLevel）。
// 已用多少读不到（远程服务、不是 llama-server 跑的模型）时写“— / 上下文大小”；truncated：上下文满了，截掉了前面一部分，
// 带告急图标，鼠标停上去看说明。compact：列多的表里省掉上下文大小（“1.2k · 3.8%”），鼠标停上去看完整数字。
// slots、busy：并行几路、模型是不是在处理请求（见 slotsNote）
function contextCell(used, size, { slots, busy = false, truncated, compact = false } = {}) {
  if (used == null && (!size || compact)) return '—';
  if (used == null) return h('span', { class: 'muted', text: `— / ${tokens(size)}`, title: tr('无法获取已用量，仅有上下文大小') });
  const ratio = ratioOf(used, size);
  const level = truncated ? 'critical' : usageLevel(ratio);
  const title = [
    size ? tr('已用 {used} / {size} tokens', { used: num(used), size: num(size) }) : tr('已用 {used} tokens', { used: num(used) }),
    slotsNote(slots, busy),
    truncated
      ? tr('已截断：上下文已满，前面的部分内容已丢弃')
      : level === 'critical'
        ? tr('已满')
        : level === 'warning'
          ? tr('接近满载')
          : null,
  ];
  return h(
    'span',
    { class: 'ctx', title: title.filter(Boolean).join('\n') },
    level ? icon(level) : null,
    size && !compact ? `${tokens(used)} / ${tokens(size)}` : tokens(used),
    ratio != null ? h('span', { class: 'muted', text: ` · ${percent(ratio)}` }) : null,
  );
}

// 灰色的“ · RTX 5070 Ti Laptop”，鼠标停上去看完整名字
function gpuNote(indices) {
  const text = gpuText(indices);
  return text ? h('span', { class: 'muted', text: ` · ${text}`, title: gpuText(indices, { full: true }) }) : null;
}

function modelState(m) {
  if (m.state === 'generating') {
    const parts = [m.speed != null ? `${num(m.speed, 1)} tokens/s` : null, m.n_decoded != null ? tr('已生成 {n}', { n: num(m.n_decoded) }) : null];
    return h('span', { class: 'state busy' }, h('strong', { text: tr('生成中') }), parts.filter(Boolean).length ? ` · ${parts.filter(Boolean).join(' · ')}` : '');
  }
  if (m.state === 'prompt') return h('span', { class: 'state busy' }, h('strong', { text: tr('处理提示词') }));
  if (m.state === 'loading') return h('span', { class: 'state' }, h('strong', { text: tr('加载中') }));
  return h('span', { class: 'state muted', text: tr('空闲') });
}

// 卡片里的速度曲线：每条样本里这个服务或工具的 speed（tokens/s），刻度至少到 floor
function speedChart(id, { title, floor, tickFormat, gapMs }) {
  return chartFigure({
    title,
    get: (smp) => smp.runtimes?.find((x) => x.id === id)?.speed ?? null,
    scale: (pts) => niceScale(Math.max(floor, ...pts.map((p) => p.v ?? 0)), tickFormat),
    format: (v) => `${num(v, 1)} tokens/s`,
    gapMs,
  });
}

// 上下文占用磁贴看哪个模型：正在生成（或处理提示词）的，否则是占得最满的
function contextModel(ms, busy) {
  if (busy && busy.context != null) return busy;
  let best = null;
  for (const m of ms) {
    const ratio = ratioOf(m.context, m.context_window);
    if (ratio != null && (!best || ratio > best.ratio)) best = { m, ratio };
  }
  return best?.m || null;
}

function ollamaView(r, gapMs) {
  const s = shell(r, 'Ollama');
  const t = { now: tile(tr('实时生成速度')), gen: tile(tr('上次生成速度')), prompt: tile(tr('上次提示词处理')), ctx: tile(tr('上下文占用'), { meter: true }) };
  const tilesEl = h('div', { class: 'tiles' }, t.now.el, t.gen.el, t.prompt.el, t.ctx.el, ...s.res.tiles);
  s.c.body.append(tilesEl);

  const models = dataTable(
    [
      { label: tr('模型') },
      { label: tr('规模') },
      { label: tr('大小'), cls: 'num-col' },
      { label: tr('进程显存'), cls: 'num-col' },
      { label: tr('显卡 / CPU') },
      { label: tr('上下文'), cls: 'num-col' },
      { label: tr('状态') },
      { label: tr('自动卸载') },
    ],
    { empty: tr('未加载模型。发起一次对话后，模型将在此展示。') },
  );
  const modelsSec = h('div', {}, sectionTitle(tr('已加载的模型')), models.el);
  s.c.body.append(modelsSec);

  const speed = speedChart(r.id, { title: tr('生成速度（tokens/s）'), floor: 10, tickFormat: (v) => `${v}`, gapMs });
  const requests = dataTable(
    [
      { label: tr('完成时间') },
      { label: tr('模型') },
      { label: tr('提示词'), cls: 'num-col' },
      { label: trc('表头', '生成'), cls: 'num-col' },
      { label: tr('上下文'), cls: 'num-col' },
      { label: tr('总耗时'), cls: 'num-col' },
    ],
    { empty: tr('暂无请求记录') },
  );
  const logNote = h('p', { class: 'footnote' });
  const splitEl = h('div', { class: 'split-layout' }, speed.el, h('div', {}, sectionTitle(tr('最近的请求')), requests.el, logNote));
  // 没有加载模型、也没有请求记录（刚启动、模型已卸载）时磁贴和表格全是空的，收成一句
  const emptyEl = h('p', { class: 'empty', text: tr('未加载模型。发起一次对话后，模型、生成速度和请求记录将在此展示。'), hidden: true });
  s.c.body.append(splitEl, emptyEl);

  const tokRate = (n, ms) => {
    const v = rate(n, ms);
    return v == null ? '—' : [`${num(n)} tokens`, h('span', { class: 'muted', text: ` · ${num(v, 1)} tokens/s` })];
  };

  return {
    el: s.c.el,
    card: s.c,
    charts: [speed],
    update(rt, sample) {
      s.head(rt, sample);
      const ms = rt.models || [];
      const reqs = rt.requests || [];
      const empty = !ms.length && !reqs.length;
      tilesEl.hidden = modelsSec.hidden = splitEl.hidden = empty;
      emptyEl.hidden = !empty;
      if (empty) return;
      const busy = ms.find(modelBusy);
      if (rt.speed != null) t.now.set(num(rt.speed, 1), ' tokens/s', busy ? busy.name : '');
      else t.now.set('—', '', busy?.state === 'prompt' ? tr('正在处理提示词') : rt.status === 'busy' ? tr('正在计算…') : tr('空闲'));

      const last = reqs[0];
      t.gen.set(last ? num(rate(last.gen_tokens, last.gen_ms), 1) : '—', last ? ' tokens/s' : '', last ? `${num(last.gen_tokens)} tokens · ${when(last.t)}` : '');
      t.prompt.set(
        last ? num(rate(last.prompt_tokens, last.prompt_ms), 0) : '—',
        last ? ' tokens/s' : '',
        last ? `${num(last.prompt_tokens)} tokens · ${duration(last.prompt_ms)}` : '',
      );

      // 空闲时是模型还留着的上一段对话占的；加载了几个模型时注明是哪个
      const cm = contextModel(ms, busy);
      const cr = cm ? ratioOf(cm.context, cm.context_window) : null;
      if (cr != null) {
        t.ctx.set(percent(cr).replace('%', ''), '%');
        t.ctx.level(cr, [`${tokens(cm.context)} / ${tokens(cm.context_window)}`, ms.length > 1 ? cm.name : null].filter(Boolean).join(' · '));
        t.ctx.el.title = [cm.name, tr('已用 {used} / {size} tokens', { used: num(cm.context), size: num(cm.context_window) }), slotsNote(cm.context_slots, modelBusy(cm))]
          .filter(Boolean)
          .join('\n');
      } else {
        t.ctx.set('—', '');
        t.ctx.level(null, ms.length ? tr('无法获取已用量') : tr('未加载模型'));
        t.ctx.el.title = '';
      }

      const procs = new Map((sample.procs?.rows || []).map((p) => [p.pid, p]));
      models.set(
        ms.map((m) => [
          h('strong', { text: m.name }),
          [m.params, m.quant].filter(Boolean).join(' · ') || '—',
          m.size ? bytes(m.size) : '—',
          m.pid && procs.get(m.pid) ? vramCell(procs.get(m.pid)) : '—',
          [splitBar(m.gpu_ratio), m.gpu_ratio > 0 ? gpuNote(usedGpus(sample.procs, rt, m.pid)) : null],
          contextCell(m.context, m.context_window, { slots: m.context_slots, busy: modelBusy(m) }),
          modelState(m),
          m.state === 'loading' ? '—' : until(m.expires),
        ]),
      );

      requests.set(
        reqs
          .slice(0, 8)
          .map((q) => [
            when(q.t),
            q.model ? h('span', { class: 'clip', text: q.model, title: q.model }) : '—',
            tokRate(q.prompt_tokens, q.prompt_ms),
            tokRate(q.gen_tokens, q.gen_ms),
            contextCell(q.context, q.context_window, { truncated: q.truncated, compact: true }),
            duration(q.total_ms),
          ]),
      );
      logNote.textContent = rt.log
        ? tr('每次请求的数据来自 Ollama 日志，仅读取计时和上下文用量（请求结束时的占用量）。')
        : tr('无法读取 Ollama 日志（远程服务或日志位置不同），故无法统计每次请求。');
    },
  };
}

function comfyView(r) {
  const s = shell(r, 'ComfyUI');
  const t = { current: tile(tr('当前任务')), queue: tile(tr('排队')), last: tile(tr('上次耗时')), avg: tile(tr('最近 5 次平均')) };
  s.c.body.append(h('div', { class: 'tiles' }, t.current.el, t.queue.el, t.last.el, t.avg.el, ...s.res.tiles));

  const jobs = dataTable(
    [{ label: tr('完成时间') }, { label: tr('任务') }, { label: trc('ComfyUI 表头', '输出') }, { label: tr('耗时'), cls: 'num-col' }, { label: tr('结果') }],
    { empty: tr('暂无已完成的任务') },
  );
  s.c.body.append(
    sectionTitle(tr('最近的任务')),
    jobs.el,
    ...sourceNote(tr('当前步数和每秒步数仅发送给提交任务的 ComfyUI 网页，外部程序无法获取，因此此处仅显示每个任务的总耗时。')),
  );

  const output = (j) => {
    const parts = [];
    if (j.outputs != null) parts.push(trc('输出文件数', '{n} 个', { n: j.outputs }));
    const kind = { image: tr('图片'), video: tr('视频'), audio: tr('音频'), images: tr('图片') }[j.media] || j.media;
    if (kind) parts.push(kind);
    if (j.width && j.height) parts.push(`${j.width}×${j.height}`);
    return parts.join(' · ') || '—';
  };

  return {
    el: s.c.el,
    card: s.c,
    charts: [],
    update(rt, sample) {
      s.head(rt, sample);
      const cur = rt.current;
      if (cur) {
        const sec = cur.elapsed != null ? Math.floor(cur.elapsed / 1000) : null;
        t.current.set(sec == null ? tr('执行中') : sec < 60 ? tr('{n} 秒', { n: sec }) : duration(sec * 1000), '', tr('已运行 · 任务 {id}', { id: String(cur.id || '').slice(0, 8) }));
      }
      else t.current.set(tr('空闲'), '');
      const q = rt.queue || {};
      t.queue.set(num(q.pending ?? 0), trc('数量单位', ' 个'), tr('等待执行'));
      t.last.set(rt.last_duration != null ? num(rt.last_duration / 1000, 1) : '—', rt.last_duration != null ? tr(' 秒') : '');
      t.avg.set(rt.avg_duration != null ? num(rt.avg_duration / 1000, 1) : '—', rt.avg_duration != null ? tr(' 秒') : '');
      jobs.set(
        (rt.jobs || []).map((j) => [
          when(j.end ?? j.created),
          h('span', { class: 'mono', text: String(j.id || '').slice(0, 8) }),
          output(j),
          duration(j.duration),
          j.status === 'completed' ? tr('完成') : j.status === 'interrupted' ? [icon('warning'), ` ${tr('已中断')}`] : [icon('critical'), ` ${tr('失败')}`],
        ]),
      );
    },
  };
}

// ---------- 命令行工具（Claude Code、Codex、DeepSeek Harness、Grok Build、OpenCode、OpenClaw、Antigravity、Cursor、WorkBuddy） ----------

const ENTRY_TEXT = {
  cli: tr('终端'),
  'claude-desktop': tr('桌面版'),
  'claude-vscode': 'VS Code',
  'sdk-ts': 'SDK',
  'sdk-py': 'SDK',
  'Codex Desktop': tr('桌面版'),
  codex_cli_rs: tr('终端'),
  codex_vscode: 'VS Code',
  codex_exec: 'exec',
  chatgpt: tr('本机执行'),
};
const WAITING_TEXT = {
  'dialog open': tr('确认对话框'),
  'input needed': tr('需要输入'),
  'sandbox request': tr('沙箱权限'),
  'goal proposal': tr('确认目标'),
  question: tr('回答问题'),
  permission: tr('确认权限'),
  review: tr('审阅'),
};
const AGENT_NOTE = {
  'claude-code': tr(
    '数据来自 Claude Code 的会话登记和会话记录，仅读取标题、目录、状态、模型和 token 用量，不读取对话内容。上下文为最近一次请求发送给模型的 tokens。' +
    '额度（Beta）读取自 Claude 桌面版记录的套餐用量，仅采用与 Claude Code 登录的组织相同的记录，并不准确：桌面版仅在启动时记录一次，' +
    '24 小时内打开过托盘用量面板时才每 15 分钟记录一次，因此常为数小时前的数据，且重置时间未知。',
  ),
  codex: tr(
    '桌面版的一个 codex 进程服务所有会话，因此列出最近有活动的会话。额度为最近一次请求时服务器返回的数值。仅读取会话记录中的状态和用量，不读取对话内容。' +
    '在 ChatGPT 中选择本机执行的任务，其对话和模型位于 ChatGPT 服务器，本机仅有代为执行命令的 codex 进程：该进程为每一轮启动数个工具进程，执行完即关闭，' +
    '因此仅可获知对话的开始时间和本轮是否仍在进行，标题、目录、模型、用量及本轮的结束方式均不可见，也不提示完成。',
  ),
  dsh: tr(
    '数据来自 DeepSeek Harness 自行生成的会话汇总（~/.dsh/storages），仅读取标题、目录、状态、模型和 token 用量。子任务不单独列出。' +
    '余额读取自“余额小鲸鱼”插件（dsh-whale-widget）记录的账本（~/.dsh/.dshw-usage.json），不读取 API key、不联网；未安装该插件时不显示余额。' +
    '“今天已用”按北京时间分日统计，与插件一致。余额预警可在设置页面的 DeepSeek Harness 一栏中修改。',
  ),
  'grok-build': tr('数据来自 Grok Build 的会话目录，仅读取标题、模型、回合和权限事件、上下文和 token 用量，不读取对话内容。token 用量在每个回合结束时更新。'),
  opencode: tr(
    '数据来自 OpenCode 的会话数据库（opencode.db），仅读取标题、目录、模型、回合的开始和结束时间、token 用量，不读取对话内容。' +
    '工具调用等待你确认或回答问题时，数据库中无记录，显示为工作中。子会话不单独列出。',
  ),
  openclaw: tr(
    '数据来自 OpenClaw 各 agent 的会话数据库，仅读取标题、聊天渠道、模型、消息的角色和结束原因、上下文和 token 用量，不读取对话内容。' +
    '工具调用等待你确认时，数据库中无记录，显示为工作中。子会话不单独列出。',
  ),
  antigravity: tr(
    '数据来自 Antigravity 的对话库（~/.gemini/antigravity），仅读取标题、目录、运行状态、每一步的状态和时间、模型名、上下文和 token 用量，不读取对话内容。' +
    'token 用量在模型每次回复完成后更新。子 agent 的对话不单独列出。' +
    '额度（Beta）每分钟从其后台程序 language_server 读取一次：使用的是未公开的本机接口（携带其启动参数中的令牌，仅连接本机），升级后可能无法获取；' +
    '同一组内的模型共用一份额度。',
  ),
  cursor: tr(
    '数据来自 Cursor 数据目录中的状态库（state.vscdb），仅读取对话的标题、工作区、状态、是否有待你处理的事项、模型和上下文，不读取对话内容。' +
    'Cursor 不将 token 用量存入数据库，因此无输入、输出和输出速度；本轮是出错还是被你停止，数据库同样不记录，均显示为已中断。' +
    '子 agent 的对话、已归档的对话不列出。',
  ),
  workbuddy: tr(
    '数据来自 WorkBuddy 数据目录（~/.workbuddy）中的会话记录和会话进程登记，仅读取标题、目录、模型、本轮的开始和结束、上下文和 token 用量，不读取对话内容。' +
    'WorkBuddy 为每个会话单独启动一个进程，本轮完成数秒后即关闭；会话记录中本轮尚未结束且该进程仍在运行时显示为工作中。' +
    '等待你回答问题时，本地无记录，同样显示为工作中。子 agent 的记录不单独列出。' +
    '额度（Beta）为服务器随请求返回并记录在会话记录中的数值，按模型分别显示，仅每轮的第一次请求才会返回。',
  ),
};
// 本地数据里没有 token 用量的工具（Cursor）：不显示输入、输出两列，也不显示输出速度和共输出
const NO_TOKENS = new Set(['cursor']);

// 列的是正在运行的会话，还是最近 N 小时有活动的会话（对不上进程和会话的工具）
export function recentText(rt) {
  return rt.recent ? tr('最近 {n} 小时', { n: Math.round(rt.recent / 3600) }) : '';
}

// 会话数量的概括，例如“2 个工作中 · 1 个等你操作”
export function sessionSummary(sessions) {
  const busy = sessions.filter((s) => s.status === 'busy').length;
  const waiting = sessions.filter((s) => s.status === 'waiting').length;
  const parts = [busy ? tr('{n} 个工作中', { n: busy }) : null, waiting ? tr('{n} 个等你操作', { n: waiting }) : null].filter(Boolean);
  return parts.length ? parts.join(' · ') : sessions.length ? tr('全部空闲') : tr('无会话');
}

// 额度窗口的名字：300 分钟 → 5 小时额度，10080 分钟 → 每周额度；short 为 true 时不带“额度”（5 小时、每周），悬浮窗里用
export function limitName(minutes, { short = false } = {}) {
  if (minutes === 10080) return short ? tr('每周') : tr('每周额度');
  if (minutes && minutes % 1440 === 0) return short ? tr('{n} 天', { n: minutes / 1440 }) : tr('{n} 天额度', { n: minutes / 1440 });
  if (minutes && minutes % 60 === 0) return short ? tr('{n} 小时', { n: minutes / 60 }) : tr('{n} 小时额度', { n: minutes / 60 });
  if (minutes) return short ? tr('{n} 分钟', { n: minutes }) : tr('{n} 分钟额度', { n: minutes });
  return short ? '' : tr('额度');
}

// 还有效的额度窗口（5 小时、每周）：Codex 的过了重置时间就不算了，要等下次请求才有新数
export function liveLimits(rt, now) {
  const lim = rt.limits;
  return [lim?.primary, lim?.secondary].filter((w) => w && !(w.resets && w.resets < now));
}

// 还是新功能（Beta）的额度：Claude Code 的是 Claude 桌面版记下的套餐用量，并不准确；
// Antigravity 的是从它后台程序没公开的本机接口读的，升级后可能读不到；
// WorkBuddy 的是会话记录里服务器随请求返回的，字段的含义是照记录推断的
const BETA_TEXT = {
  'claude-desktop': tr(
    'Beta：读取自 Claude 桌面版记录的套餐用量数据，仅供参考。桌面版仅在启动时记录一次，24 小时内打开过托盘用量面板时每 15 分钟更新一次，' +
    '可能存在数小时延迟且无重置时间说明；记录超过 5 小时将不再显示 5 小时额度。',
  ),
  antigravity: tr(
    'Beta：每分钟从 Antigravity 后台程序（language_server）读取一次（调用其本机接口），若工具升级后接口变更可能无法获取。' +
    '额度按模型分组共享。',
  ),
  workbuddy: tr(
    'Beta：读取自 WorkBuddy 会话记录中服务器随请求返回的额度，仅每轮的第一次请求才会返回，显示的是最近一次返回时的数值，并非实时。' +
    '各字段的含义根据记录推断，仅供参考。',
  ),
};
export const isBetaLimits = (lim) => !!BETA_TEXT[lim?.source];
export const betaText = (lim) => BETA_TEXT[lim?.source] || '';

export function betaTag(lim) {
  return h('span', { class: 'beta-tag', text: 'Beta', title: betaText(lim) });
}

// 按模型分组的额度（Antigravity、WorkBuddy）：还没过重置时间的组。名字短写是第一个模型加“等”，完整的是全部模型
export function livePools(rt, now) {
  return (rt.limits?.pools || []).filter((p) => !(p.resets && p.resets < now));
}
export const poolName = (p, { full = false } = {}) => (full || p.names.length < 2 ? p.names.join(tr('、')) : tr('{name} 等', { name: p.names[0] }));

// 余额（按量付费的工具，现在只有 DeepSeek Harness）：金额两位小数，币种写成中文单位（英文界面是 CNY、USD）；低于设置里的预警线时提醒
const CURRENCY = { CNY: tr('元'), USD: tr('美元') };
export const currencyUnit = (c) => CURRENCY[c] || c || '';
export const money = (n) => num(n, 2);
export const balanceLow = (b) => !!b && b.warn != null && b.amount < b.warn;
export const BALANCE_NOTE = tr('余额数据由 DeepSeek Harness 的“余额小鲸鱼”插件（dsh-whale-widget）生成并记录在本地账本中（DeepSeek Harness 界面打开时约每 25 秒自动查询一次）。预警阈值可在设置页面调整。');

// 桌面版的额度记录是多久以前的：刚刚、12 分钟前、3 小时前、2 天前
export function recordAge(ms) {
  if (ms < 60000) return tr('刚刚');
  if (ms < 3600000) return tr('{n} 分钟前', { n: Math.floor(ms / 60000) });
  if (ms < 86400000) return tr('{n} 小时前', { n: Math.floor(ms / 3600000) });
  return tr('{n} 天前', { n: Math.floor(ms / 86400000) });
}

function resetText(t) {
  // 先向上取整到分钟再分段：直接拿原数分段的话，剩 59 分 50 秒会写成“60 分钟后重置”
  const m = Math.max(1, Math.ceil((t - Date.now()) / 60000));
  if (m < 60) return tr('{n} 分钟后重置', { n: m });
  if (m < 1440) return tr('{h} 小时 {m} 分后重置', { h: Math.floor(m / 60), m: m % 60 });
  return tr('{time} 重置', { time: when(t) });
}

function sessionState(s, now) {
  if (s.status === 'waiting') {
    const why = WAITING_TEXT[s.waiting_for] || s.waiting_for;
    return h(
      'span',
      { class: 'state waiting' },
      icon('warning'),
      h('strong', { text: ` ${tr('等你操作')}` }),
      why ? h('span', { class: 'muted', text: tr('（{why}）', { why }) }) : null,
    );
  }
  if (s.status === 'busy') return h('span', { class: 'state busy' }, h('strong', { text: tr('工作中') }), s.since ? ` · ${elapsed(now - s.since)}` : '');
  return h('span', { class: 'state muted', text: tr('空闲') });
}

// remote 的会话（ChatGPT 里选在本机执行的任务）对话在服务器上，读不到的字段写“不可见”，不是“—”
const unseen = () => h('span', { class: 'muted', title: tr('对话和模型位于 ChatGPT 服务器，本机无法获取'), text: tr('不可见') });

function agentView(r, gapMs) {
  const s = shell(r, r.label, ['mem', 'cpu']);
  const noTokens = NO_TOKENS.has(r.kind);
  const t = { sessions: tile(tr('会话')), speed: tile(tr('输出速度')), out: tile(tr('这些会话共输出')) };
  // 没有用量的工具：输出速度和共输出一直隐藏（磁贴留在页面里，按模型分组的额度磁贴插在输出速度前面）
  t.speed.hide(noTokens);
  t.out.hide(noTokens);
  // 额度（5 小时、每周）：Codex 和 Claude Code 有，其余工具一直隐藏。按模型分组的额度（Antigravity、WorkBuddy）每组一个磁贴，用到时再建
  const limits = [tile(tr('{n} 小时额度', { n: 5 }), { meter: true }), tile(tr('每周额度'), { meter: true })];
  limits.forEach((x) => x.hide(true));
  const poolTiles = [];
  // 余额：按量付费、读得到余额的工具（DeepSeek Harness）有，其余一直隐藏
  const balance = tile(tr('余额'));
  balance.hide(true);
  balance.el.title = BALANCE_NOTE;
  s.c.body.append(h('div', { class: 'tiles' }, t.sessions.el, balance.el, ...limits.map((x) => x.el), t.speed.el, t.out.el, ...s.res.tiles));

  const table = dataTable(
    [
      { label: tr('会话') },
      { label: tr('目录') },
      { label: tr('模型') },
      { label: tr('状态') },
      { label: tr('上下文'), cls: 'num-col' },
      ...usageCols({ label: tr('输入'), cls: 'num-col' }, { label: tr('输出'), cls: 'num-col' }),
      { label: tr('最后活动') },
    ],
    { empty: r.recent ? tr('{recent}无会话活动', { recent: recentText(r) }) : tr('无正在运行的会话') },
  );
  // 输入、输出两列：没有用量的工具不要这两列
  function usageCols(input, output) {
    return noTokens ? [] : [input, output];
  }
  const speed = noTokens ? null : speedChart(r.id, { title: tr('输出速度（tokens/s）'), floor: 20, tickFormat: (v) => tokens(v), gapMs });
  // 有会话在工作时，空闲的会话折叠在按钮后面
  let showIdle = false;
  let last = null;
  const moreLabel = h('span');
  const more = h('button', { class: 'more-btn', type: 'button', hidden: true }, chevron(), moreLabel);
  more.addEventListener('click', () => {
    showIdle = !showIdle;
    if (last) update(...last);
  });
  s.c.body.append(
    sectionTitle(r.recent ? tr('{recent}有活动的会话', { recent: recentText(r) }) : tr('正在运行的会话')),
    table.el,
    more,
    ...(speed ? [h('div', { class: 'charts agent-chart' }, speed.el)] : []),
    ...(AGENT_NOTE[r.kind] ? sourceNote(AGENT_NOTE[r.kind]) : []),
  );

  function update(rt, sample) {
    last = [rt, sample];
    const list = rt.sessions || [];
    const summary = sessionSummary(list);
    // 工作中、等你操作时状态写会话概括（“2 个工作中”）；空闲、读取出错、启动中这些照常写状态，不写成“无会话”
    const working = rt.status === 'busy' || rt.status === 'waiting';
    s.head(rt, sample, working ? summary : null);
    t.sessions.set(num(list.length), trc('数量单位', ' 个'), working || rt.status === 'idle' ? summary : statusText(rt.kind, rt.status));
    t.speed.set(num(rt.speed ?? 0, 1), ' tokens/s', tr('最近 1 分钟平均'));
    const sum = (k) => list.reduce((a, x) => a + (x.tokens?.[k] ?? 0), 0);
    const input = sum('input');
    t.out.set(tokens(sum('output')), ' tokens', input ? tr('输入 {input}，其中缓存 {pct}%', { input: tokens(input), pct: num((sum('cached') / input) * 100) }) : '');

    const b = rt.balance;
    balance.hide(!b);
    if (b) {
      // 低于预警线时和占用接近满载一样标黄、带提醒图标；下面一行是今天（北京时间，和插件记账的分天一样）花掉的钱和查询时间
      const low = balanceLow(b);
      const unit = currencyUnit(b.currency);
      balance.el.classList.toggle('is-warning', low);
      const info = [b.today != null ? tr('今天已用 {amount} {unit}', { amount: money(b.today), unit }) : null, tr('{age}查询', { age: recordAge(sample.t - b.t) })].filter(Boolean).join(' · ');
      // 磁贴窄，提醒的文字放在一个 span 里，换行时整段在图标右边折行
      balance.set(money(b.amount), ` ${unit}`, low ? [icon('warning'), h('span', { text: `${tr('低于预警 {amount} {unit}', { amount: money(b.warn), unit })} · ${info}` })] : info);
    }

    const lim = rt.limits || {};
    const beta = isBetaLimits(lim);
    [lim.primary, lim.secondary].forEach((w, i) => {
      const x = limits[i];
      x.hide(!w);
      if (!w) return;
      x.el.querySelector('.tile-label').replaceChildren(limitName(w.window), beta ? betaTag(lim) : '');
      if (w.resets && w.resets < sample.t) {
        x.set('—', '');
        x.level(0, tr('已重置，将在下次请求后同步'));
      } else {
        x.set(num(w.used), '%');
        // 桌面版的记录没有重置时间，写是什么时候记的
        x.level(ratioOf(w.used, 100), w.resets ? resetText(w.resets) : beta ? tr('{age}记录', { age: recordAge(sample.t - lim.t) }) : '');
        x.el.title = beta ? tr('桌面版记录时间：{time}', { time: when(lim.t) }) : '';
      }
    });
    const pools = lim.pools || [];
    while (poolTiles.length < pools.length) {
      const x = tile('', { meter: true });
      t.speed.el.before(x.el);
      poolTiles.push(x);
    }
    poolTiles.forEach((x, i) => {
      const p = pools[i];
      x.hide(!p);
      if (!p) return;
      x.el.querySelector('.tile-label').replaceChildren(tr('{name} 额度', { name: poolName(p) }), betaTag(lim));
      x.el.title = `${tr('共用此额度的模型：')}\n${p.models.join('\n')}`;
      if (p.resets && p.resets < sample.t) {
        x.set('—', '');
        // Antigravity 的每分钟读一次；WorkBuddy 的要等下一轮的第一次请求
        x.level(0, lim.source === 'workbuddy' ? tr('已重置，将在下次请求后同步') : tr('已重置，将在下次读取后同步'));
      } else {
        x.set(num(p.used), '%');
        x.level(ratioOf(p.used, 100), p.resets ? resetText(p.resets) : '');
      }
    });

    const now = sample.t;
    const active = list.filter((x) => x.status !== 'idle' || (x.last && now - x.last < SESSION_HOLD_MS));
    const idle = list.length - active.length;
    const folding = active.length > 0 && idle > 0;
    more.hidden = !folding;
    more.setAttribute('aria-expanded', String(showIdle));
    moreLabel.textContent = showIdle ? tr('收起空闲会话') : tr('显示另外 {n} 个空闲会话', { n: idle });

    table.set(
      (folding && !showIdle ? active : list).map((x) => {
        const ctx = x.context == null ? '—' : x.context_window ? `${tokens(x.context)} / ${tokens(x.context_window)}` : tokens(x.context);
        const cr = ratioOf(x.context, x.context_window);
        const pct = cr != null ? h('span', { class: 'muted', text: ` · ${percent(cr)}` }) : null;
        const cache = x.tokens?.input ? h('span', { class: 'muted', text: ` · ${tr('缓存 {pct}%', { pct: num((x.tokens.cached / x.tokens.input) * 100) })}` }) : null;
        const folder = (x.cwd || '').split(/[\\/]/).filter(Boolean).pop() || '—';
        const sub = [x.entry ? ENTRY_TEXT[x.entry] || x.entry : null, x.started ? tr('{time} 开始', { time: when(x.started) }) : null].filter(Boolean).join(' · ');
        const name = [h('strong', { text: x.name }), sub ? h('span', { class: 'muted', text: ` ${sub}` }) : null];
        if (x.remote) return [name, unseen(), unseen(), sessionState(x, now), unseen(), ...usageCols(unseen(), unseen()), when(x.last)];
        return [
          name,
          h('span', { title: x.cwd || '', text: folder }),
          x.model || '—',
          sessionState(x, now),
          [ctx, pct],
          ...usageCols([tokens(x.tokens?.input), cache], tokens(x.tokens?.output)),
          when(x.last),
        ];
      }),
    );
  }

  return { el: s.c.el, card: s.c, charts: speed ? [speed] : [], update };
}

// ---------- 正在运行的模型：各服务和工具里的模型汇总成一张表（看板顶部和悬浮窗共用） ----------

// 列最近 3 小时会话的工具，空闲超过这么久的会话不算正在用的模型
const MODEL_IDLE_MS = 30 * 60 * 1000;
const MODEL_ORDER = { waiting: 0, busy: 1, loading: 2, idle: 3 };
const COMFY_KIND = { lora: 'LoRA', clip: tr('文本编码器'), vae: 'VAE', controlnet: 'ControlNet', upscale: tr('放大模型'), other: tr('其他') };
const AGENT_STATE = { waiting: tr('等你操作'), busy: tr('工作中'), idle: tr('空闲') };

// 模型文件名去掉目录和扩展名
const fileTitle = (name) => name.split(/[\\/]/).pop().replace(/\.[^.]+$/, '');

// “4 分钟后卸载”“即将卸载”“常驻”
export function unloadText(t) {
  return until(t, { unload: true });
}

// Ollama 的模型在哪：全在 CPU 上写 CPU，部分在 CPU 上写“显卡 + CPU”，读不到用的显卡时是 null
function ollamaWhere(m, r, procs) {
  if (m.gpu_ratio != null && m.gpu_ratio <= 0) return { gpu: 'CPU' };
  const gpus = usedGpus(procs, r, m.pid);
  const text = gpuText(gpus);
  if (!text) return {};
  return { gpu: m.gpu_ratio != null && m.gpu_ratio < 1 ? `${text} + CPU` : text, gpuTitle: gpuText(gpus, { full: true }) };
}

// Ollama 模型放在显卡上的比例，简短写法：100% GPU、63% GPU，全在 CPU 上写 100% CPU；读不到时是 null。
// 和模型表里的分配条一样先取整，不会出现“0% GPU”。“正在运行的模型”和悬浮窗共用
export function gpuShareText(ratio) {
  if (ratio == null) return null;
  const gpu = Math.round(ratio * 100);
  return gpu >= 100 ? '100% GPU' : gpu <= 0 ? '100% CPU' : `${gpu}% GPU`;
}

function ollamaModels(r, procs) {
  return (r.models || []).map((m) => {
    const gpu = gpuShareText(m.gpu_ratio);
    const ctx = ratioOf(m.context, m.context_window);
    const state =
      m.state === 'generating'
        ? [tr('生成中'), m.speed != null ? `${num(m.speed, 1)} tokens/s` : null].filter(Boolean).join(' · ')
        : { prompt: tr('处理提示词'), loading: tr('加载中') }[m.state] || tr('已加载');
    return {
      name: m.name,
      ...ollamaWhere(m, r, procs),
      status: m.state === 'generating' || m.state === 'prompt' ? 'busy' : m.state === 'loading' ? 'loading' : 'idle',
      state,
      detail: [
        [m.params, m.quant].filter(Boolean).join(' · '),
        m.size ? bytes(m.size) : null,
        gpu,
        ctx != null ? tr('上下文 {pct}', { pct: percent(ctx) }) : null,
        m.state === 'loading' ? null : unloadText(m.expires),
      ]
        .filter(Boolean)
        .join(' · '),
    };
  });
}

// ComfyUI 读不到显存里有哪些模型，用正在执行的任务（空闲时是上一个任务）的加载器节点
export function comfyModels(r) {
  const files = r.job_models?.files || [];
  if (!files.length) return [];
  const main = files.filter((f) => f.kind === 'model');
  const shown = main.length ? main : files.slice(0, 1);
  const groups = {};
  for (const f of files) if (!shown.includes(f)) (groups[f.kind] ||= []).push(fileTitle(f.name));
  const current = r.job_models.current;
  return [
    {
      name: shown.map((f) => fileTitle(f.name)).join(' + '),
      title: shown.map((f) => f.name).join('\n'),
      status: current ? 'busy' : 'idle',
      state: current ? tr('执行中') : tr('上次任务'),
      detail: Object.keys(COMFY_KIND)
        .filter((k) => groups[k])
        .map((k) => tr('{kind}：{names}', { kind: COMFY_KIND[k], names: groups[k].join(tr('、')) }))
        .join(' · '),
    },
  ];
}

// 命令行工具：同一个模型的会话合成一行，详情只列进行中的会话
function agentModels(r, now) {
  const groups = new Map();
  for (const s of r.sessions || []) {
    if (!s.model) continue;
    if (r.recent && s.status === 'idle' && !(s.last && now - s.last < MODEL_IDLE_MS)) continue;
    if (!groups.has(s.model)) groups.set(s.model, []);
    groups.get(s.model).push(s);
  }
  return [...groups].map(([model, list]) => {
    const status = list.some((s) => s.status === 'waiting') ? 'waiting' : list.some((s) => s.status === 'busy') ? 'busy' : 'idle';
    const active = list.filter((s) => s.status !== 'idle');
    return {
      name: model,
      status,
      state: AGENT_STATE[status],
      detail: active.length ? tr('{summary}：{names}', { summary: sessionSummary(active), names: active.map((s) => s.name).join(tr('、')) }) : '',
    };
  });
}

// ComfyUI 的模型在它的进程用的显卡上
function comfyWhere(r, procs) {
  const gpus = usedGpus(procs, r);
  return gpuText(gpus) ? { gpu: gpuText(gpus), gpuTitle: gpuText(gpus, { full: true }) } : {};
}

// [{ name, title, source, gpu, gpuTitle, status, state, detail }]，等你操作、工作中的排在前面。
// gpu：本地模型运行在哪块显卡上（或 CPU），procs 是样本里的按进程数据
export function runningModels(runtimes, now, procs) {
  const out = [];
  for (const r of runtimes || []) {
    const items =
      r.kind === 'ollama'
        ? ollamaModels(r, procs)
        : r.kind === 'comfyui'
          ? comfyModels(r).map((m) => ({ ...m, ...comfyWhere(r, procs) }))
          : r.category === 'cli'
            ? agentModels(r, now)
            : [];
    for (const m of items) out.push({ ...m, source: r.label });
  }
  return out.sort((a, b) => MODEL_ORDER[a.status] - MODEL_ORDER[b.status]);
}

function runningState(m) {
  if (m.status === 'waiting') return h('span', { class: 'state waiting' }, icon('warning'), h('strong', { text: ` ${m.state}` }));
  if (m.status === 'busy') return h('span', { class: 'state busy' }, h('strong', { text: m.state }));
  if (m.status === 'loading') return h('span', { class: 'state' }, h('strong', { text: m.state }));
  return h('span', { class: 'state muted', text: m.state });
}

export function modelsView() {
  const c = card(tr('正在运行的模型'), '', { wide: true });
  const table = dataTable([{ label: tr('模型') }, { label: tr('运行在') }, { label: tr('状态') }, { label: tr('详情'), cls: 'wrap-col' }], {
    empty: tr('当前无正在运行的模型。Ollama 加载模型、ComfyUI 执行任务或命令行工具处于活跃会话时将在此展示。'),
  });
  c.body.append(
    table.el,
    ...sourceNote(tr('Ollama 显示的是已加载的模型；ComfyUI 无法获取显存中的模型列表，显示的是正在执行的任务所用的模型，空闲时为上一个任务所用的模型；命令行工具按模型合并会话。本地模型后标注其使用的显卡，根据模型进程占用的显存判断。')),
    ...diagToggle(),
  );
  return {
    el: c.el,
    update(sample) {
      const list = runningModels(sample.runtimes, sample.t, sample.procs);
      const count = (status) => list.filter((m) => m.status === status).length;
      const parts = [count('busy') ? tr('{n} 个工作中', { n: count('busy') }) : null, count('waiting') ? tr('{n} 个等你操作', { n: count('waiting') }) : null];
      c.subEl.textContent = list.length ? [trc('模型数', '{n} 个', { n: list.length }), ...parts].filter(Boolean).join(' · ') : '';
      table.set(
        list.map((m) => [
          h('strong', { text: m.name, title: m.title || m.name }),
          [m.source, m.gpu ? h('span', { class: 'muted', text: ` · ${m.gpu}`, title: m.gpuTitle || '' }) : null],
          runningState(m),
          m.detail || '—',
        ]),
      );
    },
  };
}

function genericView(r) {
  const s = shell(r, r.label);
  s.c.body.append(h('div', { class: 'tiles' }, ...s.res.tiles));
  return {
    el: s.c.el,
    card: s.c,
    charts: [],
    update(rt, sample) {
      s.head(rt, sample);
    },
  };
}

const VIEWS = {
  ollama: ollamaView,
  comfyui: comfyView,
  'claude-code': agentView,
  codex: agentView,
  dsh: agentView,
  'grok-build': agentView,
  opencode: agentView,
  openclaw: agentView,
  antigravity: agentView,
  cursor: agentView,
  workbuddy: agentView,
};

export function runtimeView(r, gapMs) {
  return (VIEWS[r.kind] || genericView)(r, gapMs);
}
