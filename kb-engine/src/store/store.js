import fs from 'node:fs';
import path from 'node:path';
import { STORE_PATH, STORE_MODE } from '../config.js';
import { loadStoreMysql, saveStoreMysql } from './mysql.js';

export function storeExists() {
  return STORE_MODE === 'mysql' ? true : fs.existsSync(STORE_PATH);
}

export async function loadStore() {
  if (STORE_MODE === 'mysql') return loadStoreMysql();
  if (!storeExists()) {
    throw new Error(`未找到知识库索引（${STORE_PATH}），请先运行：npm run ingest`);
  }
  return JSON.parse(fs.readFileSync(STORE_PATH, 'utf8'));
}

export async function saveStore(store) {
  if (STORE_MODE === 'mysql') return saveStoreMysql(store);
  fs.mkdirSync(path.dirname(STORE_PATH), { recursive: true });
  fs.writeFileSync(STORE_PATH, JSON.stringify(store));
}
