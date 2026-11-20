// 悬浮窗：显卡、CPU、内存的关键数字，加上每个模型服务的当前状态、速度和正在运行的模型。
// 小窗模式（地址带 ?compact=1）不画进度条和折线，硬件只给数字，服务块去掉补充行。
// 在桌面程序里运行时，window.pywebview.api 提供“隐藏”“打开完整看板”“按内容调整高度”“切换小窗模式”。

import { GB, h, num, gb, rate, ratioOf, usageLevel, percent, icon, when, elapsed, pushSample } from './ui.js';
import {
  statusText,
  sessionSummary,
  limitName,
  liveLimits,
  livePools,
  poolName,
  isBetaLimits,
  betaTag,
  betaText,
  recordAge,
  currencyUnit,
  money,
  balanceLow,
  BALANCE_NOTE,
  recentText,
  activeRuntimeIds,
  comfyModels,
  gpuShareText,
  unloadText,
  recentDone,
  dismissDone,
  doneText,
  resultOf,
  staleAge,
  setGpuDevices,
  setSampleInterval,
  gpuText,
  usedGpus,
  mainGpus,
} from './runtimes.js';
import { connectStream } from './stream.js';
import { tr, followLanguage } from './i18n.js';

const SPARK_SECONDS = 60;
const params = new URLSearchParams(location.search);
// history：最近一小段精简样本，只有最新一条是完整的
// windowHidden：桌面程序把悬浮窗隐藏了（见 setWindowVisible）
// deviceName：设置里给这台电脑起的名字，undefined 是还没收到数据
// integrated：主显卡是核显，没有独显的电脑才会这样
const state = { history: [], lastHeight: 0, compact: params.get('compact') === '1', windowHidden: false, deviceName: undefined, integrated: false };
const api = () => window.pywebview?.api;
// 看不见时只收数据、不渲染。悬浮窗被隐藏时 WebView2 里的页面仍算可见（document.hidden 不变），
// 要靠桌面程序通知；document.hidden 管的是在浏览器里打开、切到别的标签页的情况
const hidden = () => document.hidden || state.windowHidden;

const theme = params.get('theme');
if (theme === 'dark' || theme === 'light') document.documentElement.dataset.theme = theme;

// ---------- 硬件部分（结构固定，只更新数值） ----------

// “空闲 xx GB”，读不到已用或总量时没有
const freeText = (used, total) => (ratioOf(used, total) != null ? tr('空闲 {v} GB', { v: gb(total - used) }) : null);

// 只有核显时，“显存”其实是借用的内存（核显没有自己的显存），名字写成“共享显存（借用内存）”；
// 小窗模式放不下，写“共享显存”。鼠标停上去看原因
const BORROWED_TITLE = tr('核显无独立显存，使用借用的内存（任务管理器中称为“GPU 内存”）');

// wide：名字放不进标签列（共享显存（借用内存）），名字和数值占一行，进度条放到下一行，和其他行的进度条对齐
function row(label, { wide = false, title = null } = {}) {
  const fill = h('div', { class: 'm-fill' });
  const value = h('span', { class: 'm-value' });
  const labelEl = h('span', { class: 'm-label', text: label, title });
  const bar = h('div', { class: 'm-bar' }, fill);
  const el = h('div', { class: wide ? 'm-row wide' : 'm-row' }, ...(wide ? [labelEl, value, bar] : [labelEl, bar, value]));
  return {
    el,
    set(ratio, text, unit = '', { level = false } = {}) {
      const pct = ratio == null ? 0 : Math.min(Math.max(ratio * 100, 0), 100);
      fill.style.width = `${pct}%`;
      // 占用 90% 以上提醒，98% 以上告急（见 usageLevel）
      const lv = level ? usageLevel(ratio) : null;
      el.classList.toggle('is-warning', lv === 'warning');
      el.classList.toggle('is-critical', lv === 'critical');
      value.replaceChildren(text, h('span', { class: 'unit', text: unit }));
    },
  };
}

const sub = () => h('div', { class: 'm-sub' });

// 显卡的几行按主显卡生成（见 buildGpus），放在 CPU 前面
const hw = { cpu: row('CPU'), cpuSub: sub(), mem: row(tr('内存')) };
const hwSection = h('section', { class: 'm-section m-hw' }, hw.cpu.el, hw.cpuSub, hw.mem.el);

// 小窗模式：两列，左边百分比，右边用量；鼠标停上去看其余细节。wide：名字比“显存”长，不按固定宽度
function cell(label, { wide = false } = {}) {
  const value = h('span', { class: 'c-value' });
  const el = h('div', { class: 'c-cell' }, h('span', { class: wide ? 'm-label wide' : 'm-label', text: label }), value);
  return {
    el,
    // extra：数字后面的灰色补充（总量、温度）；mark：数字前的提醒图标
    set(text, extra = '', { title = '', mark = null } = {}) {
      value.replaceChildren(...(mark ? [icon(mark)] : []), h('span', { class: 'c-num', text }), h('span', { class: 'c-extra', text: extra }));
      el.title = title;
    },
  };
}

const cells = { cpu: cell('CPU'), mem: cell(tr('内存')) };
const compactSection = h('section', { class: 'm-section c-grid', hidden: true }, cells.cpu.el, cells.mem.el);

// 每块主显卡一组：有独显时是所有独显，没有独显时是核显。只有一块时和以前一样叫“GPU”“显存”（核显是“共享显存（借用内存）”），
// 几块时编号（GPU1、显存1…），补充行开头写显卡的短名字。完整模式和小窗模式的都建好，按模式显示
let gpuGroups = [];
let gpuKey = null;

function buildGpus(devices) {
  const main = mainGpus(devices);
  state.integrated = main.length > 0 && !!devices[main[0]]?.integrated;
  const key = main.map((i) => devices[i]?.name).join('|');
  if (key === gpuKey) return;
  gpuKey = key;
  for (const g of gpuGroups) g.els.forEach((el) => el.remove());
  const multi = main.length > 1;
  // 编号的标签（显存1）比“显存”宽，标签列跟着放宽
  hwSection.classList.toggle('multi-gpu', multi);
  compactSection.classList.toggle('multi-gpu', multi);
  const borrowed = state.integrated;
  gpuGroups = main.map((index, n) => {
    const tag = multi ? String(n + 1) : '';
    const g = {
      index,
      multi,
      gpu: row(`GPU${tag}`),
      gpuSub: sub(),
      spark: h('canvas', { class: 'm-spark', role: 'img', 'aria-label': multi ? tr('最近 60 秒显卡 {n} 利用率', { n: tag }) : tr('最近 60 秒显卡利用率') }),
      vram: borrowed ? row(tr('共享显存{n}（借用内存）', { n: tag }), { wide: true, title: BORROWED_TITLE }) : row(tr('显存{n}', { n: tag })),
      vramSub: sub(),
      gpuCell: cell(`GPU${tag}`),
      vramCell: borrowed ? cell(tr('共享显存{n}', { n: tag }), { wide: true }) : cell(tr('显存{n}', { n: tag })),
    };
    g.els = [g.gpu.el, g.gpuSub, g.spark, g.vram.el, g.vramSub, g.gpuCell.el, g.vramCell.el];
    hw.cpu.el.before(g.gpu.el, g.gpuSub, g.spark, g.vram.el, g.vramSub);
    cells.cpu.el.before(g.gpuCell.el, g.vramCell.el);
    return g;
  });
  state.lastHeight = 0;
}
const rtSection = h('section', { class: 'm-section' });
document.getElementById('mini').append(rtSection, hwSection, compactSection);

// 点完成提示不再显示。块每秒重建，用按下而不是 click，免得按下和松开之间节点被换掉
rtSection.addEventListener('pointerdown', (e) => {
  const el = e.target.closest('[data-done]');
  const last = state.history[state.history.length - 1];
  const r = el && last?.runtimes?.find((x) => x.id === el.dataset.done);
  if (!r) return;
  dismissDone(r);
  render();
});

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// 迷你折线：只有面积和折线，底部一条基线
function drawSpark(canvas, points, t0, t1) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const ht = canvas.clientHeight;
  if (!w || !ht) return;
  if (canvas.width !== Math.round(w * dpr)) canvas.width = Math.round(w * dpr);
  if (canvas.height !== Math.round(ht * dpr)) canvas.height = Math.round(ht * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, ht);
  const color = cssVar('--series-1');
  ctx.strokeStyle = cssVar('--grid');
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, ht - 0.5);
  ctx.lineTo(w, ht - 0.5);
  ctx.stroke();
  const x = (t) => ((t - t0) / (t1 - t0)) * (w - 4) + 1;
  const y = (v) => 3 + (1 - Math.min(Math.max(v, 0), 100) / 100) * (ht - 5);
  const segs = [];
  let seg = [];
  let prev = null;
  for (const p of points) {
    if (p.v == null || (prev != null && p.t - prev > 3000)) {
      if (seg.length) segs.push(seg);
      seg = [];
    }
    if (p.v != null) seg.push(p);
    prev = p.t;
  }
  if (seg.length) segs.push(seg);
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  for (const s of segs) {
    ctx.beginPath();
    ctx.moveTo(x(s[0].t), ht - 1);
    for (const p of s) ctx.lineTo(x(p.t), y(p.v));
    ctx.lineTo(x(s[s.length - 1].t), ht - 1);
    ctx.closePath();
    ctx.globalAlpha = 0.1;
    ctx.fillStyle = color;
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.beginPath();
    s.forEach((p, i) => (i ? ctx.lineTo(x(p.t), y(p.v)) : ctx.moveTo(x(p.t), y(p.v))));
    ctx.lineWidth = 2;
    ctx.strokeStyle = color;
    ctx.stroke();
  }
}

const gpuDetail = (g) =>
  [g.temp != null ? `${g.temp}°C` : null, g.power != null ? `${num(g.power, 0)} W` : null, g.clock_gr ? `${num(g.clock_gr)} MHz` : null].filter(Boolean).join(' · ');
// CPU 温度要开着 LibreHardwareMonitor 才有，没有时不写
const cpuDetail = (c) =>
  [c.temp != null ? `${num(c.temp)}°C` : null, c.power != null ? `${num(c.power, 0)} W` : null, c.freq_mhz ? `${num(c.freq_mhz / 1000, 2)} GHz` : null]
    .filter(Boolean)
    .join(' · ');
// 显存不够、借用了内存（共享显存）：一块主显卡时是按进程表里各进程的合计，几块时用后台按显卡分开的合计。
// 核显本来就用借来的内存，不算“溢出”
function sharedVram(s, g) {
  if (state.integrated) return 0;
  if (g.multi) return s.procs?.shared?.[g.index] || 0;
  return (s.procs?.rows || []).reduce((sum, p) => sum + (p.shared || 0), 0);
}

// 几块主显卡时补充行开头写显卡的短名字，鼠标提示里写完整名字
const gpuName = (g, full = false) => (g.multi ? gpuText([g.index], { full }) : null);

function updateHardware(s) {
  if (state.compact) updateCompact(s);
  else updateFull(s);
}

function updateCompact(s) {
  for (const grp of gpuGroups) {
    const g = s.gpus?.[grp.index];
    if (g) {
      const name = gpuName(grp, true);
      grp.gpuCell.set(`${num(g.util)}%`, g.temp != null ? ` ${g.temp}°C` : '', { title: [name, gpuDetail(g)].filter(Boolean).join('\n') });
      // 桌面程序本身也会用一点共享显存，超过 1 GB 才提示（通常是模型溢出）
      const shared = sharedVram(s, grp);
      const detail = [freeText(g.mem_used, g.mem_total), shared > GB ? tr('共享显存 {v} GB', { v: gb(shared) }) : null].filter(Boolean).join(' · ');
      grp.vramCell.set(gb(g.mem_used), `/${gb(g.mem_total)}G`, {
        mark: usageLevel(ratioOf(g.mem_used, g.mem_total)) || (shared > GB ? 'warning' : null),
        title: [name, state.integrated ? BORROWED_TITLE : null, detail].filter(Boolean).join('\n'),
      });
    }
    grp.gpuCell.el.hidden = grp.vramCell.el.hidden = !g;
  }
  // 这一次没采到（采集出错）时显示“—”，不停在上一次的数字上
  const cpu = s.cpu || {};
  const mem = s.mem || {};
  cells.cpu.set(`${num(cpu.percent)}%`, cpu.temp != null ? ` ${num(cpu.temp)}°C` : '', { title: cpuDetail(cpu) });
  cells.mem.set(gb(mem.used), `/${gb(mem.total, 0)}G`, { mark: usageLevel(ratioOf(mem.used, mem.total)) });
}

function updateFull(s) {
  const t1 = s.t;
  const t0 = t1 - SPARK_SECONDS * 1000;
  for (const grp of gpuGroups) {
    const g = s.gpus?.[grp.index];
    if (g) {
      grp.gpu.set(ratioOf(g.util, 100), num(g.util), '%');
      grp.gpuSub.textContent = [gpuName(grp), gpuDetail(g)].filter(Boolean).join(' · ');
      grp.gpuSub.title = gpuName(grp, true) || '';
      grp.vram.set(ratioOf(g.mem_used, g.mem_total), gb(g.mem_used), ` / ${gb(g.mem_total)} GB`, { level: true });
      // 桌面程序本身也会用一点共享显存，超过 1 GB 才提示（通常是模型溢出）
      const shared = sharedVram(s, grp);
      const spill = shared > GB;
      grp.vramSub.replaceChildren(
        ...(spill ? [icon('warning')] : []),
        [spill ? tr('共享显存 {v} GB', { v: gb(shared) }) : null, freeText(g.mem_used, g.mem_total)].filter(Boolean).join(' · '),
      );
      drawSpark(
        grp.spark,
        state.history.filter((x) => x.t >= t0 - 3000).map((x) => ({ t: x.t, v: x.gpus?.[grp.index]?.util ?? null })),
        t0,
        t1,
      );
    }
    grp.gpu.el.hidden = grp.gpuSub.hidden = grp.spark.hidden = grp.vram.el.hidden = grp.vramSub.hidden = !g;
  }

  // 这一次没采到时显示“—”，见 updateCompact
  const cpu = s.cpu || {};
  const mem = s.mem || {};
  hw.cpu.set(ratioOf(cpu.percent, 100), num(cpu.percent), '%');
  hw.cpuSub.textContent = cpuDetail(cpu);
  hw.mem.set(ratioOf(mem.used, mem.total), gb(mem.used), ` / ${gb(mem.total, 0)} GB`, { level: true });
}

// ---------- 模型服务（每次重建，节点很少） ----------

// title：鼠标停在文字上时显示的完整内容（模型名太长会被截断）
function main(big, unit, label, title) {
  return h(
    'div',
    { class: 'rt-main' },
    big != null ? h('span', { class: 'rt-big', text: big }) : null,
    unit ? h('span', { class: 'rt-unit', text: unit }) : null,
    label ? h('span', { class: 'rt-label', text: label, title: title || label }) : null,
  );
}

function ollamaBody(r) {
  const ms = r.models || [];
  const gen = ms.find((m) => m.state === 'generating');
  const prompt = ms.find((m) => m.state === 'prompt');
  const loading = ms.find((m) => m.state === 'loading');
  const last = (r.requests || [])[0];
  const lastText = last
    ? tr('上次 {speed} tokens/s · {n} tokens', { speed: num(rate(last.gen_tokens, last.gen_ms), 1), n: num(last.gen_tokens) })
    : tr('暂无请求记录');
  // “上下文 8.2%”：空闲时是模型还留着的上一段对话占的；读不到时是 null
  const ctxText = (m) => {
    const ratio = ratioOf(m.context, m.context_window);
    return ratio != null ? tr('上下文 {pct}', { pct: percent(ratio) }) : null;
  };
  if (gen) return [main(r.speed != null ? num(r.speed, 1) : '—', 'tokens/s', gen.name), [tr('已生成 {n} tokens', { n: num(gen.n_decoded) }), ctxText(gen)].filter(Boolean).join(' · ')];
  if (prompt) return [main(null, null, [`${prompt.name} · ${tr('正在处理提示词')}`, ctxText(prompt)].filter(Boolean).join(' · ')), lastText];
  if (loading) return [main(null, null, tr('正在加载 {name}', { name: loading.name })), lastText];
  if (ms.length) {
    const m = ms[0];
    const gpu = gpuShareText(m.gpu_ratio);
    const extra = ms.length > 1 ? tr('（共 {n} 个）', { n: ms.length }) : '';
    return [main(null, null, [m.name + extra, gpu, ctxText(m), unloadText(m.expires)].filter(Boolean).join(' · ')), lastText];
  }
  return [main(null, null, tr('未加载模型')), lastText];
}

function comfyBody(r) {
  const q = r.queue || {};
  const pending = q.pending ? ` · ${tr('排队 {n}', { n: q.pending })}` : '';
  const secs = (ms) => (ms == null ? '—' : tr('{n} 秒', { n: num(ms / 1000, 1) }));
  const subText = tr('上次 {last} · 平均 {avg}', { last: secs(r.last_duration), avg: secs(r.avg_duration) });
  if (r.current) {
    // 已运行的秒数后面跟正在用的模型，鼠标停上去能看到 LoRA 等其余模型
    const sec = r.current.elapsed != null ? Math.floor(r.current.elapsed / 1000) : null;
    const m = r.job_models?.current ? comfyModels(r)[0] : null;
    const title = m ? [m.title, m.detail].filter(Boolean).join('\n') : null;
    return [main(sec != null ? String(sec) : '—', tr('秒'), m ? m.name : tr('已运行'), title), `${subText}${pending}`];
  }
  return [main(null, null, `${tr('空闲')}${pending}`), subText];
}

// 命令行工具：进行中的会话数、输出速度和它们用的模型，下面一行是在等你操作的会话或正在工作的会话（额度另起一行，见 quotaLine）
function agentBody(r) {
  const list = r.sessions || [];
  const active = list.filter((s) => s.status !== 'idle');
  const names = (status) => list.filter((s) => s.status === status).map((s) => s.name).join(tr('、'));
  const models = [...new Set(active.map((s) => s.model).filter(Boolean))].join(tr('、'));
  // 速度是各会话之和，多个会话进行中时注明“共”；放在模型名前面，窗口窄时截掉的是模型名
  const speed = r.speed > 0 ? (active.length > 1 ? tr('共 {n} tokens/s', { n: num(r.speed, 1) }) : `${num(r.speed, 1)} tokens/s`) : null;
  const body = active.length
    ? main(String(active.length), tr('个进行中'), [speed, models].filter(Boolean).join(' · '))
    : main(null, null, list.length ? tr('全部空闲') : r.recent ? tr('{recent}无会话记录', { recent: recentText(r) }) : tr('无正在运行的会话'));
  const last = Math.max(0, ...list.map((s) => s.last || 0));
  const subText = names('waiting') ? tr('等你操作：{names}', { names: names('waiting') }) : names('busy') || (last ? tr('上次活动 {time}', { time: when(last) }) : '');
  return [body, subText];
}

// 额度的各项：5 小时、每周（Codex、Claude Code），或按模型分的组（Antigravity、WorkBuddy，用得最多的在前，放不下时截掉的是用得少的）
function quotaItems(r, now) {
  const pools = livePools(r, now);
  if (pools.length) {
    return [...pools].sort((a, b) => b.used - a.used).map((p) => ({ short: poolName(p), full: poolName(p, { full: true }), used: p.used, resets: p.resets }));
  }
  return liveLimits(r, now).map((w) => ({ short: limitName(w.window, { short: true }), full: limitName(w.window), used: w.used, resets: w.resets }));
}

// 额度一行：“额度 5 小时 23% · 每周 4%”，快用完时前面带提醒图标；鼠标停上去看重置时间。
// Claude Code、Antigravity、WorkBuddy 的标上 Beta，Claude Code 的是桌面版记下的，还写是多久以前的记录。
// named：空闲的工具不显示块，这一行开头写工具名字
function quotaLine(r, now, { named = false } = {}) {
  const items = quotaItems(r, now);
  if (!items.length) return null;
  const lim = r.limits;
  const beta = isBetaLimits(lim);
  const desktop = lim.source === 'claude-desktop';
  const text = [items.map((x) => `${x.short} ${num(x.used)}%`).join(' · '), desktop ? tr('{age}记录', { age: recordAge(now - lim.t) }) : null].filter(Boolean).join(' · ');
  const reset = (x) => (x.resets ? tr('，{time} 重置', { time: when(x.resets) }) : '');
  const title = [
    ...items.map((x) => `${tr('{name} 已用 {pct}%', { name: x.full, pct: num(x.used) })}${reset(x)}`),
    desktop ? tr('桌面版记录时间：{time}', { time: when(lim.t) }) : null,
    betaText(lim) || null,
  ]
    .filter(Boolean)
    .join('\n');
  const mark = usageLevel(Math.max(...items.map((x) => ratioOf(x.used, 100) ?? 0)));
  return h(
    'div',
    { class: named ? 'rt-sub rt-quota m-quota' : 'rt-sub rt-quota', title },
    ...(mark ? [icon(mark)] : []),
    named ? h('span', { class: 'rt-quota-name', text: r.label }) : null,
    tr('额度'),
    beta ? betaTag(lim) : null,
    ` ${text}`,
  );
}

// 几段文字用“ · ”隔开，窗口窄时只在“ · ”后面换行，每一段本身不拆开（“·”前面是不换行的空格）
function joinParts(parts) {
  return parts.flatMap((text, i) => [i ? '\u00a0· ' : null, h('span', { class: 'nowrap', text })]);
}

// 余额一行：“余额 39.46 元 · 今天已用 5.22 元”，低于预警线时前面带提醒图标、写明预警线；
// 账本 5 分钟没更新（DeepSeek Harness 的界面没开，插件没去查）时写是多久以前查的。named 和额度一行一样。
// 窗口窄放不下时换到下一行，不截断
const BALANCE_FRESH_MS = 5 * 60 * 1000;
function balanceLine(r, now, { named = false } = {}) {
  const b = r.balance;
  if (!b) return null;
  const unit = currencyUnit(b.currency);
  const low = balanceLow(b);
  const parts = [
    tr('余额 {amount} {unit}', { amount: money(b.amount), unit }),
    low ? tr('低于预警 {amount} {unit}', { amount: money(b.warn), unit }) : null,
    b.today != null ? tr('今天已用 {amount} {unit}', { amount: money(b.today), unit }) : null,
    now - b.t > BALANCE_FRESH_MS ? tr('{age}查询', { age: recordAge(now - b.t) }) : null,
  ].filter(Boolean);
  return h(
    'div',
    { class: named ? 'rt-sub rt-quota rt-wrap m-quota' : 'rt-sub rt-quota rt-wrap', title: `${tr('余额查询时间：{time}', { time: when(b.t) })}\n${BALANCE_NOTE}` },
    ...(low ? [icon('warning')] : []),
    named ? h('span', { class: 'rt-quota-name nowrap', text: r.label }) : null,
    ...joinParts(parts),
  );
}

// 块里的“xxx 已完成 · 2 分钟前”，只显示最新一条，有新的就顶替掉，点一下不再显示
function doneLines(r, now) {
  return recentDone(r)
    .slice(0, 1)
    .map((e) => h('div', { class: 'rt-done', 'data-done': r.id, title: tr('点击关闭提示') }, icon(resultOf(e).icon), doneText(e, now, { short: true })));
}

// 多久没更新，取整到秒、分钟或小时，悬浮窗里要短
const shortAgo = (ms) =>
  ms < 60000 ? tr('{n} 秒', { n: Math.floor(ms / 1000) }) : ms < 3600000 ? tr('{n} 分钟', { n: Math.floor(ms / 60000) }) : tr('{n} 小时', { n: Math.floor(ms / 3600000) });

// 名字后面的显卡短名字，鼠标停上去看完整名字；Ollama 的模型全在 CPU 上时写 CPU。命令行工具的模型在云端，不标
function gpuTag(r, sample) {
  if (r.category === 'cli') return null;
  const gpus = usedGpus(sample.procs, r);
  const text = gpuText(gpus);
  if (text) return h('span', { class: 'rt-gpu', text, title: tr('运行在 {name}', { name: gpuText(gpus, { full: true }) }) });
  const ms = r.models || [];
  if (ms.length && ms.every((m) => m.gpu_ratio != null && m.gpu_ratio <= 0)) return h('span', { class: 'rt-gpu', text: 'CPU', title: tr('模型均在 CPU 上') });
  return null;
}

// doneOnly：空闲但刚完成过任务的，只显示名字和完成提示
function rtBlock(r, sample, { doneOnly = false } = {}) {
  const status = r.status || 'idle';
  const cli = r.category === 'cli';
  // 命令行工具工作中、等你操作时写会话概括；读取出错、连不上、启动中照常写状态，不写成“无会话”
  const label = cli && (status === 'busy' || status === 'waiting') ? sessionSummary(r.sessions || []) : statusText(r.kind, status);
  const failed = status === 'offline' || status === 'error';
  // 数据过期（轮询很慢或卡住）时在名字这一行右边标出多久没更新，块里的数字变淡
  const age = staleAge(r, sample.t);
  const head = h(
    'div',
    { class: 'rt-head' },
    h('span', { class: 'rt-name', text: r.label }),
    gpuTag(r, sample),
    h('span', { class: 'rt-state', 'data-status': status, title: failed && r.error ? r.error : null }, h('span', { class: 'chip-dot' }), label),
    age
      ? h('span', { class: 'rt-stale', title: tr('数据已过期，最后一次更新在 {time}前', { time: elapsed(age) }) }, icon('warning'), tr('{time}未更新', { time: shortAgo(age) }))
      : null,
  );
  const cls = age ? 'rt is-stale' : 'rt';
  // 小窗模式只显示数字，不显示余额和额度
  const quota = state.compact ? [] : [balanceLine(r, sample.t), quotaLine(r, sample.t)];
  if (doneOnly) return h('div', { class: cls }, head, ...doneLines(r, sample.t), ...quota);
  // 连不上、读取出错时块里写原因（太长截断，鼠标停上去看全部）；启动中还没有数据，不写“未加载模型”“无会话记录”这类
  const [body, subText] = failed
    ? [main(null, null, r.error || statusText(r.kind, status), r.error), '']
    : status === 'starting'
      ? [null, '']
      : r.kind === 'ollama'
        ? ollamaBody(r)
        : r.kind === 'comfyui'
          ? comfyBody(r)
          : cli
            ? agentBody(r)
            : [null, ''];
  if (state.compact) return h('div', { class: cls }, head, body, ...doneLines(r, sample.t));
  const res = sample.procs?.owners?.[r.id];
  const resText = !res
    ? null
    : cli
      ? `${tr('内存 {v} GB', { v: gb(res.mem) })} · CPU ${num(res.cpu)}%`
      : [tr('显存 {v} GB', { v: gb(res.vram) }), res.shared > GB / 4 ? tr('共享 {v} GB', { v: gb(res.shared) }) : null, tr('内存 {v} GB', { v: gb(res.mem) })]
          .filter(Boolean)
          .join(' · ');
  return h(
    'div',
    { class: cls },
    head,
    body,
    subText ? h('div', { class: 'rt-sub', text: subText }) : null,
    ...quota,
    ...doneLines(r, sample.t),
    resText ? h('div', { class: 'rt-sub', text: resText }) : null,
  );
}

function updateRuntimes(s) {
  const list = s.runtimes || [];
  if (!list.length) {
    rtSection.replaceChildren(h('div', { class: 'm-empty', text: tr('未检测到模型服务或命令行工具') }));
    return;
  }
  // 活跃的完整显示；空闲但刚完成过任务的只显示完成提示；其余空闲的只在最后一行列出名字。
  // 数据过期的也完整显示：它的“空闲”可能已经不对了，不能只留个名字
  const active = activeRuntimeIds(state.history);
  for (const r of list) if (staleAge(r, s.t)) active.add(r.id);
  const blocks = list.filter((r) => active.has(r.id)).map((r) => rtBlock(r, s));
  const rest = list.filter((r) => !active.has(r.id));
  blocks.push(...rest.filter((r) => recentDone(r).length).map((r) => rtBlock(r, s, { doneOnly: true })));
  const idle = rest.filter((r) => !recentDone(r).length);
  // 窗口窄放不下时在名字之间换行，名字本身不拆开
  if (idle.length) blocks.push(h('div', { class: 'm-idle' }, h('span', { class: 'chip-dot' }), h('span', {}, tr('空闲：'), ...joinParts(idle.map((r) => r.label)))));
  // 空闲的工具没有块，有余额、额度的在空闲那一行下面各占一行
  if (!state.compact) {
    blocks.push(...idle.flatMap((r) => [balanceLine(r, s.t, { named: true }), quotaLine(r, s.t, { named: true })]).filter(Boolean));
  }
  rtSection.replaceChildren(...blocks);
}

// ---------- 刷新、窗口高度、连接 ----------

function fitWindow() {
  const height = Math.ceil(document.body.getBoundingClientRect().height);
  if (Math.abs(height - state.lastHeight) > 1 && api()?.fit) {
    state.lastHeight = height;
    api().fit(height);
  }
}

function render() {
  const last = state.history[state.history.length - 1];
  if (!last) return;
  updateRuntimes(last);
  updateHardware(last);
  fitWindow();
}

// 标题栏显示设置里给这台电脑起的名字（没起名时是 AI InferPeek），桌面程序的托盘提示也跟着换。
// 名字在每条样本里，变了才更新；看不见时也换，托盘提示不等悬浮窗显示
const brandEl = document.getElementById('brand');
function setDeviceName(name) {
  if (name === state.deviceName) return;
  state.deviceName = name;
  brandEl.textContent = name || 'AI InferPeek';
  brandEl.title = name ? `AI InferPeek · ${name}` : '';
  api()?.set_device_name?.(name);
}

// 看不见时只收数据，重新显示时再画、再按内容调整窗口高度
const stream = connectStream({
  onInit(data) {
    setGpuDevices(data.info.gpus?.devices);
    setSampleInterval(data.info.interval);
    buildGpus(data.info.gpus?.devices || []);
    const last = data.history[data.history.length - 1];
    const keep = last ? last.t - (SPARK_SECONDS + 5) * 1000 : 0;
    state.history = data.history.filter((s) => s.t >= keep);
    if (last) setDeviceName(last.device?.name || null);
    followLanguage(last);
  },
  onSample(s) {
    pushSample(state.history, s, (SPARK_SECONDS + 5) * 1000);
    setDeviceName(s.device?.name || null);
    followLanguage(s); // 设置里改了界面语言时重新加载（地址里记着小窗模式，见 setCompact）
  },
  render,
  hidden,
  downText: tr('已断开'),
});

// 桌面程序隐藏、显示悬浮窗时调用（显示前先调，窗口一出来就是最新的）
window.setWindowVisible = (visible) => {
  state.windowHidden = !visible;
  stream.refresh();
};

const hideBtn = document.getElementById('hide');
hideBtn.hidden = true;
window.addEventListener('pywebviewready', () => {
  hideBtn.hidden = false;
  state.lastHeight = 0;
  fitWindow();
  // 桌面程序的接口比数据晚准备好时，之前收到的名字补报一次
  if (state.deviceName !== undefined) api()?.set_device_name?.(state.deviceName);
});
hideBtn.addEventListener('click', () => api()?.hide());

const compactBtn = document.getElementById('compact');

function setCompact(on) {
  state.compact = on;
  document.body.classList.toggle('compact', on);
  hwSection.hidden = on;
  compactSection.hidden = !on;
  compactBtn.setAttribute('aria-pressed', String(on));
  compactBtn.title = on ? tr('切换为完整悬浮窗') : tr('小窗模式：仅显示核心数字');
  // 记在地址上：刷新（比如换了界面语言）后保持；桌面程序换主题时按自己保存的设置重新打开
  const url = new URL(location.href);
  if (on) url.searchParams.set('compact', '1');
  else url.searchParams.delete('compact');
  history.replaceState(null, '', url);
  render();
}
window.setCompact = setCompact; // 托盘菜单切换时由桌面程序调用
setCompact(state.compact);

compactBtn.addEventListener('click', () => {
  setCompact(!state.compact);
  api()?.set_compact?.(state.compact); // 桌面程序保存设置、改窗口宽度
});
// 宽度变了内容会重新换行，马上按新高度调整窗口
window.addEventListener('resize', fitWindow);
document.getElementById('open').addEventListener('click', () => {
  if (api()?.open_dashboard) api().open_dashboard();
  else window.open('/', '_blank');
});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', render);
