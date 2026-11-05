import { niceScale, capacityScale, showTooltip, hideTooltip } from './chart.js';
import { GB, h, store, num, gb, bytes, ratioOf, PERCENT_SCALE, tile, chartFigure, dataTable, card, sectionTitle, pushSample } from './ui.js';
import { runtimeView, modelsView, activeRuntimeIds, activeFirst, recentDone, setGpuDevices, mainGpus, gpuText } from './runtimes.js';
import { noRuntimesView } from './diagnostics.js';
import { connectStream } from './stream.js';

const state = {
  info: null,
  history: [], // 精简样本，只有最新一条是完整的
  rangeSec: 60,
  views: [],
  hardware: null, // build() 用到的硬件信息，见 hardwareKey()
  runtimes: new Map(),
  active: new Set(),
  deviceName: null, // 设置里给这台电脑起的名字
};

const gapMs = () => (state.info?.interval ?? 1) * 3000;
const TEMP_SCALE = { top: 100, ticks: [0, 25, 50, 75, 100], tickFormat: (v) => `${v}°` };
// 没有上限的功耗按图上的最大值定刻度
const powerScale = (pts) => niceScale(Math.max(10, ...pts.map((p) => p.v ?? 0)), (v) => `${v} W`);

// ---------- 硬件面板 ----------

// multi：有几块主显卡（比如两块独显），按进程的表要标出每个进程在哪块上
function gpuView(dev, i, { multi = false } = {}) {
  const memTotal = dev.mem_total;
  // 核显没有自己的显存，用的是借来的内存（专用 + 共享，任务管理器里叫“GPU 内存”）
  const memLabel = dev.integrated ? '共享显存（借用内存）' : '显存';
  const c = card(dev.integrated ? '核显' : '显卡', [dev.name, dev.driver ? `驱动 ${dev.driver}` : null].filter(Boolean).join(' · '), { wide: true });
  const t = {
    util: tile('利用率'),
    mem: tile(memLabel, { meter: true }),
    temp: tile('温度'),
    power: tile('功耗'),
    clock: tile('核心频率'),
    memClock: tile('显存频率'),
    fan: tile('风扇'),
  };
  t.fan.hide(true);
  t.memClock.hide(!!dev.integrated); // 核显用的是内存，没有显存频率
  c.body.append(h('div', { class: 'tiles' }, ...Object.values(t).map((x) => x.el)));

  const g = (s) => s.gpus?.[i];
  const charts = [
    chartFigure({ title: '利用率', get: (s) => g(s)?.util, scale: PERCENT_SCALE, format: (v) => `${num(v)}%`, gapMs: gapMs() }),
    chartFigure({
      title: `${memLabel}占用`,
      get: (s) => (g(s)?.mem_used != null ? g(s).mem_used / GB : null),
      // 读不到显存总量时按图上的最大值定刻度
      scale: memTotal
        ? capacityScale(memTotal / GB, (v) => `${v} GB`)
        : (pts) => capacityScale(Math.max(1, ...pts.map((p) => p.v ?? 0)), (v) => `${v} GB`),
      format: (v) => `${num(v, 1)} GB`,
      gapMs: gapMs(),
    }),
    chartFigure({
      title: '功耗',
      get: (s) => g(s)?.power,
      scale: dev.power_limit ? niceScale(dev.power_limit, (v) => `${v} W`) : powerScale,
      format: (v) => `${num(v, 1)} W`,
      gapMs: gapMs(),
    }),
    chartFigure({
      title: '温度',
      get: (s) => g(s)?.temp,
      scale: TEMP_SCALE,
      format: (v) => `${num(v)}°C`,
      gapMs: gapMs(),
    }),
  ];
  c.body.append(h('div', { class: 'charts four' }, ...charts.map((x) => x.el)));

  // 按进程的显存占用：统计的是主显卡（有独显时是独显，否则是核显），显示在排第一的显卡卡片里
  const procTable = dataTable(
    [
      { label: '进程' },
      { label: '属于' },
      ...(multi ? [{ label: '显卡' }] : []),
      { label: '显存', cls: 'num-col' },
      { label: '共享显存', cls: 'num-col' },
      { label: 'GPU', cls: 'num-col' },
      { label: '内存', cls: 'num-col' },
      { label: 'CPU', cls: 'num-col' },
    ],
    { empty: '没有读到按进程的显存数据' },
  );
  const procSection = h(
    'div',
    { class: 'proc-section' },
    sectionTitle(multi ? '显存占用（按进程，所有独显合计）' : '显存占用（按进程）'),
    procTable.el,
    h('p', {
      class: 'footnote',
      text: dev.integrated
        ? '核显没有自己的显存，用的是内存，算在共享显存里。'
        : '共享显存是显存不够时借用的内存，读写比显存慢得多；模型的一部分落在这里时，速度会明显下降。',
    }),
  );
  if (i === 0) c.body.append(procSection);

  return {
    el: c.el,
    charts,
    update(s) {
      const d = g(s);
      if (!d) return;
      t.util.set(num(d.util), '%');
      t.mem.set(gb(d.mem_used), ` / ${gb(d.mem_total)} GB`);
      const memRatio = ratioOf(d.mem_used, d.mem_total);
      t.mem.level(memRatio, memRatio != null ? `空闲 ${gb(d.mem_total - d.mem_used)} GB` : '');
      t.temp.set(num(d.temp), '°C');
      t.power.set(num(d.power, 1), dev.power_limit ? ` / ${num(dev.power_limit)} W` : ' W');
      t.clock.set(num(d.clock_gr), ' MHz', [dev.clock_max ? `最高 ${num(dev.clock_max)} MHz` : null, d.pstate].filter(Boolean).join(' · '));
      t.memClock.set(num(d.clock_mem), ' MHz');
      // NVML 给的是百分比，通用方式（AMD 等）给的是转速
      t.fan.hide(d.fan == null && d.fan_rpm == null);
      if (d.fan != null) t.fan.set(num(d.fan), '%');
      else if (d.fan_rpm != null) t.fan.set(num(d.fan_rpm), ' RPM');

      if (i !== 0) return;
      procSection.hidden = !s.procs;
      if (!s.procs) return;
      procTable.set(
        s.procs.rows.map((p) => [
          [h('strong', { text: p.name }), h('span', { class: 'muted', text: ` ${p.pid}` })],
          p.owner_label || '—',
          ...(multi ? [h('span', { text: gpuText(p.devices) || '—', title: gpuText(p.devices, { full: true }) || '' })] : []),
          bytes(p.vram),
          p.shared ? bytes(p.shared) : '—',
          `${num(p.gpu)}%`,
          bytes(p.mem),
          `${num(p.cpu)}%`,
        ]),
      );
    },
  };
}

function noGpuView(message) {
  const c = card('显卡', '', { wide: true });
  c.body.append(h('p', { class: 'empty', text: message || '没有检测到显卡。' }));
  return { el: c.el, charts: [], update() {} };
}

function cpuView(info) {
  const c = card('CPU', [info.name, `${info.cores} 核 ${info.threads} 线程`].join(' · '));
  const t = { pct: tile('占用'), freq: tile('速度'), temp: tile('温度'), power: tile('功耗') };
  c.body.append(h('div', { class: 'tiles' }, ...Object.values(t).map((x) => x.el)));

  const usage = chartFigure({ title: '占用', get: (s) => s.cpu?.percent, scale: PERCENT_SCALE, format: (v) => `${num(v)}%`, gapMs: gapMs() });
  const temp = chartFigure({ title: '温度', get: (s) => s.cpu?.temp, scale: TEMP_SCALE, format: (v) => `${num(v)}°C`, gapMs: gapMs() });
  const power = chartFigure({ title: '功耗', get: (s) => s.cpu?.power, scale: powerScale, format: (v) => `${num(v, 1)} W`, gapMs: gapMs() });
  // 读不到 CPU 温度的原因（多半是没开 LibreHardwareMonitor），读到了不显示
  const tempNote = h('p', { class: 'footnote', hidden: true });

  // 每个核心一根竖条，悬停或聚焦时显示数值
  const bars = [];
  const coresEl = h('div', { class: 'cores', role: 'list' });
  const coreTbody = h('tbody');
  const coreTable = h(
    'table',
    { class: 'data-table', hidden: true },
    h('thead', {}, h('tr', {}, h('th', { text: '核心' }), h('th', { text: '占用' }))),
    coreTbody,
  );
  const coreBtn = h('button', { class: 'table-toggle', type: 'button', 'aria-pressed': 'false', text: '表格' });
  coreBtn.addEventListener('click', () => {
    const show = coreTable.hidden;
    coreTable.hidden = !show;
    coreBtn.setAttribute('aria-pressed', String(show));
  });
  let perCore = [];
  const coreTip = (i, target) => {
    const r = target.getBoundingClientRect();
    showTooltip(r.left + r.width / 2, r.top, `${num(perCore[i])}%`, `核心 ${i}`);
  };
  function ensureBars(n) {
    while (bars.length < n) {
      const i = bars.length;
      const fill = h('div', { class: 'core-fill' });
      const track = h('div', { class: 'core-track' }, fill);
      const item = h(
        'div',
        {
          class: 'core',
          role: 'listitem',
          tabindex: '0',
          onpointerenter: () => coreTip(i, track),
          onpointerleave: hideTooltip,
          onfocus: () => coreTip(i, track),
          onblur: hideTooltip,
        },
        track,
        h('span', { class: 'core-label', text: String(i) }),
      );
      bars.push({ item, fill });
      coresEl.append(item);
    }
  }
  const coresFig = h(
    'figure',
    { class: 'chart' },
    h('figcaption', {}, h('span', { class: 'chart-title', text: '各核心占用' }), coreBtn),
    coresEl,
    coreTable,
  );
  c.body.append(h('div', { class: 'charts' }, usage.el, coresFig, temp.el, power.el), tempNote);

  return {
    el: c.el,
    charts: [usage, temp, power],
    update(s) {
      const d = s.cpu;
      if (!d) return;
      t.pct.set(num(d.percent), '%');
      t.freq.set(d.freq_mhz != null ? num(d.freq_mhz / 1000, 2) : '—', ' GHz');
      t.temp.set(num(d.temp), '°C');
      t.power.set(num(d.power, 1), ' W');
      tempNote.hidden = !d.temp_note;
      tempNote.textContent = d.temp_note || '';
      perCore = d.per_core || [];
      ensureBars(perCore.length);
      perCore.forEach((v, i) => {
        bars[i].fill.style.height = `${Math.min(v, 100)}%`;
        bars[i].item.setAttribute('aria-label', `核心 ${i}：${num(v)}%`);
      });
      if (!coreTable.hidden) {
        coreTbody.replaceChildren(...perCore.map((v, i) => h('tr', {}, h('td', { text: `核心 ${i}` }), h('td', { text: `${num(v)}%` }))));
      }
    },
  };
}

function memView(info) {
  const c = card('内存', `${gb(info.total)} GB`);
  const t = { used: tile('已用', { meter: true }), avail: tile('可用'), swap: tile('页面文件') };
  c.body.append(h('div', { class: 'tiles' }, t.used.el, t.avail.el, t.swap.el));
  const usage = chartFigure({
    title: '已用',
    get: (s) => (s.mem ? s.mem.used / GB : null),
    scale: capacityScale(info.total / GB, (v) => `${v} GB`),
    format: (v) => `${num(v, 1)} GB`,
    gapMs: gapMs(),
  });
  c.body.append(h('div', { class: 'charts' }, usage.el));
  return {
    el: c.el,
    charts: [usage],
    update(s) {
      const d = s.mem;
      if (!d) return;
      t.used.set(gb(d.used), ` / ${gb(d.total)} GB`);
      t.used.level(ratioOf(d.used, d.total), d.percent != null ? `${num(d.percent)}%` : '');
      t.avail.set(gb(d.available), ' GB');
      t.swap.set(gb(d.swap_used), ` / ${gb(d.swap_total, 0)} GB`);
    },
  };
}

// ---------- 页面组装与刷新 ----------

const runtimesEl = h('div', { class: 'runtimes' });
const models = modelsView();

const destroyCharts = (v) => v.charts.forEach((c) => c.destroy());

// build() 按显卡、CPU、内存的信息搭卡片。重连（后端重启）后它们变了才重建：显卡的型号、顺序、容量变了，数量相同也要重建
const hardwareKey = (info) => JSON.stringify([info.gpus, info.cpu, info.mem]);

function build() {
  const { info } = state;
  const main = document.getElementById('board');
  const devices = info.gpus?.devices ?? [];
  // 重建前旧卡片的图表先释放
  for (const v of [...state.views, ...state.runtimes.values()]) destroyCharts(v);
  state.views = [];
  const multi = mainGpus(devices).length > 1;
  if (devices.length) devices.forEach((d, i) => state.views.push(gpuView(d, i, { multi })));
  else state.views.push(noGpuView(info.gpus?.error));
  if (info.cpu) state.views.push(cpuView(info.cpu));
  if (info.mem) state.views.push(memView(info.mem));
  state.runtimes.clear();
  runtimesEl.replaceChildren();
  main.replaceChildren(models.el, runtimesEl, ...state.views.map((v) => v.el));
  state.hardware = hardwareKey(info);
  renderTitle();
}

// 型号里的 (R)、(TM) 去掉，顶栏放得下
const shortModel = (name) => name?.replace(/\((R|TM)\)/gi, '').replace(/\s+/g, ' ').trim();

// 顶栏：“AI InferPeek / 名字”，名字是设置里给这台电脑起的，没起时用计算机名；下面一行是硬件
function renderTitle() {
  const { info } = state;
  if (!info) return;
  const host = info.cpu?.host;
  const name = state.deviceName;
  document.getElementById('device-name').textContent = name || host || '';
  const gpus = (info.gpus?.devices ?? []).map((d) => d.name);
  const mem = info.mem ? `${gb(info.mem.total)} GB 内存` : null;
  const machine = document.getElementById('machine');
  machine.textContent = [name ? host : null, shortModel(info.cpu?.name), mem, ...gpus].filter(Boolean).join(' · ');
  machine.title = [host, info.cpu?.name, mem, ...gpus].filter(Boolean).join('\n');
  document.title = name ? `${name} · AI InferPeek` : 'AI InferPeek';
}

// 名字在每条样本里（设置页面保存后不用刷新），变了才重画顶栏
function updateDeviceName(s) {
  const name = s.device?.name || null;
  if (name === state.deviceName) return;
  state.deviceName = name;
  renderTitle();
}

// 部署系统会随时出现或消失，按 id 增删卡片，已有的卡片保留（图表状态不丢）。
// 活跃的排在前面并展开，空闲的折叠；手动折叠 / 展开过的卡片保持手动的样子，直到它变成活跃或变回空闲
function syncRuntimes(list) {
  const ids = new Set(list.map((r) => r.id));
  for (const [id, v] of state.runtimes) {
    if (ids.has(id)) continue;
    destroyCharts(v);
    state.runtimes.delete(id);
  }
  for (const r of list) {
    if (state.runtimes.has(r.id)) continue;
    const v = runtimeView(r, gapMs());
    v.manual = null;
    v.card.onToggle = (folded) => {
      v.manual = { folded, active: state.active.has(r.id) };
      render();
    };
    state.runtimes.set(r.id, v);
  }
  for (const r of list) {
    const v = state.runtimes.get(r.id);
    const active = state.active.has(r.id);
    if (v.manual && v.manual.active !== active) v.manual = null;
    const folded = v.manual ? v.manual.folded : !active;
    if (folded !== v.card.folded) v.card.fold(folded);
  }
  // 刚完成过任务的（标题下有“xxx 已完成”）排在活跃的后面、其余空闲的前面
  const recent = new Set(list.filter((r) => recentDone(r).length).map((r) => r.id));
  const els = activeFirst(list, state.active, recent).map((r) => state.runtimes.get(r.id).el);
  if (!els.length) els.push(noRuntimesView(state.info.runtimes?.supported));
  const current = [...runtimesEl.children];
  if (current.length !== els.length || current.some((el, i) => el !== els[i])) runtimesEl.replaceChildren(...els);
}

function render() {
  const last = state.history[state.history.length - 1];
  if (!last) return;
  const t1 = last.t;
  const t0 = t1 - state.rangeSec * 1000;
  const list = last.runtimes || [];
  state.active = activeRuntimeIds(state.history);
  syncRuntimes(list);
  // 一个服务或工具都没有时，下面的“模型服务”卡片已经说明了
  models.el.hidden = !list.length;
  if (list.length) models.update(last);
  for (const r of list) {
    const v = state.runtimes.get(r.id);
    v.update(r, last);
    if (!v.card.folded) for (const c of v.charts) c.update(state.history, t0, t1);
  }
  for (const v of state.views) {
    v.update(last);
    for (const c of v.charts) c.update(state.history, t0, t1);
  }
}

function redrawCharts() {
  for (const v of [...state.views, ...state.runtimes.values()]) for (const c of v.charts) c.redraw();
}

// 页面在后台（切到别的标签页、窗口最小化）时只收数据，切回来再画
function connect() {
  connectStream({
    onInit(data) {
      state.info = data.info;
      state.history = data.history;
      setGpuDevices(data.info.gpus?.devices);
      if (hardwareKey(data.info) !== state.hardware) build();
      const last = data.history[data.history.length - 1];
      if (last) updateDeviceName(last);
    },
    onSample(s) {
      pushSample(state.history, s, (state.info?.history_seconds ?? 600) * 1000);
      updateDeviceName(s); // 在后台时也换，浏览器标签上的标题跟着变
    },
    render,
    downText: '连接断开，正在重连…',
  });
}

// 时间范围
function initRange() {
  const group = document.getElementById('range');
  const saved = Number(store.get('aiinferpeek-range'));
  if ([60, 300, 600].includes(saved)) state.rangeSec = saved;
  const sync = () => {
    for (const b of group.querySelectorAll('button')) b.setAttribute('aria-checked', String(Number(b.dataset.sec) === state.rangeSec));
  };
  group.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    state.rangeSec = Number(b.dataset.sec);
    store.set('aiinferpeek-range', String(state.rangeSec));
    sync();
    render();
  });
  sync();
}

// 主题：跟随系统 / 浅色 / 深色
function initTheme() {
  const modes = [
    ['system', '跟随系统'],
    ['light', '浅色'],
    ['dark', '深色'],
  ];
  const btn = document.getElementById('theme');
  let mode = store.get('aiinferpeek-theme') || 'system';
  const apply = () => {
    if (mode === 'system') delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = mode;
    btn.textContent = `主题：${modes.find((m) => m[0] === mode)?.[1] ?? '跟随系统'}`;
    redrawCharts();
  };
  btn.addEventListener('click', () => {
    const i = modes.findIndex((m) => m[0] === mode);
    mode = modes[(i + 1) % modes.length][0];
    store.set('aiinferpeek-theme', mode);
    apply();
  });
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', redrawCharts);
  apply();
}

initTheme();
initRange();
connect();
