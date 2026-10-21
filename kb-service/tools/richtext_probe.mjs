/**
 * 前端富文本渲染探针（任务 1.9）。
 *
 * 在 Node 里加载 `static/app.js`（补上最小浏览器全局桩），调用其中的
 * `renderContent()`，把结果以 JSON 打印到 stdout，供
 * `tools/verify_richtext.py` 与 `tests/test_richtext.py` 与后端
 * `app.core.richtext.render_html()` 做逐字符比对。
 *
 * 用法::
 *
 *     node tools/richtext_probe.mjs <cases.json>
 *
 * `cases.json` 为 `[[name, markdown], ...]`；输出为 `{name: html}`。
 * app.js 顶部的 `globalThis.__NXZ_EXPORT__` 测试钩子负责把渲染器暴露出来。
 */

import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP_JS = path.join(HERE, '..', 'static', 'app.js');

/** 宽松 DOM 桩：任意属性可读写、任意方法可调用，返回值仍是桩。 */
function makeStub() {
  const target = function stubFn() { return makeStub(); };
  return new Proxy(target, {
    get(t, prop) {
      if (prop === 'value' || prop === 'textContent' || prop === 'innerHTML') return '';
      if (prop === 'scrollHeight' || prop === 'scrollTop') return 0;
      if (prop === 'className') return '';
      if (prop === 'classList') return { add() {}, remove() {}, toggle() {} };
      if (prop === 'style') return {};
      if (prop === 'parentElement') return makeStub();
      if (prop === Symbol.toPrimitive) return () => '';
      if (prop === 'then') return undefined;
      if (!(prop in t)) t[prop] = makeStub();
      return t[prop];
    },
    set(t, prop, v) { t[prop] = v; return true; },
    apply() { return makeStub(); },
  });
}

globalThis.document = {
  getElementById: () => makeStub(),
  createElement: () => makeStub(),
  addEventListener() {},
};
globalThis.localStorage = {
  getItem: () => null,
  setItem() {},
  removeItem() {},
};
globalThis.location = { search: '', href: 'http://127.0.0.1:8000/' };
globalThis.__NXZ_EXPORT__ = {};

const casesPath = process.argv[2];
if (!casesPath) {
  console.error('用法: node tools/richtext_probe.mjs <cases.json>');
  process.exit(2);
}

const cases = JSON.parse(fs.readFileSync(casesPath, 'utf8'));
const source = fs.readFileSync(APP_JS, 'utf8');
vm.runInThisContext(source, { filename: APP_JS });

const renderContent = globalThis.__NXZ_EXPORT__.renderContent;
if (typeof renderContent !== 'function') {
  console.error('未从 app.js 取到 renderContent（测试钩子是否被移除？）');
  process.exit(3);
}

const out = {};
for (const [name, md] of cases) {
  out[name] = renderContent(md);
}
process.stdout.write(JSON.stringify(out));
