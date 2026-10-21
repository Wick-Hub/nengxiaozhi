/*!
 * 能小智嵌入 SDK v1.0.0（任务 3.9）
 *
 * 方式一（自动初始化）：
 *   <script src="https://ai.example.edu.cn/sdk/nxz.js"
 *           data-channel="campus" data-position="right"
 *           data-color="#3366ff" data-label="有问题问我"></script>
 *
 * 方式二（编程式，推荐在登录后调用 setToken）：
 *   var nxz = NengXiaoZhi.init({ channel: 'campus' });
 *   nxz.setToken(token).open();
 *
 * 身份透传：SDK 通过 postMessage 把 token / user 交给同源 iframe，
 * iframe 只保存在内存中、不写 localStorage；token 不会出现在父页 URL 上。
 */
(function (global, document) {
  'use strict';

  var VERSION = '1.0.0';
  var MSG_READY = 'nxz:sdk:ready';
  var MSG_INIT = 'nxz:sdk:init';
  var MSG_STATE = 'nxz:sdk:state';
  var MSG_CLOSE = 'nxz:sdk:close';
  var DEFAULT_COLOR = '#3366ff';
  var instances = {};

  function textValue(value, fallback) {
    return typeof value === 'string' && value.trim() ? value.trim() : fallback;
  }

  function boolValue(value, fallback) {
    if (value === true || value === '1' || value === 'true') return true;
    if (value === false || value === '0' || value === 'false') return false;
    return fallback;
  }

  function intValue(value, fallback, min, max) {
    var n = parseInt(value, 10);
    if (!isFinite(n)) n = fallback;
    return Math.max(min, Math.min(max, n));
  }

  function colorValue(value) {
    var raw = typeof value === 'string' ? value.trim() : '';
    return /^#[0-9a-fA-F]{3,8}$/.test(raw) ? raw : DEFAULT_COLOR;
  }

  function userValue(raw) {
    if (raw && typeof raw === 'object') return raw;
    if (typeof raw !== 'string' || !raw.trim()) return null;
    try {
      var parsed = JSON.parse(raw);
      return parsed && typeof parsed === 'object' ? parsed : { userNo: raw.trim() };
    } catch (e) {
      return { userNo: raw.trim() };
    }
  }

  function baseFromScript(script) {
    var src = (script && script.src) || '';
    if (!src) return '';
    return src.replace(/\/sdk\/nxz(?:\.min)?\.js(?:\?.*)?$/i, '');
  }

  // 脚本执行时 currentScript 指向自身；必须在加载期缓存，否则编程式
  // init()（load / 点击回调里调用）拿到的 currentScript 为 null。
  var SCRIPT_BASE = baseFromScript(document.currentScript);

  function originOf(url) {
    var anchor = document.createElement('a');
    anchor.href = url || '';
    if (!anchor.protocol || anchor.protocol === ':') return '*';
    return anchor.protocol + '//' + anchor.host;
  }

  function normalize(options, script) {
    var opts = options || {};
    var baseUrl = textValue(
      opts.baseUrl,
      baseFromScript(script || document.currentScript) || SCRIPT_BASE
    );
    return {
      channel: textValue(opts.channel, ''),
      instanceId: textValue(opts.instanceId, 'default'),
      baseUrl: baseUrl.replace(/\/+$/, ''),
      position: opts.position === 'left' ? 'left' : 'right',
      color: colorValue(opts.color),
      label: textValue(opts.label, '有问题问我'),
      title: textValue(opts.title, '能小智'),
      width: intValue(opts.width, 400, 280, 720),
      height: intValue(opts.height, 600, 360, 900),
      offset: intValue(opts.offset, 24, 0, 200),
      zIndex: intValue(opts.zIndex, 2147483000, 1, 2147483647),
      token: typeof opts.token === 'string' ? opts.token : '',
      user: userValue(opts.user),
      autoOpen: boolValue(opts.autoOpen, false),
      hideLauncher: boolValue(opts.hideLauncher, false),
      onReady: typeof opts.onReady === 'function' ? opts.onReady : null,
      onOpen: typeof opts.onOpen === 'function' ? opts.onOpen : null,
      onState: typeof opts.onState === 'function' ? opts.onState : null,
    };
  }

  function Widget(config) {
    this.config = config;
    this.id = config.channel + ':' + config.instanceId;
    this.isOpen = false;
    this.destroyed = false;
    this._handlers = {};
    this._targetOrigin = originOf(config.baseUrl);
    this._build();
    this._listen();
    if (config.autoOpen) this.open();
  }

  Widget.prototype._build = function () {
    var cfg = this.config;
    var side = cfg.position + ':' + cfg.offset + 'px;';
    var owner = document.body || document.documentElement;

    var button = document.createElement('button');
    button.type = 'button';
    button.textContent = cfg.label;
    button.setAttribute('aria-label', cfg.label);
    button.setAttribute('aria-expanded', 'false');
    button.setAttribute('aria-controls', 'nxz-sdk-panel-' + cfg.instanceId);
    button.setAttribute('data-nxz-sdk-launcher', cfg.channel);
    button.style.cssText =
      'position:fixed;' + side + 'bottom:' + cfg.offset + 'px;z-index:' + cfg.zIndex + ';' +
      'border:0;border-radius:999px;padding:12px 18px;background:' + cfg.color + ';' +
      'color:#fff;font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;' +
      'cursor:pointer;box-shadow:0 8px 24px rgba(0,0,0,.25);' +
      (cfg.hideLauncher ? 'display:none;' : '');

    var panel = document.createElement('div');
    panel.id = 'nxz-sdk-panel-' + cfg.instanceId;
    panel.setAttribute('data-nxz-sdk-panel', cfg.channel);
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-label', cfg.title);
    panel.hidden = true;
    panel.style.cssText =
      'position:fixed;' + side + 'bottom:' + (cfg.offset + 58) + 'px;z-index:' + (cfg.zIndex + 1) + ';' +
      'width:' + cfg.width + 'px;max-width:calc(100vw - ' + (cfg.offset * 2) + 'px);' +
      'height:' + cfg.height + 'px;max-height:calc(100vh - ' + (cfg.offset + 88) + 'px);' +
      'border-radius:14px;overflow:hidden;box-shadow:0 18px 60px rgba(0,0,0,.35);background:#fff;';

    var frame = document.createElement('iframe');
    frame.title = cfg.title;
    frame.setAttribute('allow', 'clipboard-write');
    frame.setAttribute('referrerpolicy', 'strict-origin-when-cross-origin');
    frame.style.cssText = 'width:100%;height:100%;border:0;display:block';
    var query = '?channel=' + encodeURIComponent(cfg.channel) + '&embed=1&sdk=1';
    frame.src = (cfg.baseUrl || '') + '/' + query;

    var close = document.createElement('button');
    close.type = 'button';
    close.textContent = '×';
    close.setAttribute('aria-label', '关闭');
    close.style.cssText =
      'position:absolute;top:8px;right:8px;width:28px;height:28px;border:0;border-radius:50%;' +
      'background:rgba(0,0,0,.55);color:#fff;font-size:18px;line-height:1;cursor:pointer';

    panel.appendChild(frame);
    panel.appendChild(close);
    owner.appendChild(button);
    owner.appendChild(panel);

    var self = this;
    this.button = button;
    this.panel = panel;
    this.frame = frame;
    this.closeButton = close;
    button.addEventListener('click', function () { self.toggle(); });
    close.addEventListener('click', function () { self.close(); });
    frame.addEventListener('load', function () { self.sendInit(); });
  };

  Widget.prototype._listen = function () {
    var self = this;
    this._onMessage = function (event) {
      if (!self.frame.contentWindow || event.source !== self.frame.contentWindow) return;
      var data = event.data || {};
      if (data.type === MSG_READY) {
        self.sendInit();
        self._emit('ready', data);
        if (self.config.onReady) self.config.onReady(self);
      } else if (data.type === MSG_STATE) {
        self._emit('state', data);
        if (self.config.onState) self.config.onState(data);
      } else if (data.type === MSG_CLOSE) self.close();
    };
    global.addEventListener('message', this._onMessage);
    this._onKey = function (event) {
      if (event.key === 'Escape' && self.isOpen) self.close();
    };
    document.addEventListener('keydown', this._onKey);
  };

  Widget.prototype.sendInit = function () {
    if (this.destroyed || !this.frame.contentWindow) return this;
    var target = this._targetOrigin || '*';
    this.frame.contentWindow.postMessage({
      type: MSG_INIT,
      version: VERSION,
      channel: this.config.channel,
      token: this.config.token || '',
      user: this.config.user || null,
    }, target);
    return this;
  };

  Widget.prototype.setToken = function (token) {
    this.config.token = typeof token === 'string' ? token : '';
    return this.sendInit();
  };

  Widget.prototype.setUser = function (user) {
    this.config.user = userValue(user);
    return this.sendInit();
  };

  Widget.prototype.open = function () {
    if (this.destroyed) return this;
    this.panel.hidden = false;
    this.button.setAttribute('aria-expanded', 'true');
    this.isOpen = true;
    try { this.frame.focus(); } catch (e) { /* ignore */ }
    this._emit('open');
    if (this.config.onOpen) this.config.onOpen(this);
    return this;
  };

  Widget.prototype.close = function () {
    if (this.destroyed) return this;
    this.panel.hidden = true;
    this.button.setAttribute('aria-expanded', 'false');
    this.isOpen = false;
    this._emit('close');
    return this;
  };

  Widget.prototype.toggle = function () {
    return this.isOpen ? this.close() : this.open();
  };

  Widget.prototype.on = function (name, handler) {
    if (typeof handler === 'function') {
      (this._handlers[name] || (this._handlers[name] = [])).push(handler);
    }
    return this;
  };

  Widget.prototype._emit = function (name, detail) {
    var list = this._handlers[name] || [];
    for (var i = 0; i < list.length; i++) {
      try { list[i](detail, this); } catch (e) { /* 回调异常不影响浮窗 */ }
    }
  };

  Widget.prototype.destroy = function () {
    if (this.destroyed) return;
    this.destroyed = true;
    global.removeEventListener('message', this._onMessage);
    document.removeEventListener('keydown', this._onKey);
    if (this.button.parentNode) this.button.parentNode.removeChild(this.button);
    if (this.panel.parentNode) this.panel.parentNode.removeChild(this.panel);
    delete instances[this.id];
  };

  function init(options) {
    var script = document.currentScript;
    var cfg = normalize(options, script);
    if (!cfg.channel) {
      if (global.console && console.warn) console.warn('[nxz-sdk] 缺少 channel，未初始化');
      return null;
    }
    var id = cfg.channel + ':' + cfg.instanceId;
    if (instances[id]) return instances[id];
    var widget = new Widget(cfg);
    instances[id] = widget;
    return widget;
  }

  function get(channel, instanceId) {
    return instances[channel + ':' + (instanceId || 'default')] || null;
  }

  function autoInit() {
    var script = document.currentScript;
    if (!script || !script.getAttribute('data-channel')) {
      var nodes = document.querySelectorAll('script[src*="/sdk/nxz.js"]');
      script = nodes.length ? nodes[nodes.length - 1] : null;
    }
    if (!script || !script.getAttribute('data-channel')) return;
    var data = script;
    init({
      channel: data.getAttribute('data-channel'),
      position: data.getAttribute('data-position'),
      color: data.getAttribute('data-color'),
      label: data.getAttribute('data-label'),
      title: data.getAttribute('data-title'),
      width: data.getAttribute('data-width'),
      height: data.getAttribute('data-height'),
      offset: data.getAttribute('data-offset'),
      zIndex: data.getAttribute('data-z-index'),
      token: data.getAttribute('data-token') || '',
      user: data.getAttribute('data-user'),
      autoOpen: data.getAttribute('data-auto-open'),
      hideLauncher: data.getAttribute('data-hide-launcher'),
      baseUrl: data.getAttribute('data-base-url') || baseFromScript(data),
    });
  }

  global.NengXiaoZhi = {
    version: VERSION,
    init: init,
    get: get,
    open: function (channel, instanceId) {
      var widget = get(channel, instanceId);
      return widget ? widget.open() : null;
    },
    close: function (channel, instanceId) {
      var widget = get(channel, instanceId);
      return widget ? widget.close() : null;
    },
    destroy: function (channel, instanceId) {
      var widget = get(channel, instanceId);
      if (widget) widget.destroy();
      return null;
    },
  };

  if (document.currentScript && document.currentScript.getAttribute('data-channel')) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', autoInit, { once: true });
    } else {
      autoInit();
    }
  }
})(window, document);
