import { niceScale, capacityScale, showTooltip, hideTooltip } from './chart.js';
import { GB, h, store, num, gb, bytes, ratioOf, PERCENT_SCALE, vramCell, tile, chartFigure, dataTable, card, sectionTitle, pushSample } from './ui.js';
import { runtimeView, modelsView, activeRuntimeIds, activeFirst, recentDone, setGpuDevices, setSampleInterval, mainGpus, gpuText } from './runtimes.js';
import { noRuntimesView } from './diagnostics.js';
import { connectStream } from './stream.js';
import { tr, followLanguage } from './i18n.js';

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

// multi：有几块主显卡（比如两块独显），按进程的表要标出每个进程在哪块上。
// foldable：有独显时的核显卡片可以折叠，默认折起来（平时不看它），展开过就记住
const IGPU_FOLD_KEY = 'aiinferpeek-fold-igpu';

function gpuView(dev, i, { multi = false, foldable = false } = {}) {
  const memTotal = dev.mem_total;
  // 核显没有自己的显存，用的是借来的内存（专用 + 共享，任务管理器里叫“GPU 内存”）
  const memLabel = dev.integrated ? tr('共享显存（借用内存）') : tr('显存');
  const c = card(dev.integrated ? tr('核显') : tr('显卡'), [dev.name, dev.driver ? tr('驱动 {v}', { v: dev.driver }) : null].filter(Boolean).join(' · '), { wide: true, foldable });
  if (foldable) {
    c.onToggle = (folded) => {
      c.fold(folded);
      store.set(IGPU_FOLD_KEY, folded ? '1' : '0');
      render();
    };
    c.fold(store.get(IGPU_FOLD_KEY) !== '0');
  }
  const t = {
    util: tile(tr('利用率')),
    mem: tile(memLabel, { meter: true }),
    temp: tile(tr('温度')),
    power: tile(tr('功耗')),
    clock: tile(tr('核心频率')),
    memClock: tile(tr('显存频率')),
    fan: tile(tr('风扇')),
  };
  t.fan.hide(true);
  t.memClock.hide(!!dev.integrated); // 核显用的是内存，没有显存频率
  c.body.append(h('div', { class: 'tiles' }, ...Object.values(t).map((x) => x.el)));

  const g = (s) => s.gpus?.[i];
  // 一直读不到的项（核显的功耗、温度，没有计数器时的显存）整张图收起来，剩下的图按张数平分一行（见 render() 里的 data-count）
  const charts = [
    chartFigure({ title: tr('利用率'), get: (s) => g(s)?.util, scale: PERCENT_SCALE, format: (v) => `${num(v)}%`, gapMs: gapMs(), hideWhenEmpty: true }),
    chartFigure({
      title: tr('{name}占用', { name: memLabel }),
      get: (s) => (g(s)?.mem_used != null ? g(s).mem_used / GB : null),
      // 读不到显存总量时按图上的最大值定刻度
      scale: memTotal
        ? capacityScale(memTotal / GB, (v) => `${v} GB`)
        : (pts) => capacityScale(Math.max(1, ...pts.map((p) => p.v ?? 0)), (v) => `${v} GB`),
      format: (v) => `${num(v, 1)} GB`,
      gapMs: gapMs(),
      hideWhenEmpty: true,
    }),
    chartFigure({
      title: tr('功耗'),
      get: (s) => g(s)?.power,
      scale: dev.power_limit ? niceScale(dev.power_limit, (v) => `${v} W`) : powerScale,
      format: (v) => `${num(v, 1)} W`,
      gapMs: gapMs(),
      hideWhenEmpty: true,
    }),
    chartFigure({
      title: tr('温度'),
      get: (s) => g(s)?.temp,
      scale: TEMP_SCALE,
      format: (v) => `${num(v)}°C`,
      gapMs: gapMs(),
      hideWhenEmpty: true,
    }),
  ];
  const chartsEl = h('div', { class: 'charts four' }, ...charts.map((x) => x.el));
  c.body.append(chartsEl);

  // 按进程的显存占用：统计的是主显卡（有独显时是独显，否则是核显），显示在排第一的显卡卡片里
  const procTable = dataTable(
    [
      { label: tr('进程') },
      { label: tr('属于') },
      ...(multi ? [{ label: tr('显卡') }] : []),
      { label: tr('显存'), cls: 'num-col' },
      { label: tr('共享显存'), cls: 'num-col' },
      { label: 'GPU', cls: 'num-col' },
      { label: tr('内存'), cls: 'num-col' },
      { label: 'CPU', cls: 'num-col' },
    ],
    { empty: tr('未获取到按进程的显存数据') },
  );
  const procSection = h(
    'div',
    { class: 'proc-section' },
    sectionTitle(multi ? tr('显存占用（按进程，所有独显合计）') : tr('显存占用（按进程）')),
    procTable.el,
    h('p', {
      class: 'footnote',
      text: dev.integrated
        ? tr('核显无独立显存，占用的是内存，计入共享显存。')
        : tr('共享显存是显存不足时借用的内存，读写速度远低于显存；模型有一部分位于共享显存时，速度会明显下降。'),
    }),
  );
  if (i === 0) c.body.append(procSection);

  return {
    el: c.el,
    card: c,
    charts,
    chartsEl,
    update(s) {
      // 这一次没采到（采集出错）时按全都读不到显示“—”，不停在上一次的数字上
      const d = g(s) || {};
      t.util.set(num(d.util), '%');
      t.mem.set(gb(d.mem_used), ` / ${gb(d.mem_total)} GB`);
      const memRatio = ratioOf(d.mem_used, d.mem_total);
      t.mem.level(memRatio, memRatio != null ? tr('空闲 {v} GB', { v: gb(d.mem_total - d.mem_used) }) : '');
      t.temp.set(num(d.temp), '°C');
      t.power.set(num(d.power, 1), dev.power_limit ? ` / ${num(dev.power_limit)} W` : ' W');
      t.clock.set(num(d.clock_gr), ' MHz', [dev.clock_max ? tr('最高 {v} MHz', { v: num(dev.clock_max) }) : null, d.pstate].filter(Boolean).join(' · '));
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
          vramCell(p),
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
  const c = card(tr('显卡'), '', { wide: true });
  c.body.append(h('p', { class: 'empty', text: message || tr('未检测到显卡') }));
  return { el: c.el, charts: [], update() {} };
}

function cpuView(info) {
  const c = card('CPU', [info.name, tr('{cores} 核 {threads} 线程', { cores: info.cores, threads: info.threads })].join(' · '));
  const t = { pct: tile(tr('占用')), freq: tile(tr('速度')), temp: tile(tr('温度')), power: tile(tr('功耗')) };
  c.body.append(h('div', { class: 'tiles' }, ...Object.values(t).map((x) => x.el)));

  const usage = chartFigure({ title: tr('占用'), get: (s) => s.cpu?.percent, scale: PERCENT_SCALE, format: (v) => `${num(v)}%`, gapMs: gapMs() });
  // 温度、功耗一直读不到（没开 LibreHardwareMonitor、没有能量计）时整张图收起来，只留下面那句原因
  const temp = chartFigure({ title: tr('温度'), get: (s) => s.cpu?.temp, scale: TEMP_SCALE, format: (v) => `${num(v)}°C`, gapMs: gapMs(), hideWhenEmpty: true });
  const power = chartFigure({ title: tr('功耗'), get: (s) => s.cpu?.power, scale: powerScale, format: (v) => `${num(v, 1)} W`, gapMs: gapMs(), hideWhenEmpty: true });
  // 读不到 CPU 温度的原因（多半是没开 LibreHardwareMonitor），读到了不显示
  const tempNote = h('p', { class: 'footnote', hidden: true });

  // 每个核心一根竖条，悬停或聚焦时显示数值
  const bars = [];
  const coresEl = h('div', { class: 'cores', role: 'list' });
  const coreTbody = h('tbody');
  const coreTable = h(
    'table',
    { class: 'data-table', hidden: true },
    h('thead', {}, h('tr', {}, h('th', { text: tr('核心') }), h('th', { text: tr('占用') }))),
    coreTbody,
  );
  const coreBtn = h('button', { class: 'table-toggle', type: 'button', 'aria-pressed': 'false', text: tr('表格') });
  coreBtn.addEventListener('click', () => {
    const show = coreTable.hidden;
    coreTable.hidden = !show;
    coreBtn.setAttribute('aria-pressed', String(show));
  });
  let perCore = [];
  const coreTip = (i, target) => {
    const r = target.getBoundingClientRect();
    showTooltip(r.left + r.width / 2, r.top, `${num(perCore[i])}%`, tr('核心 {i}', { i }));
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
    h('figcaption', {}, h('span', { class: 'chart-title', text: tr('各核心占用') }), coreBtn),
    coresEl,
    coreTable,
  );
  c.body.append(h('div', { class: 'charts' }, usage.el, coresFig, temp.el, power.el), tempNote);

  return {
    el: c.el,
    charts: [usage, temp, power],
    update(s) {
      const d = s.cpu || {}; // 没采到时显示“—”，见 gpuView
      t.pct.set(num(d.percent), '%');
      t.freq.set(d.freq_mhz != null ? num(d.freq_mhz / 1000, 2) : '—', ' GHz');
      t.temp.set(num(d.temp), '°C');
      t.power.set(num(d.power, 1), ' W');
      tempNote.hidden = !d.temp_note;
      tempNote.textContent = d.temp_note || '';
      perCore = d.per_core || [];
      ensureBars(perCore.length);
      // 已有的竖条都更新：这一次没采到时清空，不停在上一次的高度
      bars.forEach((b, i) => {
        b.fill.style.height = `${Math.min(perCore[i] ?? 0, 100)}%`;
        b.item.setAttribute('aria-label', tr('核心 {i}：{v}%', { i, v: num(perCore[i]) }));
      });
      if (!coreTable.hidden) {
        coreTbody.replaceChildren(...perCore.map((v, i) => h('tr', {}, h('td', { text: tr('核心 {i}', { i }) }), h('td', { text: `${num(v)}%` }))));
      }
    },
  };
}

function memView(info) {
  const c = card(tr('内存'), `${gb(info.total)} GB`);
  const t = { used: tile(tr('已用'), { meter: true }), avail: tile(tr('可用')), swap: tile(tr('页面文件')) };
  c.body.append(h('div', { class: 'tiles' }, t.used.el, t.avail.el, t.swap.el));
  const usage = chartFigure({
    title: tr('已用'),
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
      const d = s.mem || {}; // 没采到时显示“—”，见 gpuView
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
  // 有独显时核显卡片可以折叠（默认折起来），只有核显时它就是主显卡，照常展开
  if (devices.length) devices.forEach((d, i) => state.views.push(gpuView(d, i, { multi, foldable: devices.length > 1 && !!d.integrated })));
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
  const mem = info.mem ? tr('{v} GB 内存', { v: gb(info.mem.total) }) : null;
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
    if (v.card?.folded) continue;
    for (const c of v.charts) c.update(state.history, t0, t1);
    // 收起了几张图后，剩下的按张数平分一行（见 style.css 的 .charts.four[data-count]）
    if (v.chartsEl) v.chartsEl.dataset.count = String(v.charts.filter((c) => !c.el.hidden).length);
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
      setSampleInterval(data.info.interval);
      if (hardwareKey(data.info) !== state.hardware) build();
      const last = data.history[data.history.length - 1];
      if (last) updateDeviceName(last);
      followLanguage(last);
    },
    onSample(s) {
      pushSample(state.history, s, (state.info?.history_seconds ?? 600) * 1000);
      updateDeviceName(s); // 在后台时也换，浏览器标签上的标题跟着变
      followLanguage(s); // 设置里改了界面语言时重新加载
    },
    render,
    downText: tr('连接断开，正在重连…'),
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
    ['system', tr('跟随系统')],
    ['light', tr('浅色')],
    ['dark', tr('深色')],
  ];
  const btn = document.getElementById('theme');
  let mode = store.get('aiinferpeek-theme') || 'system';
  const apply = () => {
    if (mode === 'system') delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = mode;
    btn.textContent = tr('主题：{name}', { name: modes.find((m) => m[0] === mode)?.[1] ?? tr('跟随系统') });
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

// 版本号和检查更新：版本号一直显示；查到过比现在新的版本时旁边是“新版本 x.y.z”，点开它的发布页面（桌面版的窗口交给浏览器打开）。
// 桌面版的托盘自动检查查到的这里也有，切回这个页面时再取一次。开放到局域网时没有检查按钮（不让别的电脑叫它联网）
function initUpdate() {
  const versionEl = document.getElementById('app-version');
  const btn = document.getElementById('check-update');
  const badge = document.getElementById('new-version');
  const msg = document.getElementById('update-msg');
  let timer = 0;
  const show = (d) => {
    versionEl.textContent = `v${d.version}`;
    versionEl.title = tr('当前版本 {v}', { v: d.version });
    btn.hidden = !d.can_check;
    badge.hidden = !d.latest;
    if (d.latest) {
      badge.textContent = tr('新版本 {v}', { v: d.latest.version });
      badge.href = d.latest.url;
      badge.title = tr('打开 {v} 的下载页面', { v: d.latest.version });
    }
  };
  // 检查的结果：ms 毫秒后自己消失，0 是一直留着
  const note = (text, level, ms = 0) => {
    clearTimeout(timer);
    msg.textContent = text;
    msg.title = text;
    msg.dataset.level = level;
    if (ms) timer = setTimeout(() => note('', ''), ms);
  };
  const load = () =>
    fetch('/api/update', { cache: 'no-store' })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => d && show(d))
      .catch(() => {});
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    btn.classList.add('is-checking');
    note(tr('正在检查更新…'), 'info');
    try {
      const r = await fetch('/api/update/check', { method: 'POST' });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
      show(d);
      if (d.error) note(tr('检查更新失败：{error}', { error: d.error }), 'error', 15000);
      else if (d.latest) note('', ''); // 旁边的“新版本 x.y.z”已经说明了
      else note(tr('已是最新版本'), 'ok', 6000);
    } catch (e) {
      note(tr('检查更新失败：{error}', { error: e.message || e }), 'error', 15000);
    } finally {
      btn.disabled = false;
      btn.classList.remove('is-checking');
    }
  });
  document.addEventListener('visibilitychange', () => !document.hidden && load());
  load();
}

// 时间范围那一条固定在顶部：滚到下面的显卡、CPU 图表时也能切换。贴住时下面画一条线，和内容分开
function initSticky() {
  const bar = document.querySelector('.filters');
  const sentinel = h('div', { class: 'sticky-sentinel' });
  bar.before(sentinel);
  new IntersectionObserver(([e]) => bar.classList.toggle('is-stuck', !e.isIntersecting)).observe(sentinel);
}

initTheme();
initRange();
initSticky();
initUpdate();
connect();
