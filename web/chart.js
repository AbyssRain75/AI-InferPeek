// 轻量折线图（canvas），带十字线悬停、键盘查看和共享提示框。

import { tr, locale } from './i18n.js';

const FONT = '11px system-ui, -apple-system, "Segoe UI", "Microsoft YaHei UI", sans-serif';
const PAD = { left: 44, right: 10, top: 8, bottom: 22 };

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const f = raw / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
}

// 从 0 开始的刻度，顶端取整到“好看”的数
export function niceScale(max, tickFormat, targetTicks = 4) {
  const step = niceStep(Math.max(max, 1e-9) / targetTicks);
  const top = Math.ceil(max / step - 1e-9) * step;
  const ticks = [];
  for (let i = 0; i * step <= top + step * 1e-6; i++) ticks.push(+(i * step).toFixed(6));
  return { top, ticks, tickFormat };
}

// 容量（GB）用 1、2、4、8、16… 的刻度，12 GB 显存、64 GB 内存都能正好顶格
export function capacityScale(max, tickFormat, maxIntervals = 4) {
  let step = 0.25;
  while (max / step > maxIntervals) step *= 2;
  const top = Math.ceil(max / step - 1e-9) * step;
  const ticks = [];
  for (let i = 0; i * step <= top + 1e-9; i++) ticks.push(i * step);
  return { top, ticks, tickFormat };
}

export function ago(sec) {
  // 取整到秒之后再分段，59.5～60 秒不会写成“60 秒前”
  const s = Math.round(sec);
  return s >= 60 ? tr('{n} 分钟前', { n: +(sec / 60).toFixed(1) }) : tr('{n} 秒前', { n: s });
}

export function clock(t) {
  return new Date(t).toLocaleTimeString(locale, { hour12: false });
}

let tipEl;
export function showTooltip(clientX, clientY, value, detail) {
  if (!tipEl) {
    tipEl = document.createElement('div');
    tipEl.className = 'tooltip';
    tipEl.setAttribute('role', 'status');
    document.body.appendChild(tipEl);
  }
  const v = document.createElement('div');
  v.className = 'tip-value';
  v.textContent = value;
  const d = document.createElement('div');
  d.className = 'tip-detail';
  d.textContent = detail;
  tipEl.replaceChildren(v, d);
  tipEl.hidden = false;
  const r = tipEl.getBoundingClientRect();
  let left = clientX + 14;
  let top = clientY - r.height - 12;
  if (left + r.width > window.innerWidth - 8) left = clientX - r.width - 14;
  if (left < 8) left = 8;
  if (top < 8) top = clientY + 16;
  tipEl.style.left = `${left}px`;
  tipEl.style.top = `${top}px`;
}

export function hideTooltip() {
  if (tipEl) tipEl.hidden = true;
}

export class LineChart {
  constructor(host, { label, format, scale, gapMs }) {
    this.label = label;
    this.format = format;
    this.scale = scale;
    this.gapMs = gapMs;
    this.points = [];
    this.t0 = 0;
    this.t1 = 1;
    this.w = 0;
    this.h = 0;
    this.pointer = null; // 鼠标位置
    this.focusT = null; // 键盘选中的时间点

    this.canvas = document.createElement('canvas');
    this.canvas.tabIndex = 0;
    this.canvas.setAttribute('role', 'img');
    host.appendChild(this.canvas);
    this.ctx = this.canvas.getContext('2d');
    this.observer = new ResizeObserver(() => this.resize());
    this.observer.observe(host);

    this.canvas.addEventListener('pointermove', (e) => {
      this.pointer = { x: e.offsetX, cx: e.clientX, cy: e.clientY };
      this.draw();
    });
    this.canvas.addEventListener('pointerleave', () => {
      this.pointer = null;
      hideTooltip();
      this.draw();
    });
    this.canvas.addEventListener('focus', () => {
      const vis = this.visible();
      this.focusT = vis.length ? vis[vis.length - 1].t : null;
      this.draw();
    });
    this.canvas.addEventListener('blur', () => {
      this.focusT = null;
      hideTooltip();
      this.draw();
    });
    this.canvas.addEventListener('keydown', (e) => {
      const step = { ArrowLeft: -1, ArrowRight: 1 }[e.key];
      if (e.key === 'Escape') this.canvas.blur();
      if (!step || this.focusT == null) return;
      e.preventDefault();
      const vis = this.visible();
      const i = this.nearest(vis, this.focusT);
      const j = Math.min(Math.max(i + step, 0), vis.length - 1);
      if (vis[j]) this.focusT = vis[j].t;
      this.draw();
    });
  }

  setScale(scale) {
    this.scale = scale;
  }

  // 卡片移除时调用。观察器不断开的话，图表连同它的画布和数据会一直留在内存里
  destroy() {
    this.observer.disconnect();
    // 鼠标正停在图上时卡片没了，不会再有 pointerleave，提示框要自己收起
    if (this.pointer || this.focusT != null) hideTooltip();
    this.points = [];
  }

  update(points, t0, t1) {
    this.points = points;
    this.t0 = t0;
    this.t1 = t1;
    this.draw();
  }

  resize() {
    const rect = this.canvas.parentElement.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    this.w = rect.width;
    this.h = rect.height;
    this.canvas.width = Math.round(rect.width * dpr);
    this.canvas.height = Math.round(rect.height * dpr);
    this.canvas.style.width = `${rect.width}px`;
    this.canvas.style.height = `${rect.height}px`;
    this.dpr = dpr;
    this.draw();
  }

  visible() {
    return this.points.filter((p) => p.t >= this.t0 && p.v != null);
  }

  nearest(vis, t) {
    let best = -1;
    let bestD = Infinity;
    for (let i = 0; i < vis.length; i++) {
      const d = Math.abs(vis[i].t - t);
      if (d < bestD) {
        bestD = d;
        best = i;
      }
    }
    return best;
  }

  draw() {
    const { ctx, w, h } = this;
    if (!w || !h || !this.scale) return;
    const c = {
      grid: cssVar('--grid'),
      axis: cssVar('--axis'),
      muted: cssVar('--text-muted'),
      series: cssVar('--series-1'),
      surface: cssVar('--surface-1'),
    };
    const pw = w - PAD.left - PAD.right;
    const ph = h - PAD.top - PAD.bottom;
    const { top, ticks, tickFormat } = this.scale;
    const span = Math.max(this.t1 - this.t0, 1);
    const x = (t) => PAD.left + ((t - this.t0) / span) * pw;
    const y = (v) => PAD.top + (1 - Math.min(Math.max(v, 0), top) / top) * ph;

    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.font = FONT;
    ctx.lineWidth = 1;

    // 网格和纵轴刻度
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = c.muted;
    for (const v of ticks) {
      const yy = Math.round(y(v)) + 0.5;
      ctx.strokeStyle = v === 0 ? c.axis : c.grid;
      ctx.beginPath();
      ctx.moveTo(PAD.left, yy);
      ctx.lineTo(w - PAD.right, yy);
      ctx.stroke();
      ctx.fillText(tickFormat(v), PAD.left - 6, yy);
    }

    // 横轴：起点、中点、现在
    ctx.textBaseline = 'alphabetic';
    const sec = span / 1000;
    ctx.textAlign = 'left';
    ctx.fillText(ago(sec), PAD.left, h - 6);
    ctx.textAlign = 'center';
    ctx.fillText(ago(sec / 2), PAD.left + pw / 2, h - 6);
    ctx.textAlign = 'right';
    ctx.fillText(tr('现在'), w - PAD.right, h - 6);

    // 按断档切分成若干段，每段画面积和折线
    const segments = [];
    let seg = [];
    let prevT = null;
    for (const p of this.points) {
      if (p.t < this.t0 - this.gapMs) continue;
      if (p.v == null || (prevT != null && p.t - prevT > this.gapMs)) {
        if (seg.length) segments.push(seg);
        seg = [];
      }
      if (p.v != null) seg.push(p);
      prevT = p.t;
    }
    if (seg.length) segments.push(seg);

    ctx.save();
    ctx.beginPath();
    ctx.rect(PAD.left, 0, pw, h - PAD.bottom);
    ctx.clip();
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    const base = y(0);
    for (const s of segments) {
      ctx.beginPath();
      ctx.moveTo(x(s[0].t), base);
      for (const p of s) ctx.lineTo(x(p.t), y(p.v));
      ctx.lineTo(x(s[s.length - 1].t), base);
      ctx.closePath();
      ctx.globalAlpha = 0.1;
      ctx.fillStyle = c.series;
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.beginPath();
      s.forEach((p, i) => (i ? ctx.lineTo(x(p.t), y(p.v)) : ctx.moveTo(x(p.t), y(p.v))));
      ctx.lineWidth = 2;
      ctx.strokeStyle = c.series;
      ctx.stroke();
    }
    ctx.restore();

    const dot = (p) => {
      ctx.beginPath();
      ctx.arc(x(p.t), y(p.v), 4, 0, Math.PI * 2);
      ctx.fillStyle = c.series;
      ctx.fill();
      ctx.lineWidth = 2;
      ctx.strokeStyle = c.surface;
      ctx.stroke();
    };

    const vis = this.visible();
    const last = vis[vis.length - 1];
    this.canvas.setAttribute(
      'aria-label',
      last
        ? tr('{label}，当前 {value}。获得焦点后用左右方向键查看历史数值', { label: this.label, value: this.format(last.v) })
        : tr('{label}，暂无数据', { label: this.label }),
    );

    // 悬停或键盘选中的点
    let hi = -1;
    if (this.pointer) {
      const t = this.t0 + ((this.pointer.x - PAD.left) / pw) * span;
      hi = this.nearest(vis, t);
    } else if (this.focusT != null) {
      hi = this.nearest(vis, this.focusT);
    }
    if (hi >= 0) {
      const p = vis[hi];
      const xx = Math.round(x(p.t)) + 0.5;
      ctx.lineWidth = 1;
      ctx.strokeStyle = c.axis;
      ctx.beginPath();
      ctx.moveTo(xx, PAD.top);
      ctx.lineTo(xx, h - PAD.bottom);
      ctx.stroke();
      dot(p);
      const detail = `${clock(p.t)} · ${ago((this.t1 - p.t) / 1000)}`;
      if (this.pointer) {
        showTooltip(this.pointer.cx, this.pointer.cy, this.format(p.v), detail);
      } else {
        const r = this.canvas.getBoundingClientRect();
        showTooltip(r.left + x(p.t), r.top + y(p.v), this.format(p.v), detail);
      }
    } else if (last) {
      dot(last);
    }
    if (hi >= 0 && last && vis[hi] !== last) dot(last);
  }
}
