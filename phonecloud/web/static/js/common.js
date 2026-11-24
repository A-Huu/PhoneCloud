'use strict';

// 用户端和管理页共用的小工具：DOM 构建、接口请求、离线提示、消息提示。
(function () {
  const PC = {};

  // ---- DOM ----

  /** 创建元素。文本一律走文本节点，文件名等内容不会被当成 HTML 解析。 */
  PC.h = function (tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') el.className = value;
      else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
      else if (value === true) el.setAttribute(key, '');
      else el.setAttribute(key, value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  };

  const ICONS = {
    folder: 'M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z',
    file: 'M6 2h8l5 5v13a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2z M14 2v5h5',
    image:
      'M4 4h16a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1z M8.5 10a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3z M21 16l-5-5L6 20',
    video: 'M3 6a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z M10 9l5 3-5 3z',
    audio: 'M9 18V6l10-2v12 M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0z M19 16a3 3 0 1 1-6 0 3 3 0 0 1 6 0z',
    play: 'M7 4l13 8-13 8z',
    upload: 'M12 16V4 M7 9l5-5 5 5 M4 20h16',
    download: 'M12 4v12 M7 11l5 5 5-5 M4 20h16',
    close: 'M6 6l12 12 M18 6L6 18',
    prev: 'M15 5l-7 7 7 7',
    next: 'M9 5l7 7-7 7',
    drive: 'M4 5h16a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z M3 14h18 M7 16.5h.01',
    copy: 'M9 3h6a1 1 0 0 1 1 1v1h1a2 2 0 0 1 2 2v13a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h1V4a1 1 0 0 1 1-1z M9 5h6V4H9z',
    lock: 'M6 11h12v9H6z M8 11V8a4 4 0 0 1 8 0v3',
    offline: 'M3 3l18 18 M8.5 16.5a5 5 0 0 1 7 0 M5 13a10 10 0 0 1 5-2.7 M19 13a10 10 0 0 0-2.5-1.8 M12 20h.01',
  };

  PC.icon = function (name) {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('class', 'icon');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS(ns, 'path');
    path.setAttribute('d', ICONS[name] || ICONS.file);
    svg.append(path);
    return svg;
  };

  // ---- 格式化 ----

  PC.fmtSize = function (bytes) {
    if (bytes === null || bytes === undefined) return '';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    const digits = unit === 0 || value >= 100 ? 0 : value >= 10 ? 1 : 2;
    return value.toFixed(digits) + ' ' + units[unit];
  };

  PC.fmtTime = function (seconds) {
    if (!seconds) return '';
    const d = new Date(seconds * 1000);
    const pad = (n) => String(n).padStart(2, '0');
    return (
      d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes())
    );
  };

  // ---- 消息提示 ----

  PC.toast = function (message, type) {
    let box = document.querySelector('.toasts');
    if (!box) {
      box = PC.h('div', { class: 'toasts', role: 'status', 'aria-live': 'polite' });
      document.body.append(box);
    }
    const item = PC.h('div', { class: 'toast' + (type ? ' ' + type : '') }, message);
    box.append(item);
    setTimeout(() => item.remove(), type === 'error' ? 6000 : 3500);
  };

  // ---- 连接状态 ----
  // 电脑关机、睡眠或服务停止时，请求会直接失败。此时在页面顶部给出明确提示并自动重试，
  // 恢复后通知页面刷新当前内容，而不是让用户看到浏览器的报错页。

  let offline = false;
  let retryTimer = null;
  PC.onReconnect = null;

  function setOffline(value) {
    if (offline === value) return;
    offline = value;
    const banner = document.getElementById('offline-banner');
    if (banner) banner.hidden = !value;
    if (value) {
      retryTimer = setInterval(PC.checkConnection, 3000);
    } else {
      clearInterval(retryTimer);
      retryTimer = null;
      if (PC.onReconnect) PC.onReconnect();
    }
  }

  PC.isOffline = () => offline;

  /** 主动探测一次服务是否可达（图片、视频加载失败时用它判断是不是断线了）。 */
  PC.checkConnection = async function () {
    try {
      const response = await fetch('/api/ping', { cache: 'no-store' });
      setOffline(!response.ok);
    } catch (err) {
      setOffline(true);
    }
    return !offline;
  };

  // ---- 接口请求 ----

  class ApiError extends Error {
    constructor(status, message) {
      super(message);
      this.status = status;
      this.offline = status === 0;
    }
  }
  PC.ApiError = ApiError;
  PC.onUnauthorized = null;

  function errorMessage(data, status) {
    if (data && typeof data.detail === 'string') return data.detail;
    return '请求失败（' + status + '）';
  }
  PC.errorMessage = errorMessage;

  /** 调用 JSON 接口。options.quiet401 为 true 时，401 不触发"跳回登录页"（登录请求自己处理）。 */
  PC.api = async function (method, url, body, options) {
    let response;
    try {
      response = await fetch(url, {
        method,
        headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: 'same-origin',
        cache: 'no-store',
      });
    } catch (err) {
      setOffline(true);
      throw new ApiError(0, '无法连接到电脑，请确认电脑已开机且 PhoneCloud 正在运行');
    }
    setOffline(false);
    let data = null;
    try {
      data = await response.json();
    } catch (err) {
      data = null;
    }
    if (!response.ok) {
      if (response.status === 401 && !(options && options.quiet401) && PC.onUnauthorized) PC.onUnauthorized();
      throw new ApiError(response.status, errorMessage(data, response.status));
    }
    return data;
  };

  window.PC = PC;
})();
