"""精排层（任务 1.6）。

严格对齐 kb-engine/src/retrieve/rerank.js：
    - 模型：onnx-community/bge-reranker-v2-m3-ONNX（q8 量化，与 Node 版 dtype 一致）
    - 交叉编码打分：``tokenizer(query, text_pair=chunk.text, truncation, max_length=512)``
      → ``logits[batch, 1]`` 作为相关性分数
    - **候选数 ≤ topK 时直接返回原序**（与原型一致，避免无谓推理）
    - **加载或推理失败自动降级为原序前 topK**，不阻断主链路

离线优先加载，路径解析见 ``model_dir.py``；模型缺失时降级而非报错。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

from app.config import settings
from app.core.model_dir import candidate_dirs, onnx_file, valid_dir
from app.core.retriever import Retrieved

logger = logging.getLogger(__name__)

MAX_LEN = 512


class Reranker:
    """BGE 交叉编码精排（ONNX 推理），单例复用。"""

    def __init__(self, model_name: str | None = None, model_dir: str | Path | None = None) -> None:
        self.model_name = model_name or settings.rerank_model
        self._model_dir_override = Path(model_dir) if model_dir else None

        self._session = None
        self._tokenizer = None
        self._ready = False
        self._failed = False
        self._resolved_dir: Path | None = None
        self._error: str | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 属性
    @property
    def enabled(self) -> bool:
        return settings.rerank_enabled

    @property
    def available(self) -> bool:
        """精排是否真的可用（已成功加载）。不触发加载。"""
        return self._ready

    # ------------------------------------------------------------------ 加载
    def _candidate_dirs(self) -> list[Path]:
        return candidate_dirs(
            self.model_name, self._model_dir_override, extra_env=("KB_RERANK_DIR",)
        )

    def load(self) -> bool:
        """加载模型；成功返回 True，失败返回 False（且不再重试）。"""
        if self._ready:
            return True
        if self._failed:
            return False
        with self._lock:
            if self._ready:
                return True
            if self._failed:
                return False

            target = next((c for c in self._candidate_dirs() if valid_dir(c, prefer_quantized=True)), None)
            if target is None:
                self._failed = True
                self._error = "未找到可用的本地精排模型目录"
                logger.warning("精排模型不可用：%s", self._error)
                return False
            try:
                self._load_onnx(target)
            except Exception as exc:  # noqa: BLE001 - 降级前需吞掉所有加载异常
                self._failed = True
                self._error = f"ONNX 加载失败：{exc}"
                logger.warning("精排模型加载失败，降级为不精排：%s", exc)
                return False

            self._ready = True
            self._resolved_dir = target
            logger.info("精排模型已加载（onnx，量化）：%s", target)
            return True

    def _load_onnx(self, model_dir: Path) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        weight = onnx_file(model_dir, prefer_quantized=True)
        assert weight is not None

        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(
            str(weight), sess_options=opts, providers=["CPUExecutionProvider"]
        )

        tok = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        tok.enable_truncation(max_length=MAX_LEN)
        self._tokenizer = tok

    # ------------------------------------------------------------------ 打分
    def score(self, query: str, texts: list[str]) -> list[float]:
        """逐条交叉编码打分，返回 logits。

        **固定 batch=1、不做 padding**：该量化（q8）模型对 padding 敏感，
        实测批处理会使分数最大偏差 0.59，足以翻转排序。原型 rerank.js 亦为
        逐条推理（``for (const it of items)``），此处保持一致。
        """
        if not self._ready:
            raise RuntimeError("精排模型未加载")
        scores: list[float] = []
        for t in texts:
            enc = self._tokenizer.encode(query, t)
            ids = np.asarray([enc.ids], dtype=np.int64)
            mask = np.asarray([enc.attention_mask], dtype=np.int64)
            (logits,) = self._session.run(
                ["logits"], {"input_ids": ids, "attention_mask": mask}
            )
            scores.append(float(logits[0, 0]))
        return scores

    # ------------------------------------------------------------------ 精排
    def rerank(
        self,
        query: str,
        candidates: list[Retrieved],
        top_k: int | None = None,
    ) -> list[Retrieved]:
        """对召回候选精排并取前 top_k。

        与原型一致：
            - 未启用或候选数 ≤ top_k → 原序返回（不截断）
            - 模型不可用或推理异常 → 原序前 top_k
        """
        top_k = top_k or settings.top_k
        if not self.enabled or len(candidates) <= top_k:
            return candidates
        if not self.load():
            return candidates[:top_k]
        try:
            scores = self.score(query, [c.chunk.text for c in candidates])
        except Exception as exc:  # noqa: BLE001 - 降级为不精排
            logger.warning("精排推理失败，降级为不精排：%s", exc)
            return candidates[:top_k]

        order = sorted(range(len(candidates)), key=lambda i: -scores[i])
        out: list[Retrieved] = []
        for i in order[:top_k]:
            item = candidates[i]
            item.extra["rerank"] = float(scores[i])
            out.append(item)
        return out

    # ------------------------------------------------------------------ 自省
    def status(self) -> dict:
        return {
            "model": self.model_name,
            "enabled": self.enabled,
            "available": self.available,
            "model_dir": str(self._resolved_dir) if self._resolved_dir else None,
            "error": self._error,
        }


_reranker: Reranker | None = None


def get_reranker() -> Reranker:
    global _reranker
    if _reranker is None:
        _reranker = Reranker()
    return _reranker


def reset_reranker() -> None:
    """仅供测试：清空单例。"""
    global _reranker
    _reranker = None
