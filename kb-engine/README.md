# kb-engine —— 知识库原型引擎

对 `../knowledge-base/` 目录下的知识文档完成 **解析 → 切片 → 向量化 → 混合检索** 的最小可运行实现，用于在开发环境验证 RAG 管线与检索效果，并作为生产实现的参考基准。

## 快速开始

```bash
npm install                 # 首次安装依赖（含 onnxruntime，体积较大）
npm run gen:xlsx            # 生成 xlsx 示例知识（可选，已提交可跳过）
npm run ingest              # 构建知识库索引（首次运行自动下载嵌入模型约 90MB）

# 检索验证（--role 模拟登录身份做权限过滤）
npm run ask -- "差旅住宿费标准是多少" --role 教职工
npm run ask -- "一卡通丢了怎么办" --role 本科生
npm run ask -- "宿舍是几人间有空调吗" --role 访客
```

## 检索模式

```bash
node src/query.js "问题" --mode hybrid   # 默认：向量 + BM25，RRF 融合
node src/query.js "问题" --mode vector   # 纯向量（语义）
node src/query.js "问题" --mode bm25     # 纯关键词（bigram）
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HF_ENDPOINT` | `https://hf-mirror.com` | 嵌入模型下载源（可改为 `https://huggingface.co`） |
| `HTTPS_PROXY` | - | 下载模型走代理（如 `http://127.0.0.1:7897`） |
| `KB_ROOT` | `../knowledge-base` | 知识目录 |
| `EMBED_MODEL` | `Xenova/bge-small-zh-v1.5` | 嵌入模型（512 维，本地 CPU） |
| `EMBED_DTYPE` | `fp32` | 模型精度（可改 `q8` 减小体积） |

## 目录结构

```
kb-engine/
├── src/
│   ├── config.js               # 路径与切片参数
│   ├── parse/                  # 多格式解析 → 结构化块（标题/段落/表格/图片/问答）
│   │   ├── markdown.js  docx.js  pdf.js  xlsx.js  json.js  frontmatter.js
│   ├── chunk/chunker.js        # 标题层级切片；表格整块；FAQ 整对；角色元数据
│   ├── embed/embedder.js       # bge-small-zh-v1.5 本地向量化（transformers.js）
│   ├── store/store.js          # data/store.json 索引存取
│   ├── retrieve/
│   │   ├── bm25.js             # 中文 bigram BM25
│   │   └── retriever.js        # 混合检索 + RRF 融合 + 角色权限过滤
│   ├── ingest.js               # 构建索引 CLI
│   └── query.js                # 检索查询 CLI
├── scripts/gen-xlsx-sample.mjs # xlsx 示例生成器
└── data/store.json             # 构建产物（切片 + 向量 + 元数据）
```

## 原型 vs 生产实现差距

| 环节 | 原型实现 | 生产实现（对应招标条款） |
| --- | --- | --- |
| 文档解析 | md 原生；docx(mammoth)/pdf(pdf.js)/xlsx(SheetJS) 结构解析 | + 扫描件 OCR、版面分析（MinerU / RAGFlow DeepDoc）▲M-K-06 |
| 切片 | 标题层级 + 400 字滚动窗口；表格整块；FAQ 整对 | + 图片原文保留（MinIO 本地存储）▲M-K-06 |
| 向量化 | bge-small-zh-v1.5（本地 CPU，512 维） | BGE-M3（稠密+稀疏）C-02 |
| 向量存储 | store.json + 内存余弦 | Milvus + 标量字段过滤（角色/部门） |
| 检索 | 向量 + BM25 + RRF | + 重排模型（bge-reranker）、top_k/阈值/相似度可调 M-A-05 |
| 生成 | 无（止于检索与引用溯源） | LLM 流式生成 + 引用知识点 P-QA-04 |
| 审核 | frontmatter status 门禁 | 管理后台审核流 M-K-05 |

## 已知限制

- 扫描件 PDF（图片型）无 OCR，抽取不到文本，需生产环境 MinerU/DeepDoc 处理
- xlsx 无 frontmatter，dept/ktype 依目录推断，visible_roles 默认"全部"
- store.json 为单文件全量加载，切片量上万后需换 Milvus
