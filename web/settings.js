// 设置页面：管理 aiinferpeek.json 里的界面语言、这台电脑的名字、监控哪些工具、各工具的数据目录（Ollama 是日志位置）和手动添加的服务地址。
// 每个工具旁边是后台的检测情况（/api/diagnostics，和看板上“没看到某个工具？”是同一份），每 5 秒刷新。
// 保存时服务端校验、写文件、换上新配置并重新检测一次，返回新的检测情况，不用重启。
// 最后一张卡片说明 CPU 温度要另外运行 LibreHardwareMonitor、怎么设置，并显示现在读没读到（/api/latest，也是每 5 秒刷新）

import { h, store, card, icon, num } from './ui.js';
import { diagLineEl, diagSummaryEl, diagMark, diagTiming, DIAG_STATE, DIAG_SCOPE, DIAG_REFRESH_MS } from './diagnostics.js';
import { tr, lang, locale } from './i18n.js';

const CATEGORY = { service: tr('模型服务'), cli: tr('命令行工具') };
const PATH_LABEL = { ollama_log: tr('日志文件位置') };
// 语言的名字用它自己的文字写，不翻译：看不懂当前语言的人也认得出
const LANGUAGE_NAMES = { 'zh-CN': '简体中文', en: 'English' }; // i18n: 不翻译
const URL_HINT = { ollama: 'http://192.168.1.20:11434', comfyui: 'http://192.168.1.20:8188' };
// 官方仓库最新版的发布页：老版本用的驱动 WinRing0 会被杀毒软件报警，新版换成了 PawnIO
const LHM_DOWNLOAD = 'https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/latest';

const state = {
  data: null, // GET /api/config 的结果：文件路径、版本、能监控的工具……
  saved: null, // 文件里现在的设置（读取或保存后），比较它判断有没有改动
  form: null, // 页面上正在改的设置
  problems: new Map(), // 字段（比如 endpoints[0].url）→ 服务端说的问题
  message: null, // 保存栏里的提示 { text, level, reload }
  diag: null,
  diagError: null,
  latest: undefined, // GET /api/latest 的结果：最新一条样本，还没采样时是 null，还没取过是 undefined
  latestError: null,
  saving: false,
};

const els = {
  main: document.getElementById('settings'),
  bar: document.getElementById('save-bar'),
  save: document.getElementById('save'),
  revert: document.getElementById('revert'),
  msg: document.getElementById('save-msg'),
};

let view = null; // build() 建好的各部分

const normUrl = (url) => (typeof url === 'string' ? url.trim().replace(/\/+$/, '') : '');
const pathKeys = () => state.data.tools.filter((t) => t.key).map((t) => t.key);
// 读得到余额的工具（DeepSeek Harness），可以填余额预警
const balanceKinds = () => state.data.tools.filter((t) => t.balance).map((t) => t.kind);
const toolLabel = (kind) => state.data.tools.find((t) => t.kind === kind)?.label || kind;

// ---------- 设置的数据 ----------

// 文件里的配置 → 页面上改的样子。手动改坏的值也尽量显示出来，保存时由服务端指出
function toForm(config) {
  const endpoints = Array.isArray(config.endpoints) ? config.endpoints : [];
  return {
    // 空的是跟随系统（文件里没写、null 或 auto）
    language: config.language == null || config.language === 'auto' ? '' : String(config.language),
    deviceName: config.device_name == null ? '' : String(config.device_name),
    disabled: Array.isArray(config.disabled_tools) ? [...config.disabled_tools] : [],
    paths: Object.fromEntries(pathKeys().map((k) => [k, typeof config[k] === 'string' ? config[k] : ''])),
    // 余额预警：每个工具一个输入框，空的是不提醒
    balanceWarn: Object.fromEntries(
      balanceKinds().map((k) => {
        const v = config.balance_warning && typeof config.balance_warning === 'object' ? config.balance_warning[k] : null;
        return [k, v == null ? '' : String(v)];
      }),
    ),
    // 每个地址保留文件里别的字段，只改 kind 和 url
    endpoints: endpoints.map((ep) => (ep && typeof ep === 'object' ? { ...ep } : { kind: '', url: String(ep ?? '') })),
  };
}

// 页面上的设置 → 发给服务端的 JSON：名字、路径留空是用默认的（null）
function payload(form) {
  const out = {
    language: form.language || null,
    device_name: form.deviceName.trim() || null,
    endpoints: form.endpoints.map((ep) => ({ ...ep, url: typeof ep.url === 'string' ? ep.url.trim() : ep.url })),
    disabled_tools: state.data.tools.map((t) => t.kind).filter((k) => form.disabled.includes(k)),
  };
  for (const k of pathKeys()) out[k] = form.paths[k].trim() || null;
  // 写的是数字就发数字；写得不对的原样发过去，由服务端指出；都空着是 null（不提醒）
  const warn = {};
  for (const k of balanceKinds()) {
    const v = form.balanceWarn[k].trim();
    if (v) warn[k] = Number.isFinite(Number(v)) ? Number(v) : v;
  }
  out.balance_warning = Object.keys(warn).length ? warn : null;
  return out;
}

const dirty = () => JSON.stringify(payload(state.form)) !== JSON.stringify(payload(state.saved));

// ---------- 读取和保存 ----------

async function load() {
  let data;
  try {
    const r = await fetch('/api/config', { cache: 'no-store' });
    data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  } catch (e) {
    els.main.replaceChildren(h('p', { class: 'empty', text: tr('无法获取设置：{error}', { error: e.message || e }) }));
    return;
  }
  state.data = data;
  state.problems = new Map((data.problems || []).map((p) => [p.field, p.message]));
  state.message = null;
  build(data.config);
  loadDiag();
  loadCpuTemp();
}

async function save() {
  if (state.saving || !dirty()) return;
  state.saving = true;
  state.message = { text: tr('正在保存并检测…'), level: 'info' };
  updateBar();
  // 保存要等新配置生效（一两秒），这期间输入框照样能改：记下发出去的，收到结果后比一比，改过的不能被覆盖
  const sent = JSON.stringify(payload(state.form));
  let r;
  let body;
  try {
    r = await fetch('/api/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ version: state.data.version, config: JSON.parse(sent) }),
    });
    body = await r.json().catch(() => ({}));
  } catch (e) {
    state.saving = false;
    state.message = { text: tr('保存失败：无法连接看板服务（{error}）', { error: e.message || e }), level: 'error' };
    updateBar();
    return;
  }
  state.saving = false;
  if (r.ok) {
    Object.assign(state.data, { version: body.version, exists: true, error: null });
    state.problems.clear();
    if (body.diagnostics) {
      state.diag = body.diagnostics;
      state.diagError = null;
    }
    // 先把“已保存”的基线换成刚存进文件的：下面重新加载时 beforeunload 不会再当成有没保存的修改来拦
    const edited = JSON.stringify(payload(state.form)) !== sent;
    state.saved = toForm(body.config);
    state.data.config = body.config;
    const switching = languageOf(body.config) !== lang;
    const time = new Date().toLocaleTimeString(locale, { hour12: false });
    const text = body.applied ? tr('设置已保存并生效 · {time}', { time }) : tr('设置已保存，正在生效… · {time}', { time });
    if (!edited) {
      state.message = { text, level: 'ok' };
      build(body.config);
    } else {
      // 保存期间改过：页面上改的留着（不重建），提示这部分还没保存
      const notes = [text, tr('保存期间修改的内容尚未保存'), switching ? tr('刷新页面后切换为新的界面语言') : null];
      state.message = { text: notes.filter(Boolean).join(tr('；')), level: 'ok' };
      renderFileCard();
      renderProblems();
      renderDiag();
      updateBar();
    }
    // 换了界面语言：重新加载，整个页面换成新的语言（看板和悬浮窗看到样本里的语言变了也会自己重新加载）。
    // 还有没保存的修改时浏览器会先问要不要离开，所以保存栏要在这之前就换成保存完的样子：
    // 选了留下时页面不会重新加载，也不会再走到别的地方去更新它，保存、撤销按钮得已经能用
    if (switching) location.reload();
    return;
  }
  // 写错的项（400）放到对应输入框旁边；文件被改过（409）可以重新载入；写不进去（500）、被拒绝（403）说明原因
  state.problems = new Map((body.problems || []).map((p) => [p.field, p.message]));
  state.message = { text: body.error || tr('保存失败（HTTP {status}）', { status: r.status }), level: 'error', reload: r.status === 409 };
  renderProblems();
  updateBar();
}

// 配置里的 language 实际用哪种语言：没写、auto（或写错了）时跟随系统
const languageOf = (config) => (config.language in LANGUAGE_NAMES ? config.language : state.data.system_language || 'zh-CN');

function revert() {
  state.problems.clear();
  state.message = null;
  build(state.data.config);
}

// ---------- 检测情况 ----------

async function loadDiag() {
  try {
    const r = await fetch('/api/diagnostics', { cache: 'no-store' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    state.diag = await r.json();
    state.diagError = null;
  } catch (e) {
    state.diagError = e.message || String(e);
  }
  renderDiag();
}

function renderDiag() {
  if (!view) return;
  const savedOff = new Set(state.saved.disabled);
  const formOff = new Set(state.form.disabled);
  for (const [kind, v] of view.tools) {
    const tool = state.diag?.tools?.find((x) => x.kind === kind);
    v.row.classList.toggle('is-off', formOff.has(kind));
    let status = 'pending';
    let text;
    let lines = [];
    if (savedOff.has(kind)) {
      status = 'off';
      text = formOff.has(kind) ? tr('已停用（不检测且不轮询）') : tr('已停用 · 保存后启用监控');
    } else if (!tool) {
      text = state.diagError ? tr('无法获取检测情况（{error}）', { error: state.diagError }) : tr('正在检测…');
    } else {
      status = tool.status;
      text = (DIAG_STATE[tool.status] || tool.status) + (formOff.has(kind) ? ` · ${tr('保存后停用监控')}` : '');
      // 数据目录的情况写在输入框下面，这里不重复；其余的和看板上“没看到某个工具？”的写法一样
      lines = tool.checks.filter((c) => !(c.code === 'missing_path' && v.tool.key && c.key === v.tool.key)).map((c) => diagLineEl(c, tool));
    }
    v.row.dataset.status = status;
    v.stateEl.replaceChildren(diagMark(status), h('span', { text }));
    v.lines.replaceChildren(...lines);
    v.lines.hidden = !lines.length;
    if (v.pathState) renderPathState(v, tool, savedOff.has(kind));
  }
  renderEndpointStates();
  view.diagNote.textContent = [...diagTiming(state.diag), DIAG_SCOPE].join(' · ');
}

// 数据目录输入框下面：改了还没保存、找不到（检测情况里的 missing_path）、找到了
function renderPathState(v, tool, off) {
  const key = v.tool.key;
  const value = state.saved.paths[key].trim();
  let content = [];
  let level = 'info';
  if (state.form.paths[key].trim() !== value) {
    content = [h('span', { text: tr('保存后将检测此路径') })];
  } else if (!off && tool) {
    const miss = tool.checks.find((c) => c.code === 'missing_path' && c.key === key);
    if (miss) {
      level = miss.level;
      content = [icon('warning'), diagSummaryEl(miss, tool)];
    } else if (tool.category === 'cli' || tool.status === 'ok') {
      // 模型服务没在本机运行时后台不看日志，说不上找没找到
      level = 'ok';
      content = [icon('done'), h('span', {}, tr('已定位：'), h('span', { class: 'diag-path', text: value || v.tool.default || '' }))];
    }
  }
  v.pathState.dataset.level = level;
  v.pathState.replaceChildren(...content);
  v.pathState.hidden = !content.length;
}

// 每个手动添加的地址下面：保存后才检测、所属的工具关掉了、连上了、连不上
function renderEndpointStates() {
  const savedOff = new Set(state.saved.disabled);
  const saved = new Set(state.saved.endpoints.map((e) => `${e.kind} ${normUrl(e.url)}`));
  for (const r of view.endpointRows) {
    const { ep } = r;
    let content;
    if (!saved.has(`${ep.kind} ${normUrl(ep.url)}`)) content = [h('span', { text: tr('保存后进行连接检测') })];
    else if (savedOff.has(ep.kind)) content = [h('span', { text: tr('{tool} 已停用，跳过连接此地址', { tool: toolLabel(ep.kind) }) })];
    else {
      const tool = state.diag?.tools?.find((t) => t.kind === ep.kind);
      // 指的是本机发现的服务时（写法可能不同，比如 localhost），后台记在那个服务的 endpoints 里
      const same = (u) => normUrl(u) === normUrl(ep.url);
      const c = tool?.checks.find((x) => x.url && (same(x.url) || (x.endpoints || []).some(same)));
      if (!c) content = [h('span', { text: tr('正在检测…') })];
      else if (c.code === 'found') content = [icon('done'), h('span', { text: c.configured ? tr('已连接') : tr('已连接（与本机自动发现的为同一服务，进程 {pid}）', { pid: c.pid }) })];
      else content = [icon('warning'), diagSummaryEl(c, tool)];
    }
    r.stateEl.replaceChildren(...content);
  }
}

async function loadCpuTemp() {
  try {
    const r = await fetch('/api/latest', { cache: 'no-store' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    state.latest = await r.json();
    state.latestError = null;
  } catch (e) {
    state.latestError = e.message || String(e);
  }
  renderCpuTemp();
}

// 读不到温度的原因和看板 CPU 卡片下面的是同一句
function renderCpuTemp() {
  if (!view) return;
  const cpu = state.latest?.cpu;
  let content;
  if (state.latestError) content = [icon('warning'), h('span', { text: tr('获取当前状态失败（{error}）', { error: state.latestError }) })];
  else if (!state.latest) content = [h('span', { text: tr('正在检测…') })];
  else if (!cpu) content = [icon('warning'), h('span', { text: tr('未采集到本次 CPU 监控数据') })];
  else if (cpu.temp != null) content = [icon('done'), h('span', { text: tr('已连接 LibreHardwareMonitor，CPU 当前温度 {v}°C', { v: num(cpu.temp) }) })];
  else content = [icon('warning'), h('span', { text: cpu.temp_note || tr('暂未获取到 CPU 温度') })];
  view.cpuTempState.replaceChildren(...content);
}

// ---------- 页面 ----------

function notice(text, level) {
  return h('p', { class: 'notice', 'data-level': level }, icon(level === 'error' ? 'critical' : 'warning'), h('span', { text }));
}

function errorEl(field, id) {
  return h('p', { class: 'field-error', 'data-error': field, id, role: 'alert' });
}

// 内容改了：清掉对应字段的错误提示和上次保存的结果
function changed(...fields) {
  for (const f of fields) {
    for (const key of [...state.problems.keys()]) if (key === f || key.startsWith(`${f}[`)) state.problems.delete(key);
  }
  if (!state.saving) state.message = null; // 保存中改的：“正在保存并检测…”留着，结果回来后再说
  renderProblems();
  renderDiag();
  updateBar();
}

function toolRow(t) {
  const id = `tool-${t.kind}`;
  const input = h('input', { type: 'checkbox', role: 'switch', id, 'aria-label': tr('监控 {tool}', { tool: t.label }) });
  input.checked = !state.form.disabled.includes(t.kind);
  input.addEventListener('change', () => {
    const off = new Set(state.form.disabled);
    if (input.checked) off.delete(t.kind);
    else off.add(t.kind);
    state.form.disabled = [...off];
    changed('disabled_tools');
  });
  const v = { tool: t, input, stateEl: h('span', { class: 'tool-state' }), lines: h('ul', { class: 'diag-lines' }) };
  const main = h(
    'div',
    { class: 'tool-main' },
    h(
      'div',
      { class: 'tool-head' },
      h('label', { class: 'tool-name', for: id }, h('strong', { text: t.label })),
      h('span', { class: 'tool-cat', text: CATEGORY[t.category] || t.category }),
      v.stateEl,
    ),
    v.lines,
  );
  if (t.key) {
    const pid = `path-${t.key}`;
    v.pathInput = h('input', {
      type: 'text',
      class: 'text-input',
      id: pid,
      'data-field': t.key,
      placeholder: t.default ? tr('默认：{path}', { path: t.default }) : tr('默认位置'),
      spellcheck: 'false',
      autocomplete: 'off',
      'aria-describedby': `${pid}-error`,
    });
    v.pathInput.value = state.form.paths[t.key];
    v.pathInput.addEventListener('input', () => {
      state.form.paths[t.key] = v.pathInput.value;
      changed(t.key);
    });
    v.pathState = h('p', { class: 'path-state' });
    main.append(
      h(
        'div',
        { class: 'field' },
        h(
          'label',
          { class: 'field-label', for: pid },
          PATH_LABEL[t.key] || tr('数据目录'),
          h('span', { class: 'field-key', text: ` · ${tr('aiinferpeek.json 的 {key}，留空表示使用默认位置', { key: t.key })}` }),
        ),
        v.pathInput,
        errorEl(t.key, `${pid}-error`),
        v.pathState,
      ),
    );
  }
  if (t.balance) main.append(balanceField(t));
  v.row = h('li', { class: 'tool-row' }, h('label', { class: 'switch', title: tr('监控 {tool}', { tool: t.label }) }, input, h('span', { class: 'switch-ui', 'aria-hidden': 'true' })), main);
  view.tools.set(t.kind, v);
  return v.row;
}

// 余额预警：余额低于这个数时看板和悬浮窗标黄提醒。余额是 DeepSeek Harness 的余额小鲸鱼插件查到后记下的，这里只读它的账本
function balanceField(t) {
  const field = `balance_warning.${t.kind}`;
  const id = `balance-${t.kind}`;
  const input = h('input', {
    type: 'text',
    class: 'text-input short',
    id,
    inputmode: 'decimal',
    'data-field': field,
    placeholder: tr('不提醒'),
    spellcheck: 'false',
    autocomplete: 'off',
    'aria-describedby': `${id}-error ${id}-note`,
  });
  input.value = state.form.balanceWarn[t.kind];
  input.addEventListener('input', () => {
    state.form.balanceWarn[t.kind] = input.value;
    changed(field);
  });
  return h(
    'div',
    { class: 'field' },
    h(
      'label',
      { class: 'field-label', for: id },
      tr('余额预警'),
      h('span', { class: 'field-key', text: ` · ${tr('aiinferpeek.json 的 {key}，余额低于此阈值时提醒，留空表示不提醒', { key: field })}` }),
    ),
    input,
    errorEl(field, `${id}-error`),
    h('p', {
      class: 'path-state',
      id: `${id}-note`,
      text: tr(
        '预警币种与账户一致（如人民币单位为“元”）。余额数据读取自 DeepSeek Harness 的“余额小鲸鱼”插件（dsh-whale-widget）生成的本地账本，未安装该插件时无法展示余额。',
      ),
    }),
  );
}

function endpointRow(ep, i) {
  const f = `endpoints[${i}]`;
  const kinds = state.data.service_kinds;
  const select = h('select', { class: 'select-input', 'data-field': `${f}.kind`, 'aria-label': tr('第 {n} 个地址的服务类型', { n: i + 1 }) });
  if (!kinds.includes(ep.kind)) select.append(h('option', { value: ep.kind ?? '', text: ep.kind ? tr('{kind}（无法识别）', { kind: ep.kind }) : tr('选择类型') }));
  select.append(...kinds.map((k) => h('option', { value: k, text: toolLabel(k) })));
  select.value = ep.kind ?? '';
  const url = h('input', {
    type: 'text',
    class: 'text-input',
    inputmode: 'url',
    spellcheck: 'false',
    autocomplete: 'off',
    'data-field': `${f}.url`,
    placeholder: URL_HINT[ep.kind] || 'http://',
    'aria-label': tr('第 {n} 个地址', { n: i + 1 }),
    'aria-describedby': `${f}-url-error`,
  });
  url.value = typeof ep.url === 'string' ? ep.url : String(ep.url ?? '');
  select.addEventListener('change', () => {
    ep.kind = select.value;
    url.placeholder = URL_HINT[ep.kind] || 'http://';
    changed(`${f}.kind`);
  });
  url.addEventListener('input', () => {
    ep.url = url.value;
    changed(`${f}.url`);
  });
  const del = h('button', { class: 'btn', type: 'button', 'aria-label': tr('删除第 {n} 个地址', { n: i + 1 }) }, tr('删除'));
  del.addEventListener('click', () => {
    state.form.endpoints.splice(i, 1);
    // 后面的地址序号变了，旧的错误提示对不上，一起清掉
    for (const key of [...state.problems.keys()]) if (key.startsWith('endpoints[')) state.problems.delete(key);
    renderEndpoints();
    changed();
  });
  const stateEl = h('p', { class: 'endpoint-state' });
  const el = h(
    'div',
    { class: 'endpoint-row' },
    h('div', { class: 'endpoint-fields' }, select, url, del),
    errorEl(f),
    errorEl(`${f}.kind`),
    errorEl(`${f}.url`, `${f}-url-error`),
    stateEl,
  );
  return { el, ep, stateEl };
}

function renderEndpoints() {
  view.endpointRows = state.form.endpoints.map(endpointRow);
  view.endpointList.replaceChildren(
    ...(view.endpointRows.length
      ? view.endpointRows.map((r) => r.el)
      : [h('p', { class: 'footnote', text: tr('暂无手动添加的服务地址。') })]),
  );
  if (state.data.readonly) for (const x of view.endpointList.querySelectorAll('input, select, button')) x.disabled = true;
}

// 界面语言：看板、悬浮窗、设置页面和托盘菜单一起换。标题一直带着英文，看不懂中文的人也找得到
function languageCard() {
  const c = card(tr('界面语言 / Language'), tr('看板、悬浮窗、设置页面和托盘菜单使用的语言'));
  const select = h('select', { class: 'select-input', id: 'language', 'data-field': 'language', 'aria-describedby': 'language-error' });
  const system = LANGUAGE_NAMES[state.data.system_language] || state.data.system_language || '';
  select.append(
    h('option', { value: '', text: tr('跟随系统（{name}）', { name: system }) }),
    ...Object.entries(LANGUAGE_NAMES).map(([value, text]) => h('option', { value, text })),
  );
  // 文件里手动写了不认识的值：先照样显示出来，保存时由服务端指出
  if (state.form.language && !(state.form.language in LANGUAGE_NAMES)) select.append(h('option', { value: state.form.language, text: state.form.language }));
  select.value = state.form.language;
  select.addEventListener('change', () => {
    state.form.language = select.value;
    changed('language');
  });
  c.body.append(
    h(
      'div',
      { class: 'field device-field' },
      h('label', { class: 'field-label', for: 'language' }, tr('语言'), h('span', { class: 'field-key', text: ` · ${tr('aiinferpeek.json 的 language，保存后页面将重新加载')}` })),
      select,
      errorEl('language', 'language-error'),
    ),
  );
  return c;
}

// 这台电脑的名字：看板标题、浏览器标签、悬浮窗标题、托盘提示用它，留空用计算机名
function deviceCard() {
  const c = card(tr('设备名称'), tr('自定义当前设备的显示名称，将同步展示在看板标题、浏览器标签页、悬浮窗及系统托盘中'));
  const input = h('input', {
    type: 'text',
    class: 'text-input',
    id: 'device-name',
    'data-field': 'device_name',
    placeholder: tr('计算机名：{name}', { name: state.data.host || '' }),
    spellcheck: 'false',
    autocomplete: 'off',
    'aria-describedby': 'device-name-error',
  });
  input.value = state.form.deviceName;
  input.addEventListener('input', () => {
    state.form.deviceName = input.value;
    changed('device_name');
  });
  c.body.append(
    h(
      'div',
      { class: 'field device-field' },
      h(
        'label',
        { class: 'field-label', for: 'device-name' },
        tr('设备显示名称'),
        h('span', { class: 'field-key', text: ` · ${tr('aiinferpeek.json 的 device_name，最多 {n} 个字，留空表示使用计算机名', { n: state.data.device_name_max || 32 })}` }),
      ),
      input,
      errorEl('device_name', 'device-name-error'),
    ),
  );
  return c;
}

// CPU 温度：不是配置文件里的设置，只说明怎么开 LibreHardwareMonitor。
// 下载按钮是新窗口打开的链接：桌面版的窗口会把它交给系统默认浏览器，不在程序里打开
function cpuTempCard() {
  const c = card(tr('CPU 温度'), tr('Windows 系统未开放普通应用程序的 CPU 核心温度读取接口，需配合运行 LibreHardwareMonitor（免费开源硬件监控工具）获取实时读数'));
  view.cpuTempState = h('p', { class: 'path-state cpu-temp-state' });
  const download = h('a', { class: 'btn', href: LHM_DOWNLOAD, target: '_blank', rel: 'noopener noreferrer', title: LHM_DOWNLOAD }, tr('下载 LibreHardwareMonitor'));
  c.body.append(
    view.cpuTempState,
    h(
      'ol',
      { class: 'setup-steps' },
      h('li', {}, h('span', { text: tr('前往其 GitHub 官方 Releases 页面下载最新版本并解压。') }), download),
      h('li', { text: tr('右键点击 LibreHardwareMonitor.exe，选择“以管理员身份运行”（加载硬件传感器驱动需要管理员权限）。') }),
      h('li', { text: tr('在菜单栏勾选 Options → Remote Web Server → Run（保持默认端口 8085，且不勾选 Authentication 密码认证）。') }),
      h('li', {
        text: tr('安全建议：在 Options → Remote Web Server → Interface / Port 中将监听网卡选为 127.0.0.1（仅允许本机回环访问，防止局域网内其他设备读取本机硬件读数）。'),
      }),
    ),
    h('p', {
      class: 'footnote',
      text: tr('服务启动数秒内看板与悬浮窗将自动识别并显示 CPU 温度，无需重启本软件。如需开机常驻，请在其 Options 中勾选 Run On Windows Startup 与 Start Minimized。'),
    }),
  );
  return c;
}

function renderFileCard() {
  const d = state.data;
  const parts = [
    h(
      'p',
      { class: 'file-path' },
      h('span', { class: 'diag-path', text: d.path }),
      d.exists ? null : h('span', { class: 'diag-detail', text: ` · ${tr('文件尚不存在，保存时创建')}` }),
    ),
    d.readonly ? notice(d.readonly, 'warning') : null,
    d.error ? notice(d.error, 'error') : null,
    d.extra?.length ? h('p', { class: 'footnote', text: tr('文件中还有以下不由设置页面管理的项，保存时原样保留：{keys}', { keys: d.extra.join(tr('、')) }) }) : null,
    h('p', { class: 'footnote', text: tr('配置保存后即刻生效，无需重启软件。') }),
  ];
  view.fileCard.body.replaceChildren(...parts.filter(Boolean));
}

// 错误提示放到对应输入框旁边；没有对应输入框的（比如 disabled_tools 第 N 项）放到所在卡片，再没有就放进保存栏
function renderProblems() {
  const placed = new Set();
  for (const el of els.main.querySelectorAll('[data-error]')) {
    el.textContent = state.problems.get(el.dataset.error) || '';
    if (el.textContent) placed.add(el.dataset.error);
  }
  for (const input of els.main.querySelectorAll('[data-field]')) input.setAttribute('aria-invalid', String(state.problems.has(input.dataset.field)));
  view.rest = [];
  for (const [field, message] of state.problems) {
    if (placed.has(field)) continue;
    const el = els.main.querySelector(`[data-error="${field.split(/[[.]/)[0]}"]`);
    if (field && el) el.textContent += (el.textContent ? tr('；') : '') + message;
    else view.rest.push(message);
  }
}

function updateBar() {
  const d = state.data;
  const isDirty = dirty();
  els.save.disabled = Boolean(d.readonly || d.error) || state.saving || !isDirty;
  els.revert.disabled = Boolean(d.readonly) || state.saving || !isDirty;
  reportUnsaved(isDirty);
  const msg = state.message || (view.rest?.length ? { text: view.rest.join(tr('；')), level: 'error' } : isDirty ? { text: tr('有未保存的修改'), level: 'info' } : null);
  els.msg.dataset.level = msg?.level || '';
  els.msg.replaceChildren();
  if (!msg) return;
  if (msg.level !== 'info') els.msg.append(icon(msg.level === 'ok' ? 'done' : 'critical'));
  els.msg.append(h('span', { text: msg.text }));
  if (msg.reload) {
    const btn = h('button', { class: 'btn', type: 'button' }, tr('重新载入（放弃当前修改）'));
    btn.addEventListener('click', load);
    els.msg.append(btn);
  }
}

// 在桌面程序的看板窗口里打开时，告诉它有没有没保存的修改：关窗口、退出前它会先问一下。
// 浏览器里关标签页靠下面的 beforeunload，窗口直接关掉时页面不会收到
let reportedUnsaved = false;
function reportUnsaved(unsaved) {
  const api = window.pywebview?.api;
  if (unsaved === reportedUnsaved || !api?.set_unsaved) return;
  reportedUnsaved = unsaved;
  api.set_unsaved(unsaved);
}

function build(config) {
  state.saved = toForm(config);
  state.form = toForm(config);
  state.data.config = config;
  view = { tools: new Map(), endpointRows: [], rest: [] };

  view.fileCard = card(tr('配置文件'), '');
  renderFileCard();

  const tools = card(tr('监控的工具'), tr('停用的工具将停止发现与轮询，且不在看板中显示'));
  view.diagNote = h('p', { class: 'footnote' });
  tools.body.append(errorEl('disabled_tools'), h('ul', { class: 'tool-list' }, ...state.data.tools.map(toolRow)), view.diagNote);

  const eps = card(tr('手动添加的服务地址'), tr('本机运行的服务将自动发现；此处可添加局域网远程服务或无法自动探测的地址'));
  view.endpointList = h('div', { class: 'endpoint-list' });
  const add = h('button', { class: 'btn', type: 'button' }, tr('添加地址'));
  add.addEventListener('click', () => {
    state.form.endpoints.push({ kind: state.data.service_kinds[0], url: '' });
    renderEndpoints();
    changed();
    view.endpointRows.at(-1)?.el.querySelector('input')?.focus();
  });
  eps.body.append(
    errorEl('endpoints'),
    view.endpointList,
    h('div', { class: 'endpoint-actions' }, add),
    h('p', { class: 'footnote', text: tr('仅可获取 API 接口数据，无法采集远程设备的系统资源。所属工具停用时不会发起连接。') }),
  );

  els.main.replaceChildren(view.fileCard.el, languageCard().el, deviceCard().el, tools.el, eps.el, cpuTempCard().el);
  renderEndpoints();
  if (state.data.readonly) for (const x of els.main.querySelectorAll('input, select, button')) x.disabled = true;
  els.bar.hidden = false;
  renderProblems();
  renderDiag();
  renderCpuTemp();
  updateBar();
}

function applyTheme() {
  // 和看板用同一个主题设置（看板右上角的按钮）
  const mode = store.get('aiinferpeek-theme');
  if (mode === 'light' || mode === 'dark') document.documentElement.dataset.theme = mode;
}

applyTheme();
els.save.addEventListener('click', save);
els.revert.addEventListener('click', revert);
window.addEventListener('beforeunload', (e) => {
  if (state.form && dirty()) e.preventDefault();
});
// 桌面程序的接口比页面晚准备好，之前改的要补报一次
window.addEventListener('pywebviewready', () => state.form && reportUnsaved(dirty()));
setInterval(() => {
  if (view && !document.hidden && !state.saving) loadDiag();
  if (view && !document.hidden) loadCpuTemp();
}, DIAG_REFRESH_MS);
document.addEventListener('visibilitychange', () => {
  if (view && !document.hidden) {
    loadDiag();
    loadCpuTemp();
  }
});
load();
