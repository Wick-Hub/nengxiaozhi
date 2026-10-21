import fs from 'node:fs';
import path from 'node:path';
import { parseFrontmatter } from './frontmatter.js';
import { mdToBlocks } from './markdown.js';
import { docxToBlocks } from './docx.js';
import { pdfToBlocks } from './pdf.js';
import { xlsxToBlocks, csvToBlocks } from './xlsx.js';
import { jsonToBlocks } from './json.js';

export const SUPPORTED = ['.md', '.txt', '.docx', '.pdf', '.xlsx', '.xls', '.csv', '.json'];

export async function parseFile(filePath) {
  const ext = path.extname(filePath).toLowerCase();
  const raw = fs.readFileSync(filePath);
  let meta = {};
  let blocks = [];

  if (ext === '.md') {
    const { meta: m, body } = parseFrontmatter(raw.toString('utf8'));
    meta = m;
    blocks = mdToBlocks(body);
  } else if (ext === '.txt') {
    blocks = mdToBlocks(raw.toString('utf8'));
  } else if (ext === '.docx') {
    blocks = await docxToBlocks(raw);
  } else if (ext === '.pdf') {
    blocks = await pdfToBlocks(raw);
  } else if (ext === '.xlsx' || ext === '.xls') {
    blocks = xlsxToBlocks(raw);
  } else if (ext === '.csv') {
    blocks = csvToBlocks(raw);
  } else if (ext === '.json') {
    blocks = jsonToBlocks(JSON.parse(raw.toString('utf8')));
  }

  return { meta, blocks };
}
