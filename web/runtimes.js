// 模型服务面板：每个检测到的部署系统、命令行工具一张卡片。

import { niceScale } from './chart.js';
import { h, store, num, gb, bytes, duration, elapsed, tokens, when, until, rate, ratioOf, icon, chevron, tile, chartFigure, dataTable, card, sectionTitle } from './ui.js';
import { diagToggle } from './diagnostics.js';

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

// 后台给每个实例带着最后一次成功轮询的时间（polled）。和样本时间差超过 10 秒（轮询很慢或卡住了）
// 才算过期，返回已经多久没更新；正常时返回 0。连不上、读取出错的已经有提示，不再重复
const STALE_MS = 10000;

export function staleAge(rt, now) {
  if (rt.polled == null || rt.status === 'offline' || rt.status === 'error') return 0;
  const age = now - rt.polled;
  return age > STALE_MS ? age : 0;
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
// 后台保留最近 5 分钟结束的任务（rt.done）。点“知道了”后记下这一刻之前的都看过了，
// 存在浏览器里，看板和悬浮窗是同一个地址，关掉一边另一边也不再显示

const DONE_SEEN_KEY = 'aiinferpeek-done-seen';

// 任务怎么结束的（后台给的 result）
const RESULTS = {
  ok: { text: '已完成', icon: 'done' },
  failed: { text: '失败', icon: 'critical' },
  interrupted: { text: '已中断', icon: 'warning' },
  lost: { text: '可能已中断', icon: 'warning' },
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
  const ago = now - e.t < 60000 ? '刚刚' : `${Math.floor((now - e.t) / 60000)} 分钟前`;
  const count = e.count > 1 ? `（5 分钟内 ${e.count} 次）` : '';
  const parts = [`${e.name} ${resultOf(e).text}${count}`, ago];
  if (!short && e.duration) parts.push(`用时 ${duration(e.duration)}`);
  if (!short && e.detail) parts.push(e.detail);
  return parts.join(' · ');
}

// 卡片标题下的提示：每条一行，右边一个“知道了”
function doneList() {
  const items = h('div', { class: 'done-items' });
  const ack = h('button', { class: 'more-btn ack-btn', type: 'button', text: '已知晓' });
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

const BUSY_TEXT = { ollama: '生成中', comfyui: '执行中' };
const STATUS_TEXT = { loading: '加载模型中', idle: '空闲', starting: '启动中', offline: '无法连接', error: '读取出错' };

// 服务或工具的状态文字：工作中的 Ollama 是“生成中”、ComfyUI 是“执行中”，其余见 STATUS_TEXT。卡片右上角和悬浮窗共用
export const statusText = (kind, status) => (status === 'busy' ? BUSY_TEXT[kind] || '运行中' : STATUS_TEXT[status] || status || '');

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
  const all = { vram: tile('显存'), shared: tile('共享显存'), mem: tile('内存'), cpu: tile('CPU') };
  const t = Object.fromEntries(keys.map((k) => [k, all[k]]));
  return {
    tiles: Object.values(t).map((x) => x.el),
    update(total) {
      t.vram?.set(total ? gb(total.vram) : '—', ' GB', total?.gpu ? `GPU 利用率 ${num(total.gpu)}%` : '');
      t.shared?.set(total ? gb(total.shared) : '—', ' GB', total?.shared > 512 * 1024 ** 2 ? '借用的内存，比显存慢' : '');
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
  const res = resourceTiles(resKeys);
  return {
    c,
    res,
    head(rt, sample, chipText) {
      const parts = [rt.version ? `版本 ${rt.version}` : null, rt.url ? rt.url.replace(/^https?:\/\//, '') : rt.home];
      if (rt.pytorch) parts.push(`PyTorch ${rt.pytorch}`);
      if (!rt.local) parts.push('远程（看不到进程资源）');
      // 模型服务用的显卡；命令行工具的模型在云端，它的进程用显卡只是画界面，不标
      const gpus = rt.category === 'cli' ? null : usedGpus(sample.procs, rt);
      if (gpuText(gpus)) parts.push(`显卡 ${gpuText(gpus)}`);
      c.subEl.textContent = parts.filter(Boolean).join(' · ');
      c.subEl.title = gpuText(gpus, { full: true }) || '';
      chip.set(rt.kind, rt.status, chipText);
      const age = staleAge(rt, sample.t);
      stale.hidden = !age;
      if (age) stale.replaceChildren(icon('warning'), `数据已过期 · ${elapsed(age)}前`);
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

// 灰色的“ · RTX 5070 Ti Laptop”，鼠标停上去看完整名字
function gpuNote(indices) {
  const text = gpuText(indices);
  return text ? h('span', { class: 'muted', text: ` · ${text}`, title: gpuText(indices, { full: true }) }) : null;
}

function modelState(m) {
  if (m.state === 'generating') {
    const parts = [m.speed != null ? `${num(m.speed, 1)} tokens/s` : null, m.n_decoded != null ? `已生成 ${num(m.n_decoded)}` : null];
    return h('span', { class: 'state busy' }, h('strong', { text: '生成中' }), parts.filter(Boolean).length ? ` · ${parts.filter(Boolean).join(' · ')}` : '');
  }
  if (m.state === 'prompt') return h('span', { class: 'state busy' }, h('strong', { text: '处理提示词' }));
  if (m.state === 'loading') return h('span', { class: 'state' }, h('strong', { text: '加载中' }));
  return h('span', { class: 'state muted', text: '空闲' });
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

function ollamaView(r, gapMs) {
  const s = shell(r, 'Ollama');
  const t = { now: tile('实时生成速度'), gen: tile('上次生成速度'), prompt: tile('上次提示词处理') };
  s.c.body.append(h('div', { class: 'tiles' }, t.now.el, t.gen.el, t.prompt.el, ...s.res.tiles));

  const models = dataTable(
    [
      { label: '模型' },
      { label: '规模' },
      { label: '大小', cls: 'num-col' },
      { label: '进程显存', cls: 'num-col' },
      { label: '显卡 / CPU' },
      { label: '上下文', cls: 'num-col' },
      { label: '状态' },
      { label: '自动卸载' },
    ],
    { empty: '没有加载模型。发起一次对话后，模型会出现在这里。' },
  );
  s.c.body.append(sectionTitle('已加载的模型'), models.el);

  const speed = speedChart(r.id, { title: '生成速度（tokens/s）', floor: 10, tickFormat: (v) => `${v}`, gapMs });
  const requests = dataTable(
    [{ label: '完成时间' }, { label: '模型' }, { label: '提示词', cls: 'num-col' }, { label: '生成', cls: 'num-col' }, { label: '总耗时', cls: 'num-col' }],
    { empty: '还没有请求记录' },
  );
  const logNote = h('p', { class: 'footnote' });
  s.c.body.append(
    h('div', { class: 'split-layout' }, speed.el, h('div', {}, sectionTitle('最近的请求'), requests.el, logNote)),
  );

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
      const busy = ms.find((m) => m.state === 'generating' || m.state === 'prompt');
      if (rt.speed != null) t.now.set(num(rt.speed, 1), ' tokens/s', busy ? busy.name : '');
      else t.now.set('—', '', busy?.state === 'prompt' ? '正在处理提示词' : rt.status === 'busy' ? '正在计算…' : '空闲');

      const reqs = rt.requests || [];
      const last = reqs[0];
      t.gen.set(last ? num(rate(last.gen_tokens, last.gen_ms), 1) : '—', last ? ' tokens/s' : '', last ? `${num(last.gen_tokens)} tokens · ${when(last.t)}` : '');
      t.prompt.set(
        last ? num(rate(last.prompt_tokens, last.prompt_ms), 0) : '—',
        last ? ' tokens/s' : '',
        last ? `${num(last.prompt_tokens)} tokens · ${duration(last.prompt_ms)}` : '',
      );

      const procs = new Map((sample.procs?.rows || []).map((p) => [p.pid, p]));
      models.set(
        ms.map((m) => [
          h('strong', { text: m.name }),
          [m.params, m.quant].filter(Boolean).join(' · ') || '—',
          m.size ? bytes(m.size) : '—',
          m.pid && procs.get(m.pid) ? bytes(procs.get(m.pid).vram) : '—',
          [splitBar(m.gpu_ratio), m.gpu_ratio > 0 ? gpuNote(usedGpus(sample.procs, rt, m.pid)) : null],
          m.context ? num(m.context) : '—',
          modelState(m),
          m.state === 'loading' ? '—' : until(m.expires),
        ]),
      );

      requests.set(
        reqs
          .slice(0, 8)
          .map((q) => [when(q.t), q.model || '—', tokRate(q.prompt_tokens, q.prompt_ms), tokRate(q.gen_tokens, q.gen_ms), duration(q.total_ms)]),
      );
      logNote.textContent = rt.log
        ? '每次请求的数据来自 Ollama 日志，只读取计时信息。'
        : '读不到 Ollama 日志（远程服务或日志位置不同），无法统计每次请求。';
    },
  };
}

function comfyView(r) {
  const s = shell(r, 'ComfyUI');
  const t = { current: tile('当前任务'), queue: tile('排队'), last: tile('上次耗时'), avg: tile('最近 5 次平均') };
  s.c.body.append(h('div', { class: 'tiles' }, t.current.el, t.queue.el, t.last.el, t.avg.el, ...s.res.tiles));

  const jobs = dataTable(
    [{ label: '完成时间' }, { label: '任务' }, { label: '输出' }, { label: '耗时', cls: 'num-col' }, { label: '结果' }],
    { empty: '还没有完成的任务' },
  );
  s.c.body.append(
    sectionTitle('最近的任务'),
    jobs.el,
    h('p', { class: 'footnote', text: '第几步、每秒几步只会发给提交任务的那个 ComfyUI 网页，外部程序读不到，所以这里只显示每个任务的总耗时。' }),
  );

  const output = (j) => {
    const parts = [];
    if (j.outputs != null) parts.push(`${j.outputs} 个`);
    const kind = { image: '图片', video: '视频', audio: '音频', images: '图片' }[j.media] || j.media;
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
        t.current.set(sec == null ? '执行中' : sec < 60 ? `${sec} 秒` : duration(sec * 1000), '', `已运行 · 任务 ${String(cur.id || '').slice(0, 8)}`);
      }
      else t.current.set('空闲', '');
      const q = rt.queue || {};
      t.queue.set(num(q.pending ?? 0), ' 个', '等待执行');
      t.last.set(rt.last_duration != null ? num(rt.last_duration / 1000, 1) : '—', rt.last_duration != null ? ' 秒' : '');
      t.avg.set(rt.avg_duration != null ? num(rt.avg_duration / 1000, 1) : '—', rt.avg_duration != null ? ' 秒' : '');
      jobs.set(
        (rt.jobs || []).map((j) => [
          when(j.end ?? j.created),
          h('span', { class: 'mono', text: String(j.id || '').slice(0, 8) }),
          output(j),
          duration(j.duration),
          j.status === 'completed' ? '完成' : j.status === 'interrupted' ? [icon('warning'), ' 已中断'] : [icon('critical'), ' 失败'],
        ]),
      );
    },
  };
}

// ---------- 命令行工具（Claude Code、Codex、DeepSeek Harness、Grok Build、OpenCode、OpenClaw、Antigravity） ----------

const ENTRY_TEXT = {
  cli: '终端',
  'claude-desktop': '桌面版',
  'claude-vscode': 'VS Code',
  'sdk-ts': 'SDK',
  'sdk-py': 'SDK',
  'Codex Desktop': '桌面版',
  codex_cli_rs: '终端',
  codex_vscode: 'VS Code',
  codex_exec: 'exec',
  chatgpt: '本机执行',
};
const WAITING_TEXT = {
  'dialog open': '确认对话框',
  'input needed': '需要输入',
  'sandbox request': '沙箱权限',
  'goal proposal': '确认目标',
  question: '回答问题',
  permission: '确认权限',
  review: '审阅',
};
const AGENT_NOTE = {
  'claude-code':
    '数据来自 Claude Code 的会话登记和会话记录，只读取标题、目录、状态、模型和 token 用量，不读取对话内容。上下文是最近一次请求发给模型的 tokens。' +
    '额度（Beta）读的是 Claude 桌面版记下的套餐用量，只用和 Claude Code 登录的是同一个组织的记录，并不准确：桌面版只在启动时记一次，' +
    '24 小时内点开过托盘里的用量面板时才每 15 分钟记一次，所以常常是几小时前的数，也不知道什么时候重置。',
  codex:
    '桌面版的一个 codex 进程服务所有会话，所以列出最近有活动的会话。额度是最近一次请求时服务器返回的数值。只读取会话记录里的状态和用量，不读取对话内容。' +
    'ChatGPT 里选在本机执行的任务，对话和模型在 ChatGPT 服务器上，本机只有替它执行命令的 codex 进程：它为每一轮开几个工具进程、做完就关，' +
    '所以只看得到对话什么时候开始、这一轮是不是还在进行，标题、目录、模型、用量和这一轮怎么结束的都看不到，也不提示完成。',
  dsh:
    '数据来自 DeepSeek Harness 自己算好的会话汇总（~/.dsh/storages），只读取标题、目录、状态、模型和 token 用量。子任务不单独列出。' +
    '余额读的是余额小鲸鱼插件（dsh-whale-widget）记下的账本（~/.dsh/.dshw-usage.json），不读 API key、不联网；没装这个插件时没有余额。' +
    '“今天已用”按北京时间分天，和插件一样。余额预警在设置页面的 DeepSeek Harness 那一栏里改。',
  'grok-build': '数据来自 Grok Build 的会话目录，只读取标题、模型、回合和权限事件、上下文和 token 用量，不读取对话内容。token 用量在每个回合结束时更新。',
  opencode:
    '数据来自 opencode 的会话数据库（opencode.db），只读取标题、目录、模型、回合什么时候开始和结束、token 用量，不读取对话内容。' +
    '工具调用等你确认、等你回答问题时库里没有记录，显示的是工作中。子会话不单独列出。',
  openclaw:
    '数据来自 OpenClaw 每个 agent 的会话数据库，只读取标题、聊天渠道、模型、消息的角色和结束原因、上下文和 token 用量，不读取对话内容。' +
    '工具调用等你确认时库里没有记录，显示的是工作中。子会话不单独列出。',
  antigravity:
    '数据来自 Antigravity 的对话库（~/.gemini/antigravity），只读取标题、目录、运行状态、每一步的状态和时间、模型名、上下文和 token 用量，不读取对话内容。' +
    'token 用量在模型每回复完一次后更新。子 agent 的对话不单独列出。' +
    '额度（Beta）每分钟从它的后台程序 language_server 读一次：用的是没公开的本机接口（带上它启动参数里的令牌，只连本机），升级后可能读不到；' +
    '同一组的几个模型共用一份额度。',
};

// 列的是正在运行的会话，还是最近 N 小时有活动的会话（对不上进程和会话的工具）
export function recentText(rt) {
  return rt.recent ? `最近 ${Math.round(rt.recent / 3600)} 小时` : '';
}

// 会话数量的概括，例如“2 个工作中 · 1 个等你操作”
export function sessionSummary(sessions) {
  const busy = sessions.filter((s) => s.status === 'busy').length;
  const waiting = sessions.filter((s) => s.status === 'waiting').length;
  const parts = [busy ? `${busy} 个工作中` : null, waiting ? `${waiting} 个等你操作` : null].filter(Boolean);
  return parts.length ? parts.join(' · ') : sessions.length ? '全部空闲' : '没有会话';
}

// 额度窗口的名字：300 分钟 → 5 小时额度，10080 分钟 → 每周额度
export function limitName(minutes) {
  if (minutes === 10080) return '每周额度';
  if (minutes && minutes % 1440 === 0) return `${minutes / 1440} 天额度`;
  if (minutes && minutes % 60 === 0) return `${minutes / 60} 小时额度`;
  return minutes ? `${minutes} 分钟额度` : '额度';
}

// 还有效的额度窗口（5 小时、每周）：Codex 的过了重置时间就不算了，要等下次请求才有新数
export function liveLimits(rt, now) {
  const lim = rt.limits;
  return [lim?.primary, lim?.secondary].filter((w) => w && !(w.resets && w.resets < now));
}

// 还是新功能（Beta）的额度：Claude Code 的是 Claude 桌面版记下的套餐用量，并不准确；
// Antigravity 的是从它后台程序没公开的本机接口读的，升级后可能读不到
const BETA_TEXT = {
  'claude-desktop':
    'Beta：读取自 Claude 桌面版记录的套餐用量数据，仅供参考。桌面版仅在启动时记录一次，24 小时内打开过托盘用量面板时每 15 分钟更新一次，' +
    '可能存在数小时延迟且无重置时间说明；记录超过 5 小时将不再显示 5 小时额度。',
  antigravity:
    'Beta：每分钟从 Antigravity 后台程序（language_server）读取一次（调用其本机接口），若工具升级后接口变更可能无法获取。' +
    '额度按模型分组共享。',
};
export const isBetaLimits = (lim) => !!BETA_TEXT[lim?.source];
export const betaText = (lim) => BETA_TEXT[lim?.source] || '';

export function betaTag(lim) {
  return h('span', { class: 'beta-tag', text: 'Beta', title: betaText(lim) });
}

// 按模型分组的额度（Antigravity）：还没过重置时间的组。名字短写是第一个模型加“等”，完整的是全部模型
export function livePools(rt, now) {
  return (rt.limits?.pools || []).filter((p) => !(p.resets && p.resets < now));
}
export const poolName = (p, { full = false } = {}) => (full || p.names.length < 2 ? p.names.join('、') : `${p.names[0]} 等`);

// 余额（按量付费的工具，现在只有 DeepSeek Harness）：金额两位小数，币种写成中文单位；低于设置里的预警线时提醒
const CURRENCY = { CNY: '元', USD: '美元' };
export const currencyUnit = (c) => CURRENCY[c] || c || '';
export const money = (n) => num(n, 2);
export const balanceLow = (b) => !!b && b.warn != null && b.amount < b.warn;
export const BALANCE_NOTE = '余额数据由 DeepSeek Harness 的“余额小鲸鱼”插件（dsh-whale-widget）生成并记录在本地账本中（DeepSeek Harness 界面打开时约每 25 秒自动查询一次）。预警阈值可在设置页面调整。';

// 桌面版的额度记录是多久以前的：刚刚、12 分钟前、3 小时前、2 天前
export function recordAge(ms) {
  if (ms < 60000) return '刚刚';
  if (ms < 3600000) return `${Math.floor(ms / 60000)} 分钟前`;
  if (ms < 86400000) return `${Math.floor(ms / 3600000)} 小时前`;
  return `${Math.floor(ms / 86400000)} 天前`;
}

function resetText(t) {
  const s = (t - Date.now()) / 1000;
  if (s < 3600) return `${Math.max(1, Math.ceil(s / 60))} 分钟后重置`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时 ${Math.floor((s % 3600) / 60)} 分后重置`;
  return `${when(t)} 重置`;
}

function sessionState(s, now) {
  if (s.status === 'waiting') {
    const why = WAITING_TEXT[s.waiting_for] || s.waiting_for;
    return h(
      'span',
      { class: 'state waiting' },
      icon('warning'),
      h('strong', { text: ' 等你操作' }),
      why ? h('span', { class: 'muted', text: `（${why}）` }) : null,
    );
  }
  if (s.status === 'busy') return h('span', { class: 'state busy' }, h('strong', { text: '工作中' }), s.since ? ` · ${elapsed(now - s.since)}` : '');
  return h('span', { class: 'state muted', text: '空闲' });
}

// remote 的会话（ChatGPT 里选在本机执行的任务）对话在服务器上，读不到的字段写“看不到”，不是“—”
const unseen = () => h('span', { class: 'muted', title: '对话和模型在 ChatGPT 服务器上，本机读不到', text: '看不到' });

function agentView(r, gapMs) {
  const s = shell(r, r.label, ['mem', 'cpu']);
  const t = { sessions: tile('会话'), speed: tile('输出速度'), out: tile('这些会话共输出') };
  // 额度（5 小时、每周）：Codex 和 Claude Code 有，其余工具一直隐藏。按模型分组的额度（Antigravity）每组一个磁贴，用到时再建
  const limits = [tile('5 小时额度', { meter: true }), tile('每周额度', { meter: true })];
  limits.forEach((x) => x.hide(true));
  const poolTiles = [];
  // 余额：按量付费、读得到余额的工具（DeepSeek Harness）有，其余一直隐藏
  const balance = tile('余额');
  balance.hide(true);
  balance.el.title = BALANCE_NOTE;
  s.c.body.append(h('div', { class: 'tiles' }, t.sessions.el, balance.el, ...limits.map((x) => x.el), t.speed.el, t.out.el, ...s.res.tiles));

  const table = dataTable(
    [
      { label: '会话' },
      { label: '目录' },
      { label: '模型' },
      { label: '状态' },
      { label: '上下文', cls: 'num-col' },
      { label: '输入', cls: 'num-col' },
      { label: '输出', cls: 'num-col' },
      { label: '最后活动' },
    ],
    { empty: r.recent ? `${recentText(r)}没有会话活动` : '没有正在运行的会话' },
  );
  const speed = speedChart(r.id, { title: '输出速度（tokens/s）', floor: 20, tickFormat: (v) => tokens(v), gapMs });
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
    sectionTitle(r.recent ? `${recentText(r)}有活动的会话` : '正在运行的会话'),
    table.el,
    more,
    h('div', { class: 'charts agent-chart' }, speed.el),
    h('p', { class: 'footnote', text: AGENT_NOTE[r.kind] || '' }),
  );

  function update(rt, sample) {
    last = [rt, sample];
    const list = rt.sessions || [];
    const summary = sessionSummary(list);
    s.head(rt, sample, rt.status === 'idle' ? '空闲' : summary);
    t.sessions.set(num(list.length), ' 个', summary);
    t.speed.set(num(rt.speed ?? 0, 1), ' tokens/s', '最近 1 分钟平均');
    const sum = (k) => list.reduce((a, x) => a + (x.tokens?.[k] ?? 0), 0);
    const input = sum('input');
    t.out.set(tokens(sum('output')), ' tokens', input ? `输入 ${tokens(input)}，其中缓存 ${num((sum('cached') / input) * 100)}%` : '');

    const b = rt.balance;
    balance.hide(!b);
    if (b) {
      // 低于预警线时和占用接近满载一样标黄、带提醒图标；下面一行是今天（北京时间，和插件记账的分天一样）花掉的钱和查询时间
      const low = balanceLow(b);
      const unit = currencyUnit(b.currency);
      balance.el.classList.toggle('is-warning', low);
      const info = [b.today != null ? `今天已用 ${money(b.today)} ${unit}` : null, `${recordAge(sample.t - b.t)}查询`].filter(Boolean).join(' · ');
      // 磁贴窄，提醒的文字放在一个 span 里，换行时整段在图标右边折行
      balance.set(money(b.amount), ` ${unit}`, low ? [icon('warning'), h('span', { text: `低于预警 ${money(b.warn)} ${unit} · ${info}` })] : info);
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
        x.level(0, '已重置，将在下次请求后同步');
      } else {
        x.set(num(w.used), '%');
        // 桌面版的记录没有重置时间，写是什么时候记的
        x.level(ratioOf(w.used, 100), w.resets ? resetText(w.resets) : beta ? `${recordAge(sample.t - lim.t)}记录` : '');
        x.el.title = beta ? `桌面版记录时间：${when(lim.t)}` : '';
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
      x.el.querySelector('.tile-label').replaceChildren(`${poolName(p)} 额度`, betaTag(lim));
      x.el.title = `共用这份额度的模型：\n${p.models.join('\n')}`;
      if (p.resets && p.resets < sample.t) {
        x.set('—', '');
        x.level(0, '已重置，将在下次读取后同步');
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
    moreLabel.textContent = showIdle ? '收起空闲会话' : `显示另外 ${idle} 个空闲会话`;

    table.set(
      (folding && !showIdle ? active : list).map((x) => {
        const ctx = x.context == null ? '—' : x.context_window ? `${tokens(x.context)} / ${tokens(x.context_window)}` : tokens(x.context);
        const pct = x.context != null && x.context_window ? h('span', { class: 'muted', text: ` · ${num((x.context / x.context_window) * 100)}%` }) : null;
        const cache = x.tokens?.input ? h('span', { class: 'muted', text: ` · 缓存 ${num((x.tokens.cached / x.tokens.input) * 100)}%` }) : null;
        const folder = (x.cwd || '').split(/[\\/]/).filter(Boolean).pop() || '—';
        const sub = [x.entry ? ENTRY_TEXT[x.entry] || x.entry : null, x.started ? `${when(x.started)} 开始` : null].filter(Boolean).join(' · ');
        const name = [h('strong', { text: x.name }), sub ? h('span', { class: 'muted', text: ` ${sub}` }) : null];
        if (x.remote) return [name, unseen(), unseen(), sessionState(x, now), unseen(), unseen(), unseen(), when(x.last)];
        return [
          name,
          h('span', { title: x.cwd || '', text: folder }),
          x.model || '—',
          sessionState(x, now),
          [ctx, pct],
          [tokens(x.tokens?.input), cache],
          tokens(x.tokens?.output),
          when(x.last),
        ];
      }),
    );
  }

  return { el: s.c.el, card: s.c, charts: [speed], update };
}

// ---------- 正在运行的模型：各服务和工具里的模型汇总成一张表（看板顶部和悬浮窗共用） ----------

// 列最近 3 小时会话的工具，空闲超过这么久的会话不算正在用的模型
const MODEL_IDLE_MS = 30 * 60 * 1000;
const MODEL_ORDER = { waiting: 0, busy: 1, loading: 2, idle: 3 };
const COMFY_KIND = { lora: 'LoRA', clip: '文本编码器', vae: 'VAE', controlnet: 'ControlNet', upscale: '放大模型', other: '其他' };
const AGENT_STATE = { waiting: '等你操作', busy: '工作中', idle: '空闲' };

// 模型文件名去掉目录和扩展名
const fileTitle = (name) => name.split(/[\\/]/).pop().replace(/\.[^.]+$/, '');

// “4 分钟后卸载”“即将卸载”“常驻”
export function unloadText(t) {
  const s = until(t);
  return s.endsWith('后') ? `${s}卸载` : s;
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
    const state =
      m.state === 'generating'
        ? ['生成中', m.speed != null ? `${num(m.speed, 1)} tokens/s` : null].filter(Boolean).join(' · ')
        : { prompt: '处理提示词', loading: '加载中' }[m.state] || '已加载';
    return {
      name: m.name,
      ...ollamaWhere(m, r, procs),
      status: m.state === 'generating' || m.state === 'prompt' ? 'busy' : m.state === 'loading' ? 'loading' : 'idle',
      state,
      detail: [[m.params, m.quant].filter(Boolean).join(' · '), m.size ? bytes(m.size) : null, gpu, m.state === 'loading' ? null : unloadText(m.expires)]
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
      state: current ? '执行中' : '上次任务',
      detail: Object.keys(COMFY_KIND)
        .filter((k) => groups[k])
        .map((k) => `${COMFY_KIND[k]}：${groups[k].join('、')}`)
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
      detail: active.length ? `${sessionSummary(active)}：${active.map((s) => s.name).join('、')}` : '',
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
  const c = card('正在运行的模型', '', { wide: true });
  const table = dataTable([{ label: '模型' }, { label: '运行在' }, { label: '状态' }, { label: '详情', cls: 'wrap-col' }], {
    empty: '当前无正在运行的模型。Ollama 加载模型、ComfyUI 执行任务或命令行工具处于活跃会话时将在此展示。',
  });
  c.body.append(
    table.el,
    h('p', {
      class: 'footnote',
      text: 'Ollama 是已加载的模型；ComfyUI 读不到显存里有哪些模型，显示的是正在执行的任务用的模型，空闲时是上一个任务用的；命令行工具按模型合并会话。本地模型后面是它用的显卡，按模型进程占的显存看出来。',
    }),
    ...diagToggle(),
  );
  return {
    el: c.el,
    update(sample) {
      const list = runningModels(sample.runtimes, sample.t, sample.procs);
      const count = (status) => list.filter((m) => m.status === status).length;
      const parts = [count('busy') ? `${count('busy')} 个工作中` : null, count('waiting') ? `${count('waiting')} 个等你操作` : null];
      c.subEl.textContent = list.length ? [`${list.length} 个`, ...parts].filter(Boolean).join(' · ') : '';
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
};

export function runtimeView(r, gapMs) {
  return (VIEWS[r.kind] || genericView)(r, gapMs);
}
