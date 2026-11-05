// 看板和悬浮窗共用：连接 /api/stream 接收样本，标题栏的状态点显示实时 / 数据停更（连着但没有新样本）/ 连接断开。

import { elapsed } from './ui.js';

// onInit(data)：连上（断线重连也是）时收到的静态信息和全部历史；onSample(s)：每条新样本。
// 收到数据后看得见时调 render()；hidden() 为 true 时只收数据、不渲染，重新看得见时调返回的 refresh() 补一次。
// downText：连接断开时状态点的文字
export function connectStream({ onInit, onSample, render, hidden = () => document.hidden, downText }) {
  const el = document.getElementById('status');
  let connected = false;
  let lastSampleAt = 0; // 最后一次收到样本的时间（本机时钟）
  let interval = 1;

  function updateStatus() {
    const idle = Date.now() - lastSampleAt;
    // 连接还在、但超过采样间隔的 5 倍（至少 5 秒）没有新样本，说明后台采集停住了
    const status = !connected ? 'down' : idle > Math.max(5, interval * 5) * 1000 ? 'stalled' : 'live';
    el.classList.toggle('is-live', status === 'live');
    el.classList.toggle('is-stalled', status === 'stalled');
    el.querySelector('.label').textContent = { live: '实时', stalled: '数据停更', down: downText }[status];
    el.title = status === 'stalled' ? `已经 ${elapsed(idle)}没有收到新数据，后台采集可能卡住了` : '';
    document.body.classList.toggle('stale', status !== 'live');
  }

  // 第一次连上之前保持“连接中…”
  function refresh() {
    if (hidden() || !lastSampleAt) return;
    updateStatus();
    render();
  }

  const es = new EventSource('/api/stream');
  es.addEventListener('init', (e) => {
    const data = JSON.parse(e.data);
    const last = data.history[data.history.length - 1];
    interval = data.info?.interval ?? 1;
    // 按服务端时间算最新一条样本已经多久了，换算成本机时间
    lastSampleAt = last && data.now ? Date.now() - (data.now - last.t) : Date.now();
    connected = true;
    onInit(data);
    refresh();
  });
  es.addEventListener('sample', (e) => {
    onSample(JSON.parse(e.data));
    lastSampleAt = Date.now();
    refresh();
  });
  es.addEventListener('error', () => {
    connected = false;
    updateStatus();
  });
  // 没有新样本时不会触发任何事件，定时检查
  setInterval(() => lastSampleAt && !hidden() && updateStatus(), 1000);
  // 切回这个标签页、窗口从最小化还原时马上按最新数据刷新
  document.addEventListener('visibilitychange', refresh);
  return { refresh };
}
