import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// 加载 kb-engine/.env（无第三方依赖；已存在的环境变量优先）
const envPath = path.resolve(__dirname, '../.env');
if (fs.existsSync(envPath)) {
  for (const line of fs.readFileSync(envPath, 'utf8').split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$/);
    if (m && !m[1].startsWith('#') && process.env[m[1]] === undefined) {
      process.env[m[1]] = m[2].replace(/^["']|["']$/g, '');
    }
  }
}

export const KB_ROOT = process.env.KB_ROOT || path.resolve(__dirname, '../../knowledge-base');
export const DATA_DIR = process.env.DATA_DIR || path.resolve(__dirname, '../data');
export const STORE_PATH = path.join(DATA_DIR, 'store.json');
export const EMBED_MODEL = process.env.EMBED_MODEL || 'Xenova/bge-small-zh-v1.5';
export const HF_ENDPOINT = process.env.HF_ENDPOINT || 'https://hf-mirror.com';
export const EMBED_DTYPE = process.env.EMBED_DTYPE || 'fp32';

export const CHUNK_MAX_CHARS = 400;
export const CHUNK_CARRY = 60;
export const CHUNK_MIN_CHARS = 40;

export const KTYPE_DIRS = ['政策文件', '办事指南', '常见问题', '服务事项', '组织机构'];

export const STORE_MODE = process.env.KB_STORE || 'mysql';

export const DB = {
  host: process.env.DB_HOST || '127.0.0.1',
  port: +(process.env.DB_PORT || 3306),
  user: process.env.DB_USER || 'root',
  password: process.env.DB_PASSWORD || 'please-change-me',
  database: process.env.DB_NAME || 'nengxiaozhi',
};

export const LLM = {
  apiKey: process.env.LLM_API_KEY || '',
  baseUrl: (process.env.LLM_BASE_URL || 'https://api.deepseek.com/v1').replace(/\/+$/, ''),
  model: process.env.LLM_MODEL || 'deepseek-chat',
  temperature: +(process.env.LLM_TEMPERATURE || 0.3),
  maxTokens: +(process.env.LLM_MAX_TOKENS || 1024),
  mock: process.env.LLM_MOCK === '1',
};

export const RERANK = {
  enabled: process.env.RERANK_ENABLED !== '0',
  model: process.env.RERANK_MODEL || 'onnx-community/bge-reranker-v2-m3-ONNX',
  topN: +(process.env.RERANK_TOPN || 30),
  dtype: process.env.RERANK_DTYPE || 'q8',
};
