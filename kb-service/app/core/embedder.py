"""向量化（任务 1.4）。

严格对齐 kb-engine/src/embed/embedder.js 的语义：
    - 模型：Xenova/bge-small-zh-v1.5（ONNX，fp32），BERT 架构，hidden_size=512
    - 池化：attention_mask 加权的 mean pooling，再做 L2 归一化
    - 维度：512

与 Node 版的差异（有意为之）：
    - Node 版依赖 @huggingface/transformers 在线拉取；本实现**离线优先**，
      按「显式目录 → 服务自带 models/ → 环境变量 → Node 版缓存 → HF 镜像下载」
      的顺序解析模型，适配 Windows Server 离线交付。
    - 当模型完全不可用时，降级为**确定性哈希向量**（backend="hash"），
      保证链路可跑通、可回归；该状态会显式暴露（is_fallback=True），
      绝不在日志或健康检查中伪装成真实模型。
"""

from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path

import numpy as np

from app.config import settings
from app.core.model_dir import candidate_dirs, onnx_file, valid_dir

logger = logging.getLogger(__name__)

# BGE-small-zh-v1.5 的最大位置编码长度
MAX_LEN = 512
_PAD_ID = 0
_PAD_TOKEN = "[PAD]"


class Embedder:
    """BGE 中文嵌入（ONNX 推理），单例复用。"""

    def __init__(
        self,
        model_name: str | None = None,
        dim: int | None = None,
        model_dir: str | Path | None = None,
    ) -> None:
        self.model_name = model_name or settings.embed_model
        self.dim = dim or settings.embed_dim
        self._model_dir_override = Path(model_dir) if model_dir else None

        self._session = None
        self._tokenizer = None
        self._backend: str | None = None  # "onnx" | "hash"
        self._resolved_dir: Path | None = None
        self._load_error: str | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 路径解析
    def _candidate_dirs(self) -> list[Path]:
        """按优先级列出可能的模型目录（公共实现见 model_dir.py）。"""
        return candidate_dirs(
            self.model_name, self._model_dir_override, extra_env=("KB_EMBED_DIR",)
        )

    @staticmethod
    def _onnx_file(d: Path) -> Path | None:
        return onnx_file(d)

    @classmethod
    def _valid_dir(cls, d: Path) -> bool:
        return valid_dir(d)

    def _download(self) -> Path:
        """从 HF 镜像下载模型到服务自带目录。"""
        from huggingface_hub import snapshot_download

        dest = settings.model_dir / self.model_name.split("/")[-1]
        dest.mkdir(parents=True, exist_ok=True)
        logger.info("从 %s 下载嵌入模型 %s …", settings.hf_endpoint, self.model_name)
        snapshot_download(
            repo_id=self.model_name,
            local_dir=str(dest),
            endpoint=settings.hf_endpoint or None,
            allow_patterns=[
                "onnx/*.onnx",
                "tokenizer.json",
                "tokenizer_config.json",
                "config.json",
                "vocab.txt",
                "special_tokens_map.json",
            ],
        )
        return dest

    # ------------------------------------------------------------------ 加载
    def load(self) -> None:
        """惰性加载模型（线程安全，只执行一次）。"""
        with self._lock:
            if self._backend is not None:
                return

            target = next((c for c in self._candidate_dirs() if self._valid_dir(c)), None)
            if target is None:
                try:
                    target = self._download()
                except Exception as exc:  # noqa: BLE001 - 降级前需吞掉所有加载异常
                    self._load_error = f"模型下载失败：{exc}"
                    target = None

            if target is not None:
                try:
                    self._load_onnx(target)
                    self._backend = "onnx"
                    self._resolved_dir = target
                    logger.info("嵌入模型已加载（onnx）：%s", target)
                    return
                except Exception as exc:  # noqa: BLE001
                    self._load_error = f"ONNX 加载失败：{exc}"
                    logger.warning("ONNX 加载失败，降级哈希向量：%s", exc)
            elif self._load_error is None:
                self._load_error = "未找到可用的本地模型目录"

            self._backend = "hash"
            logger.warning(
                "嵌入模型不可用（%s），使用确定性哈希向量降级；"
                "backend=hash，仅保证链路可跑通，检索质量不可用于生产评估。",
                self._load_error,
            )

    def _load_onnx(self, model_dir: Path) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        onnx_file = self._onnx_file(model_dir)
        assert onnx_file is not None

        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        opts.intra_op_num_threads = 0  # 由 onnxruntime 自行决定
        self._session = ort.InferenceSession(
            str(onnx_file),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )

        tok = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        tok.enable_truncation(max_length=MAX_LEN)
        tok.enable_padding(pad_id=_PAD_ID, pad_token=_PAD_TOKEN)
        self._tokenizer = tok

    # ------------------------------------------------------------------ 编码
    def encode(self, texts: list[str], batch_size: int = 16) -> list[list[float]]:
        """批量编码，返回 512 维 L2 归一化向量。"""
        if self._backend is None:
            self.load()
        if not texts:
            return []

        if self._backend == "hash":
            return [self._hash_vector(t) for t in texts]

        out: list[list[float]] = []
        total = len(texts)
        for i in range(0, total, batch_size):
            # 与 Node 版保持一致：不修改原文（空串交由 tokenizer 处理）
            batch = list(texts[i : i + batch_size])
            encs = self._tokenizer.encode_batch(batch)
            ids = np.asarray([e.ids for e in encs], dtype=np.int64)
            mask = np.asarray([e.attention_mask for e in encs], dtype=np.int64)
            types = np.asarray([e.type_ids for e in encs], dtype=np.int64)
            (hidden,) = self._session.run(
                ["last_hidden_state"],
                {"input_ids": ids, "attention_mask": mask, "token_type_ids": types},
            )
            out.extend(self._pool(hidden, mask))
            if total > batch_size:
                logger.info("  向量化进度 %d/%d", min(i + batch_size, total), total)
        return out

    def encode_one(self, text: str) -> list[float]:
        return self.encode([text])[0]

    @staticmethod
    def _pool(hidden: np.ndarray, mask: np.ndarray) -> list[list[float]]:
        """attention_mask 加权 mean pooling + L2 归一化。"""
        m = mask[..., None].astype(np.float32)
        summed = (hidden * m).sum(axis=1)
        counts = np.clip(m.sum(axis=1), 1e-9, None)
        pooled = summed / counts
        norm = np.linalg.norm(pooled, axis=1, keepdims=True)
        pooled = pooled / np.clip(norm, 1e-12, None)
        return pooled.astype(np.float32).tolist()

    # ------------------------------------------------------------------ 降级实现
    def _hash_vector(self, text: str) -> list[float]:
        """确定性哈希向量：字符 bigram → 512 维 → L2 归一化。

        仅用于模型缺失时保持链路可测；相同文本必然得到相同向量。
        """
        v = np.zeros(self.dim, dtype=np.float32)
        t = text.strip() or " "
        grams = [t[i : i + 2] for i in range(len(t) - 1)] or [t]
        for g in grams:
            h = int.from_bytes(hashlib.blake2b(g.encode("utf-8"), digest_size=8).digest(), "big")
            v[h % self.dim] += 1.0
        n = float(np.linalg.norm(v))
        if n > 0:
            v = v / n
        return v.astype(np.float32).tolist()

    # ------------------------------------------------------------------ 自省
    @property
    def backend(self) -> str:
        if self._backend is None:
            self.load()
        return self._backend or "hash"

    @property
    def loaded_backend(self) -> str | None:
        """已加载的后端；未加载返回 None（供健康检查使用，不触发模型加载）。"""
        return self._backend

    @property
    def is_fallback(self) -> bool:
        return self.backend == "hash"

    def status(self) -> dict:
        return {
            "model": self.model_name,
            "dim": self.dim,
            "backend": self.backend,
            "is_fallback": self.is_fallback,
            "model_dir": str(self._resolved_dir) if self._resolved_dir else None,
            "error": self._load_error,
        }


_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = Embedder()
    return _embedder


def reset_embedder() -> None:
    """仅供测试：清空单例。"""
    global _embedder
    _embedder = None
