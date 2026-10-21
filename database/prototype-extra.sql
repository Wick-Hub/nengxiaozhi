-- =====================================================================
-- "能小智"原型扩展表(kb-engine 本地原型专用,不属于生产 schema 交付物)
-- 生产环境:向量存 Milvus 集合 kb_chunk_vec(HNSW,§5.3),MySQL 只存元数据;
-- 原型环境:单机演示,在 MySQL 内以 JSON 列模拟向量集合,可随时 TRUNCATE 重建
-- =====================================================================
USE nengxiaozhi;

CREATE TABLE IF NOT EXISTS kb_chunk_vec_proto (
  chunk_id   VARCHAR(64) PRIMARY KEY COMMENT '切片ID(对应 kb_chunk.chunk_id)',
  embedding  JSON NOT NULL COMMENT '向量数组(原型 512 维 bge-small-zh-v1.5)',
  dim        INT NOT NULL DEFAULT 512 COMMENT '向量维度',
  model      VARCHAR(128) NOT NULL DEFAULT '' COMMENT '嵌入模型标识',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) COMMENT='切片向量(原型模拟 Milvus kb_chunk_vec)';
