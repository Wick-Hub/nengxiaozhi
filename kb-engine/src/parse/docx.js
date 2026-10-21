import mammoth from 'mammoth';
import * as cheerio from 'cheerio';
import { toMdTable } from './markdown.js';

export async function docxToBlocks(buffer) {
  const { value: html } = await mammoth.convertToHtml({ buffer });
  const $ = cheerio.load(html);
  const blocks = [];

  $('body').children().each((_, el) => {
    const $el = $(el);
    const tag = (el.tagName || '').toLowerCase();

    if (/^h[1-6]$/.test(tag)) {
      blocks.push({ type: 'heading', level: +tag[1], text: $el.text().trim() });
    } else if (tag === 'table') {
      const rows = [];
      $el.find('tr').each((__, tr) => {
        const cells = [];
        $(tr).find('td,th').each((___, td) => cells.push($(td).text().trim()));
        if (cells.length) rows.push(cells);
      });
      if (rows.length) blocks.push({ type: 'table', rows, text: toMdTable(rows) });
    } else if (tag === 'ol' || tag === 'ul') {
      const items = [];
      $el.children('li').each((__, li) => items.push('• ' + $(li).text().trim()));
      if (items.length) blocks.push({ type: 'para', text: items.join('\n') });
    } else if (tag === 'p') {
      const hasImg = $el.find('img').length > 0;
      const text = $el.text().trim();
      if (hasImg) blocks.push({ type: 'image', text: text || '文档内嵌图片' });
      else if (text) blocks.push({ type: 'para', text });
    }
  });

  return blocks;
}
