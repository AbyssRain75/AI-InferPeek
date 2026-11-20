// 界面语言。源码里的界面文字照常写中文，用 tr('中文') 包起来：当前语言是英文时换成译文（web/i18n/en.json），没有译文的用中文。
// 文字里的 {名字} 是占位：tr('版本 {v}', { v: '1.0' })，先翻译再填，译文里的占位可以换位置。后端 aiinferpeek/i18n.py 写法一样。
// 当前语言和译文由页面 <head> 里的 api/i18n.js 放在 window.I18N 里（服务端按设置里的“界面语言”给），这里直接用，不用等网络。
// 静态 HTML 里的文字：元素带 data-i18n 的换掉文字，带 data-i18n-attr="title aria-label" 的换掉列出的属性；这个模块加载时换好。

const data = window.I18N || {};
export const lang = data.lang || 'zh-CN';
export const isEnglish = lang === 'en';
// 数字、时间的格式：toLocaleString(locale)
export const locale = isEnglish ? 'en-US' : 'zh-CN';
const messages = data.messages || {};

// {n} 换成 params.n；译文里的 {n?model|models} 按 n 是不是 1 选单数或复数（英文要分，中文不用）。params 里没有的原样留着
export function fill(text, params) {
  if (!params) return text;
  return text.replace(/\{(\w+)(?:\?([^|{}]*)\|([^{}]*))?\}/g, (m, k, one, other) => {
    if (!Object.hasOwn(params, k)) return m;
    if (one === undefined) return String(params[k]);
    return Number(params[k]) === 1 ? one : other;
  });
}

export function tr(text, params) {
  return fill(Object.hasOwn(messages, text) ? messages[text] : text, params);
}

// 同一句中文在不同地方要译成不同英文时（比如“{n} 个”），带上说明用在哪：译文的键是“说明|中文”，中文界面照样显示中文
export function trc(context, text, params) {
  const key = `${context}|${text}`;
  return fill(Object.hasOwn(messages, key) ? messages[key] : text, params);
}

export function translatePage(root = document) {
  for (const el of root.querySelectorAll('[data-i18n]')) el.textContent = tr(el.textContent.trim());
  for (const el of root.querySelectorAll('[data-i18n-attr]')) {
    for (const name of el.dataset.i18nAttr.split(/\s+/).filter(Boolean)) {
      const value = el.getAttribute(name);
      if (value) el.setAttribute(name, tr(value));
    }
  }
}

// 设置页面改了界面语言：每条样本里带着服务端现在的语言（device.lang），和这个页面加载时的不一样就刷新页面。
// 悬浮窗也是直接刷新：主题、小窗模式都记在地址里（见 mini.js 的 setCompact），刷新后还在
export function followLanguage(sample) {
  const now = sample?.device?.lang;
  if (now && now !== lang && !followLanguage.reloading) {
    followLanguage.reloading = true;
    location.reload();
  }
}

translatePage();
document.documentElement.lang = locale;
document.documentElement.classList.remove('i18n-pending'); // 见 api/i18n.js：换好文字前先不显示，免得闪一下中文
