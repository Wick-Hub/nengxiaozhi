/**
 * 关键词检索前后端一致性探针（任务 1.11）。
 *
 * 加载 static/preview.js（走 __NXZ_EXPORT__ 分支，不触碰 DOM），
 * 对同一批用例输出 findRanges / parseTerms 的结果，供 tools/verify_preview.py 比对。
 *
 * 用法： node tools/preview_probe.mjs <cases.json>
 * 输入： {"ranges": [[haystack, term], ...], "terms": [query, ...],
 *          "images": [[docId, image, {}], ...]}
 * 输出： {"ranges": [[[start, end], ...], ...], "terms": [[term, ...], ...],
 *          "images": [{"url": "...", "html": "..."}, ...]}
 */
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

globalThis.__NXZ_EXPORT__ = true;

const src = readFileSync(new URL('../static/preview.js', import.meta.url), 'utf8');
vm.runInThisContext(src, { filename: 'preview.js' });

const api = globalThis.__NXZ_PREVIEW__;
if (!api) {
  process.stderr.write('preview.js 未导出 __NXZ_PREVIEW__\n');
  process.exit(1);
}

const payload = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const ranges = (payload.ranges || []).map(([hay, term]) => api.findRanges(hay, term));
const terms = (payload.terms || []).map(q => api.parseTerms(q));
const images = (payload.images || []).map(([docId, img]) => ({
  url: api.imageUrl(docId, img.key),
  html: api.imageCard(docId, img),
}));

process.stdout.write(JSON.stringify({ ranges, terms, images }));
