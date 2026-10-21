"""性能与容量验证（任务 5.3）。

    python tools/perf_load.py --all                 # 检索微基准 + HTTP 并发 + 报告
    python tools/perf_load.py --micro-only          # 只测嵌入/召回/精排
    python tools/perf_load.py --http-only --base-url http://127.0.0.1:8011
    python tools/perf_load.py --stages 1,4,8,16 --duration 8 --skip-scale

测什么：

* **检索链**：嵌入、混合召回（向量 + BM25 + RRF）、精排、完整 retrieve，
  并用克隆切片模拟 350 / 3500 / 10000 规模，观察线性成本。
* **HTTP**：非流式 ``/api/chat`` 与流式 ``/api/chat/stream``，闭环并发分级；
  流式单独统计**首 token**（首个 delta 事件）与全程耗时。
* **资源**：服务进程 RSS / CPU（Windows psapi、Linux /proc），主机可用内存。

默认以 ``LLM_MOCK=1``、``AUTH_ENABLED=false``、``RATE_LIMIT_ENABLED=false``
启动独立实例（端口 8099），测完自动停止；使用 MySQL 时按前缀清理压测产生的
会话/消息/引用/计量，幂等可重复。所有结论写入 ``artifacts/perf-report.md``。
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import math
import os
import platform
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

ARTIFACTS = SERVICE_DIR / "artifacts"
QUESTIONS = [
    "一卡通丢了怎么办？",
    "图书馆开放时间是什么？",
    "宿舍空调怎么使用？",
    "如何申请奖学金？",
    "补办学生证需要哪些材料？",
    "校园网怎么连接？",
    "毕业生档案如何转递？",
    "食堂营业时间？",
    "教室借用流程？",
    "校医院就诊时间？",
]

TARGET_P95_MS = 3000  # token_usage.first_token_ms 的设计目标：P95 < 3s
TARGET_ERROR_RATE = 0.01


# --------------------------------------------------------------------- 统计
def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, math.ceil(p / 100 * len(ordered)) - 1))
    return float(ordered[idx])


def summarize(values: list[float], digits: int = 2) -> dict:
    if not values:
        return {"n": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), digits),
        "p50": round(percentile(values, 50), digits),
        "p95": round(percentile(values, 95), digits),
        "p99": round(percentile(values, 99), digits),
        "max": round(max(values), digits),
    }


# --------------------------------------------------------------------- 资源采样
@dataclass
class HostInfo:
    platform: str
    python: str
    cpu_count: int
    ram_total_mb: float
    store: str
    processor: str


def host_info(store: str) -> HostInfo:
    total_mb = 0.0
    processor = os.environ.get("PROCESSOR_IDENTIFIER", "")
    try:
        if sys.platform == "win32":
            total_mb = _win_host_memory_mb()
        elif Path("/proc/meminfo").exists():
            text = Path("/proc/meminfo").read_text(encoding="utf-8")
            for line in text.splitlines():
                if line.startswith("MemTotal:"):
                    total_mb = int(line.split()[1]) / 1024
                    break
    except Exception:  # noqa: BLE001 - 资源信息尽力而为
        total_mb = 0.0
    return HostInfo(
        platform=f"{sys.platform} {platform.release()}",
        python=sys.version.split()[0],
        cpu_count=os.cpu_count() or 0,
        ram_total_mb=round(total_mb, 1),
        store=store,
        processor=processor,
    )


def _win_host_memory_mb() -> float:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(stat)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return 0.0
    return stat.ullTotalPhys / 1048576


def _win_process_metrics(pid: int):
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(
            ctypes.c_void_p(handle), ctypes.byref(counters), counters.cb
        ):
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(
            ctypes.c_void_p(handle),
            ctypes.byref(times[0]),
            ctypes.byref(times[1]),
            ctypes.byref(times[2]),
            ctypes.byref(times[3]),
        ):
            return None

        def ft(v):
            return (v.dwHighDateTime << 32) | v.dwLowDateTime

        cpu_s = (ft(times[2]) + ft(times[3])) / 1e7
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(stat)
        avail = 0.0
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            avail = stat.ullAvailPhys / 1048576
        return counters.WorkingSetSize / 1048576, cpu_s, avail
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def _linux_process_metrics(pid: int):
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
        rss_kb = next(int(x.split()[1]) for x in status.splitlines() if x.startswith("VmRSS:"))
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        ticks = os.sysconf("SC_CLK_TCK")
        cpu_s = (int(stat[13]) + int(stat[14])) / ticks
        meminfo = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
        avail_kb = next(int(x.split()[1]) for x in meminfo if x.startswith("MemAvailable:"))
        return rss_kb / 1024, cpu_s, avail_kb / 1024
    except Exception:  # noqa: BLE001
        return None


def process_metrics(pid: int):
    if sys.platform == "win32":
        return _win_process_metrics(pid)
    return _linux_process_metrics(pid)


def process_cpu_seconds(pid: int) -> float | None:
    metrics = process_metrics(pid)
    return metrics[1] if metrics else None


def system_cpu_snapshot():
    """返回 (idle, total) 累计值；用于计算整机 CPU 占用。"""
    try:
        if sys.platform == "win32":
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            times = [wintypes.FILETIME() for _ in range(3)]
            if not kernel32.GetSystemTimes(
                ctypes.byref(times[0]), ctypes.byref(times[1]), ctypes.byref(times[2])
            ):
                return None

            def ft(v):
                return (v.dwHighDateTime << 32) | v.dwLowDateTime

            idle = ft(times[0])
            return idle, ft(times[1]) + ft(times[2])
        line = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()
        values = [int(x) for x in line[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return idle, sum(values)
    except Exception:  # noqa: BLE001
        return None


def find_listener_pid(port: int) -> int | None:
    """按监听端口定位真正的服务进程（Windows 上启动器可能不是工作进程）。"""
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            ).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
                    return int(parts[4])
        else:
            out = subprocess.run(
                ["ss", "-ltnp"], capture_output=True, text=True, timeout=10, check=False
            ).stdout
            for line in out.splitlines():
                if f":{port}" in line:
                    match = re.search(r"pid=(\d+)", line)
                    if match:
                        return int(match.group(1))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return None


@dataclass
class ResourceSample:
    ts: float
    rss_mb: float
    cpu_s: float
    host_avail_mb: float
    host_cpu_pct: float = 0.0


class ResourceSampler(threading.Thread):
    """每秒采样一次服务进程；跨平台，失败时静默降级为空样本。"""

    def __init__(self, pid: int, interval: float = 1.0) -> None:
        super().__init__(daemon=True)
        self.pid = pid
        self.interval = interval
        self.samples: list[ResourceSample] = []
        self._stop_event = threading.Event()
        self._sys_prev = system_cpu_snapshot()

    def _host_cpu(self) -> float:
        curr = system_cpu_snapshot()
        prev, self._sys_prev = self._sys_prev, curr
        if not prev or not curr:
            return 0.0
        idle = curr[0] - prev[0]
        total = curr[1] - prev[1]
        return max(0.0, min(100.0, 100.0 * (1 - idle / total))) if total > 0 else 0.0

    def run(self) -> None:
        while not self._stop_event.wait(self.interval):
            metrics = process_metrics(self.pid)
            if metrics:
                rss, cpu_s, avail = metrics
                self.samples.append(
                    ResourceSample(
                        time.monotonic(), rss, cpu_s, avail, self._host_cpu()
                    )
                )

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=3)

    def summary(self) -> dict:
        if not self.samples:
            return {"available": False}
        first, last = self.samples[0], self.samples[-1]
        span = max(0.001, last.ts - first.ts)
        cores = max(0.0, (last.cpu_s - first.cpu_s) / span)
        rss = [s.rss_mb for s in self.samples]
        return {
            "available": True,
            "cpu_cores_avg": round(cores, 2),
            "cpu_cores_max": round(
                max(
                    (b.cpu_s - a.cpu_s) / max(0.001, b.ts - a.ts)
                    for a, b in zip(self.samples, self.samples[1:])
                )
                if len(self.samples) > 1
                else cores,
                2,
            ),
            "rss_mb_avg": round(statistics.fmean(rss), 1),
            "rss_mb_max": round(max(rss), 1),
            "host_cpu_pct_avg": round(
                statistics.fmean(s.host_cpu_pct for s in self.samples), 1
            ),
            "host_cpu_pct_max": round(max(s.host_cpu_pct for s in self.samples), 1),
            "host_avail_mb_min": round(min(s.host_avail_mb for s in self.samples), 1),
            "samples": len(self.samples),
        }


# --------------------------------------------------------------------- 检索微基准
@dataclass
class MicroResult:
    index: dict
    stages: dict = field(default_factory=dict)
    scale: list = field(default_factory=list)


def _warm_models() -> None:
    from app.core.embedder import get_embedder
    from app.core.reranker import get_reranker

    get_embedder().encode_one("预热")
    get_reranker().load()


def micro_benchmark(rounds: int, skip_scale: bool) -> MicroResult:
    from app.config import settings
    from app.core.embedder import get_embedder
    from app.core.index_store import get_index
    from app.core.reranker import get_reranker
    from app.core.retriever import hybrid_search

    snapshot = get_index()
    chunks = snapshot.chunks
    if snapshot.error:
        raise SystemExit(f"索引不可用：{snapshot.error}")
    if not chunks:
        raise SystemExit("索引为空，无法压测检索层")

    print(f"索引：{len(snapshot.docs)} 文档 / {len(chunks)} 切片（{settings.store_label}）")
    print("加载嵌入与精排模型 …")
    _warm_models()

    embedder = get_embedder()
    reranker = get_reranker()
    rerank_topn = max(settings.rerank_topn, settings.top_k)
    stage_values: dict[str, list[float]] = {
        "embed": [],
        "recall": [],
        "rerank": [],
        "retrieve_total": [],
        "retrieve_cpu_s": [],
    }

    for r in range(rounds):
        for q in QUESTIONS:
            q = f"{q}" if r == 0 else f"{q}（第{r + 1}轮）"
            cpu0 = process_cpu_seconds(os.getpid())
            t0 = time.perf_counter()
            vector = embedder.encode_one(q)
            t1 = time.perf_counter()
            candidates = hybrid_search(
                chunks, vector, q, role="本科生", top_n=rerank_topn
            )
            t2 = time.perf_counter()
            if settings.rerank_enabled and len(candidates) > settings.top_k:
                reranker.rerank(q, candidates, settings.top_k)
            t3 = time.perf_counter()
            cpu1 = process_cpu_seconds(os.getpid())
            stage_values["embed"].append((t1 - t0) * 1000)
            stage_values["recall"].append((t2 - t1) * 1000)
            stage_values["rerank"].append((t3 - t2) * 1000)
            stage_values["retrieve_total"].append((t3 - t0) * 1000)
            if cpu0 is not None and cpu1 is not None:
                stage_values["retrieve_cpu_s"].append(max(0.0, cpu1 - cpu0))

    stages = {
        k: summarize(v, digits=4 if k == "retrieve_cpu_s" else 2)
        for k, v in stage_values.items()
    }
    result = MicroResult(
        index={
            "docs": len(snapshot.docs),
            "chunks": len(chunks),
            "store": settings.store_label,
            "rerank_enabled": settings.rerank_enabled,
            "rerank_available": reranker.available,
            "top_k": settings.top_k,
            "rerank_topn": rerank_topn,
        },
        stages=stages,
    )
    print(
        "检索微基准："
        f"embed p95 {stages['embed']['p95']}ms · recall p95 {stages['recall']['p95']}ms · "
        f"rerank p95 {stages['rerank']['p95']}ms · full p95 {stages['retrieve_total']['p95']}ms"
        + (
            f" · CPU p50 {stages['retrieve_cpu_s']['p50']}s/次"
            if stages.get("retrieve_cpu_s", {}).get("n")
            else ""
        )
    )

    if skip_scale:
        return result

    print("规模模拟（克隆切片，观察召回成本随规模变化）…")
    plans = [(350, 10), (3500, 5), (10000, 3)]
    sizes = [len(chunks)] + [n for n, _ in plans if n > len(chunks)]
    iterations = {len(chunks): 15}
    iterations.update({n: it for n, it in plans})
    for size in sizes:
        pool = _clone_chunks(chunks, size)
        q = QUESTIONS[0]
        vector = embedder.encode_one(q)
        hybrid_search(pool, vector, q, role="本科生", top_n=rerank_topn)  # 预热
        samples = []
        for i in range(iterations.get(size, 5)):
            qq = q if i == 0 else f"{q}（规模{size}·{i}）"
            t0 = time.perf_counter()
            hybrid_search(pool, vector, qq, role="本科生", top_n=rerank_topn)
            samples.append((time.perf_counter() - t0) * 1000)
        row = {"chunks": size, "iterations": len(samples), **summarize(samples)}
        result.scale.append(row)
        print(f"  {size:>6} 切片 → p50 {row['p50']}ms / p95 {row['p95']}ms")
    return result


def _clone_chunks(chunks: list, size: int) -> list:
    from app.core.chunker import Chunk

    pool = list(chunks)
    i = 0
    while len(pool) < size:
        src = chunks[i % len(chunks)]
        pool.append(
            Chunk(
                chunk_id=f"{src.chunk_id}#{i // len(chunks)}",
                doc_id=src.doc_id,
                seq_no=src.seq_no,
                char_count=src.char_count,
                dept=src.dept,
                ktype=src.ktype,
                visible_roles=src.visible_roles,
                title_path=src.title_path,
                block_type=src.block_type,
                text=src.text,
                embed_text=src.embed_text,
                embedding=src.embedding,
            )
        )
        i += 1
    return pool


# --------------------------------------------------------------------- HTTP 压测
@dataclass
class RequestOutcome:
    ok: bool
    total_ms: float
    ttft_ms: float | None
    server_latency_ms: float | None
    status: int
    error: str = ""


async def _one_stream(client, url: str, payload: dict) -> RequestOutcome:
    t0 = time.perf_counter()
    ttft = None
    status = 0
    try:
        async with client.stream("POST", url, json=payload) as resp:
            status = resp.status_code
            if status != 200:
                await resp.aread()
                return RequestOutcome(
                    False,
                    (time.perf_counter() - t0) * 1000,
                    None,
                    None,
                    status,
                    f"HTTP {status}",
                )
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                try:
                    event = json.loads(body)
                except json.JSONDecodeError:
                    continue
                kind = event.get("type")
                if kind == "delta" and ttft is None:
                    ttft = (time.perf_counter() - t0) * 1000
                elif kind == "error":
                    return RequestOutcome(
                        False,
                        (time.perf_counter() - t0) * 1000,
                        ttft,
                        None,
                        status,
                        str(event.get("error", "SSE error")),
                    )
                elif kind == "done":
                    return RequestOutcome(
                        True,
                        (time.perf_counter() - t0) * 1000,
                        ttft,
                        _as_float(event.get("latencyMs")),
                        status,
                    )
        return RequestOutcome(
            False, (time.perf_counter() - t0) * 1000, ttft, None, status, "流在 done 前结束"
        )
    except Exception as exc:  # noqa: BLE001 - 网络异常计入错误率
        return RequestOutcome(
            False,
            (time.perf_counter() - t0) * 1000,
            ttft,
            None,
            status,
            f"{type(exc).__name__}: {exc}",
        )


async def _one_json(client, url: str, payload: dict) -> RequestOutcome:
    t0 = time.perf_counter()
    try:
        resp = await client.post(url, json=payload)
        body = resp.json()
        elapsed = (time.perf_counter() - t0) * 1000
        data = body.get("data") or {}
        ok = resp.status_code == 200 and bool(body.get("ok"))
        return RequestOutcome(
            ok,
            elapsed,
            None,
            _as_float(data.get("latencyMs")),
            resp.status_code,
            "" if ok else str(body.get("error") or f"HTTP {resp.status_code}"),
        )
    except Exception as exc:  # noqa: BLE001
        return RequestOutcome(
            False,
            (time.perf_counter() - t0) * 1000,
            None,
            None,
            0,
            f"{type(exc).__name__}: {exc}",
        )


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def _run_stage(
    base_url: str,
    concurrency: int,
    duration: float,
    stream: bool,
    prefix: str,
    role: str,
) -> dict:
    import httpx

    stop = time.monotonic() + duration
    outcomes: list[RequestOutcome] = []
    lock = asyncio.Lock()
    limits = httpx.Limits(max_connections=max(10, concurrency * 2), max_keepalive_connections=concurrency)
    timeout = httpx.Timeout(60.0, connect=10.0)
    url = f"{base_url}/api/chat/stream" if stream else f"{base_url}/api/chat"

    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        async def worker(index: int) -> None:
            session_id = f"{prefix}-{index:03d}"
            n = 0
            while time.monotonic() < stop:
                question = QUESTIONS[n % len(QUESTIONS)]
                payload = {"question": question, "role": role, "session_id": session_id}
                if stream:
                    outcome = await _one_stream(client, url, payload)
                else:
                    outcome = await _one_json(client, url, payload)
                async with lock:
                    outcomes.append(outcome)
                n += 1

        await asyncio.gather(*(worker(i) for i in range(concurrency)))

    total = len(outcomes)
    errors = [o for o in outcomes if not o.ok]
    elapsed = duration
    totals = [o.total_ms for o in outcomes]
    ttfts = [o.ttft_ms for o in outcomes if o.ttft_ms is not None and o.ok]
    server = [o.server_latency_ms for o in outcomes if o.server_latency_ms is not None]
    return {
        "concurrency": concurrency,
        "stream": stream,
        "duration_s": round(elapsed, 1),
        "requests": total,
        "rps": round(total / elapsed, 2) if elapsed else 0.0,
        "error_count": len(errors),
        "error_rate": round(len(errors) / total, 4) if total else 1.0,
        "errors_top": _top_errors(errors),
        "total": summarize(totals),
        "ttft": summarize(ttfts) if stream else None,
        "server_llm_latency": summarize(server) if server else None,
    }


def _top_errors(errors: list[RequestOutcome], limit: int = 3) -> list[str]:
    counts: dict[str, int] = {}
    for e in errors:
        key = (e.error or f"HTTP {e.status}")[:160]
        counts[key] = counts.get(key, 0) + 1
    return [f"{k} ×{v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:limit]]


def _health(base_url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url}/api/health", timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _bind_affinity(pid: int, cores: int) -> None:
    """把压测服务绑到前 N 个逻辑核，近似目标机 CPU 规格；失败即中止。"""
    try:
        if sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.restype = ctypes.c_void_p
            handle = kernel32.OpenProcess(0x0200 | 0x1000, False, pid)
            if not handle:
                raise OSError("OpenProcess 失败")
            try:
                mask = (1 << cores) - 1
                if not kernel32.SetProcessAffinityMask(ctypes.c_void_p(handle), ctypes.c_size_t(mask)):
                    raise OSError("SetProcessAffinityMask 失败")
            finally:
                kernel32.CloseHandle(ctypes.c_void_p(handle))
        else:
            os.sched_setaffinity(pid, list(range(cores)))
        print(f"已将服务进程绑定到前 {cores} 个逻辑核")
    except Exception as exc:
        raise SystemExit(f"绑定 {cores} 核失败（{exc}）；如只需全机数据请去掉 --affinity") from exc


def start_server(port: int, log_path: Path, affinity: int = 0) -> subprocess.Popen:
    env = {
        **os.environ,
        "LLM_MOCK": "1",
        "AUTH_ENABLED": "false",
        "RATE_LIMIT_ENABLED": "false",
        "PYTHONIOENCODING": "utf-8",
    }
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("wb")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
            "--no-access-log",
        ],
        cwd=SERVICE_DIR,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    if affinity > 0:
        _bind_affinity(proc.pid, affinity)
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"服务启动失败，见 {log_path}")
        if _health(base_url):
            return proc
        time.sleep(0.5)
    proc.terminate()
    raise SystemExit(f"服务 90s 内未就绪，见 {log_path}")


def cleanup_mysql(prefix: str) -> dict:
    from sqlalchemy import delete, select

    from app.config import settings
    from app.db import models as M
    from app.db.session import db_transaction

    if settings.kb_store.lower() != "mysql":
        return {"skipped": "json"}
    like = f"{prefix}-%"
    with db_transaction() as conn:
        rows = conn.execute(
            select(M.ChatMessage.msg_id, M.ChatMessage.trace_id).where(
                M.ChatMessage.session_id.like(like)
            )
        ).all()
        msg_ids = [int(r[0]) for r in rows]
        trace_ids = [str(r[1]) for r in rows if r[1]]
        citations = 0
        if msg_ids:
            citations = conn.execute(
                delete(M.AnswerCitation).where(M.AnswerCitation.msg_id.in_(msg_ids))
            ).rowcount or 0
        tokens = 0
        if trace_ids:
            tokens = conn.execute(
                delete(M.TokenUsage).where(M.TokenUsage.trace_id.in_(trace_ids))
            ).rowcount or 0
        msgs = conn.execute(
            delete(M.ChatMessage).where(M.ChatMessage.session_id.like(like))
        ).rowcount or 0
        sessions = conn.execute(
            delete(M.ChatSession).where(M.ChatSession.session_id.like(like))
        ).rowcount or 0
        unrecognized = conn.execute(
            delete(M.UnrecognizedQuestion).where(M.UnrecognizedQuestion.session_id.like(like))
        ).rowcount or 0
    return {
        "messages": msgs,
        "sessions": sessions,
        "citations": citations,
        "token_usage": tokens,
        "unrecognized": unrecognized,
    }


async def http_benchmark(
    base_url: str, stages: list[int], duration: float, role: str, pid: int | None
) -> list[dict]:
    import httpx

    results: list[dict] = []
    prefix = f"perf-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    if pid:
        sampler = ResourceSampler(pid)
        sampler.start()
    else:
        sampler = None
    try:
        async with httpx.AsyncClient(timeout=60.0) as warm:
            for _ in range(2):
                await _one_json(
                    warm,
                    f"{base_url}/api/chat",
                    {"question": QUESTIONS[0], "role": role, "session_id": f"{prefix}-warm"},
                )
            await _one_stream(
                warm,
                f"{base_url}/api/chat/stream",
                {"question": QUESTIONS[0], "role": role, "session_id": f"{prefix}-warm"},
            )
        for stream in (False, True):
            kind = "流式" if stream else "非流式"
            for concurrency in stages:
                print(f"  {kind} · {concurrency} 并发 · {duration:.0f}s …", flush=True)
                stage_sampler = ResourceSampler(pid) if pid else None
                if stage_sampler:
                    stage_sampler.start()
                row = await _run_stage(
                    base_url, concurrency, duration, stream, prefix, role
                )
                if stage_sampler:
                    stage_sampler.stop()
                    row["resources"] = stage_sampler.summary()
                else:
                    row["resources"] = {"available": False}
                results.append(row)
                print(
                    f"    {row['requests']} 请求 · {row['rps']} RPS · "
                    f"错误 {row['error_rate'] * 100:.2f}% · "
                    f"p95 {row['total']['p95']}ms"
                    + (
                        f" · 首 token p95 {row['ttft']['p95']}ms"
                        if stream and row.get("ttft")
                        else ""
                    ),
                    flush=True,
                )
    finally:
        if sampler:
            sampler.stop()
        cleanup = cleanup_mysql(prefix)
        print(f"  压测数据清理：{cleanup}")
    return results


# --------------------------------------------------------------------- 报告
def build_report(
    host: HostInfo,
    micro: MicroResult | None,
    http_rows: list[dict],
    args,
) -> tuple[dict, str]:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report = {
        "generated_at": stamp,
        "host": asdict(host),
        "config": {
            "llm": "mock",
            "auth": "off",
            "rate_limit": "off",
            "stages": args.stages,
            "duration_s": args.duration,
            "role": args.role,
            "affinity": args.affinity,
        },
        "micro": asdict(micro) if micro else None,
        "http": http_rows,
    }
    report["verdict"] = _verdict(http_rows, args.target_concurrency)
    return report, _render_markdown(report)


def _verdict(rows: list[dict], target: int) -> dict:
    target_rows = [r for r in rows if r["concurrency"] == target]
    if not target_rows:
        return {"passed": None, "reason": f"未测试 {target} 并发"}
    worst = max(target_rows, key=lambda r: r["error_rate"])
    p95s = []
    for r in target_rows:
        if r.get("ttft"):
            p95s.append(r["ttft"]["p95"])
        else:
            p95s.append(r["total"]["p95"])
    p95 = max(p95s) if p95s else 0.0
    passed = worst["error_rate"] <= TARGET_ERROR_RATE and p95 <= TARGET_P95_MS
    return {
        "passed": passed,
        "target_concurrency": target,
        "error_rate": worst["error_rate"],
        "p95_ms": p95,
        "thresholds": {"error_rate": TARGET_ERROR_RATE, "p95_ms": TARGET_P95_MS},
    }


def _capacity(
    rows: list[dict], cpu_count: int, target: int, cores_limit: int = 8, affinity: int = 0
) -> dict:
    """按「每请求 CPU 秒」估算 8C16G 容量，预留 40% CPU 余量。

    用 CPU 时间而不是墙钟吞吐做主依据：共享开发机上的其他进程会拉长时延，
    但一次请求消耗的 CPU 秒相对稳定。结果再以实测 RPS 封顶，避免外推超过
    压测实际跑出的处理能力（连接池、GIL、MySQL 写入都会先于 CPU 见顶）。
    """
    usable = [
        r
        for r in rows
        if r.get("resources", {}).get("available")
        and r["rps"] > 0
        and r["resources"].get("cpu_cores_avg", 0) > 0
    ]
    candidates = [
        r
        for r in usable
        if r["error_rate"] <= TARGET_ERROR_RATE
        and (r["ttft"]["p95"] if r.get("ttft") else r["total"]["p95"]) <= TARGET_P95_MS
    ]
    pool = candidates or usable
    if not pool:
        return {"available": False}
    best = min(
        pool,
        key=lambda r: r["resources"]["cpu_cores_avg"] / max(r["rps"], 0.01),
    )
    cores = best["resources"]["cpu_cores_avg"]
    rps = best["rps"]
    cpu_s_per_req = cores / rps
    raw_qps = (cores_limit * 0.6) / cpu_s_per_req
    qps = round(min(raw_qps, rps), 1)
    mem = max(  # 各档单独采样，取全轮峰值而不是基准档，避免低报内存
        r["resources"].get("rss_mb_max", 0) for r in pool
    ) or best["resources"].get("rss_mb_max", 0)
    return {
        "available": True,
        "basis": {
            "concurrency": best["concurrency"],
            "stream": best["stream"],
            "rps": rps,
            "cpu_cores_avg": cores,
            "cpu_s_per_request": round(cpu_s_per_req, 4),
            "host_cpu_pct_avg": best["resources"].get("host_cpu_pct_avg"),
            "rss_mb_max": mem,
            "host_cpu_count": cpu_count,
            "met_target": bool(candidates),
        },
        "cpu_headroom_60pct": True,
        "cores_limit": cores_limit,
        "affinity": affinity,
        "raw_cpu_qps": round(raw_qps, 1),
        "estimated_qps_8c": qps,
        "rss_mb_peak": mem,
        "estimated_daily_4h_peak": round(qps * 3600 * 4 * 0.2),
        "memory_note": (
            f"单进程峰值 RSS {mem} MB；16GB 单机内存可容纳 "
            f"{max(1, int(16 * 1024 * 0.7 / max(mem, 1)))} 个同等进程（CPU 先成为瓶颈）"
        ),
    }


def _render_markdown(report: dict) -> str:
    host = report["host"]
    lines = [
        "# 性能与容量验证报告（任务 5.3）",
        "",
        (
            f"> 生成时间：{report['generated_at']} ｜ 场景：LLM mock、鉴权关闭、限流关闭、"
            f"角色 {report['config']['role']}"
        ),
        (
            f"> 主机：{host['platform']} ｜ {host['cpu_count']} 逻辑核 ｜ "
            f"{host['ram_total_mb']:.0f} MB 内存 ｜ Python {host['python']} ｜ 存储 {host['store']}"
        ),
        "",
        "## 1. 检索链微基准",
        "",
    ]
    micro = report.get("micro")
    if micro:
        idx = micro["index"]
        lines += [
            (
                f"索引规模：**{idx['docs']} 文档 / {idx['chunks']} 切片**；"
                f"精排 {'启用' if idx['rerank_enabled'] else '关闭'}"
                f"（可用={idx['rerank_available']}）；topK={idx['top_k']}，召回={idx['rerank_topn']}。"
            ),
            "",
            "| 阶段 | 样本 | 均值 | P50 | P95 | P99 | 最大 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        names = {
            "embed": "嵌入（ONNX）",
            "recall": "混合召回（向量+BM25+RRF）",
            "rerank": "精排（BGE cross-encoder）",
            "retrieve_total": "完整 retrieve",
            "retrieve_cpu_s": "完整 retrieve（CPU 秒）",
        }
        for key, label in names.items():
            s = micro["stages"].get(key)
            if not s or not s.get("n"):
                continue
            unit = "s" if key == "retrieve_cpu_s" else "ms"
            lines.append(
                f"| {label} | {s['n']} | {s['mean']}{unit} | {s['p50']}{unit} | "
                f"{s['p95']}{unit} | {s['p99']}{unit} | {s['max']}{unit} |"
            )
        if micro.get("scale"):
            lines += [
                "",
                "### 规模模拟（克隆切片）",
                "",
                "| 切片数 | 迭代 | P50 | P95 | 最大 |",
                "| ---: | ---: | ---: | ---: | ---: |",
            ]
            for row in micro["scale"]:
                lines.append(
                    f"| {row['chunks']} | {row['iterations']} | {row['p50']}ms | "
                    f"{row['p95']}ms | {row['max']}ms |"
                )
            lines.append("")
            lines.append(
                "> 规模模拟只放大召回成本，不改变精排候选数（固定 topN）；"
                "真实扩容后需重新实测。"
            )
    else:
        lines.append("（本轮跳过检索微基准）")

    lines += ["", "## 2. HTTP 并发", ""]
    if report["http"]:
        lines += [
            "| 模式 | 并发 | 请求数 | RPS | 错误率 | 总耗时 P50 | P95 | P99 | 首 token P50 | P95 | 进程 CPU(核) | 主机 CPU | RSS 峰值 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for r in report["http"]:
            ttft = r.get("ttft") or {}
            res = r.get("resources") or {}
            lines.append(
                f"| {'流式' if r['stream'] else '非流式'} | {r['concurrency']} | "
                f"{r['requests']} | {r['rps']} | {r['error_rate'] * 100:.2f}% | "
                f"{r['total']['p50']}ms | {r['total']['p95']}ms | {r['total']['p99']}ms | "
                f"{ttft.get('p50', '—')}{'ms' if ttft else ''} | "
                f"{ttft.get('p95', '—')}{'ms' if ttft else ''} | "
                f"{res.get('cpu_cores_avg', '—')} | "
                f"{res.get('host_cpu_pct_avg', '—')}{'%' if res.get('host_cpu_pct_avg') is not None else ''} | "
                f"{res.get('rss_mb_max', '—')} MB |"
            )
        errors = [e for r in report["http"] for e in r.get("errors_top", [])]
        if errors:
            lines += ["", "错误样本：" + "；".join(errors[:5])]
    else:
        lines.append("（本轮跳过 HTTP 压测）")

    verdict = report["verdict"]
    lines += ["", "## 3. 验收判定", ""]
    if verdict.get("passed") is None:
        lines.append(verdict["reason"])
    else:
        mark = "✅ PASS" if verdict["passed"] else "❌ FAIL"
        lines.append(
            f"- 目标并发 **{verdict['target_concurrency']}**：{mark}"
            f"（错误率 {verdict['error_rate'] * 100:.2f}% ≤ 1%，"
            f"P95 {verdict['p95_ms']:.0f}ms ≤ 3000ms）"
        )

    capacity = report.get("capacity")
    if capacity and capacity.get("available"):
        basis = capacity["basis"]
        bound = capacity.get("affinity") or 0
        limit = capacity.get("cores_limit") or 8
        scope = (
            f"本进程绑定 {bound} 核实测"
            if bound >= limit
            else f"由本机 {basis['host_cpu_count']} 核外推"
        )
        lines += [
            "",
            f"## 4. 容量结论（{limit}C16G，{scope}）",
            "",
            (
                f"- 基准点：{'流式' if basis['stream'] else '非流式'} {basis['concurrency']} 并发，"
                f"{basis['rps']} RPS，进程占用 {basis['cpu_cores_avg']} 核，"
                f"**每请求 {basis['cpu_s_per_request']} CPU 秒**，"
                f"RSS 峰值 {capacity.get('rss_mb_peak', basis['rss_mb_max'])} MB（本机 {basis['host_cpu_count']} 核，"
                f"该档主机 CPU 均值 {basis.get('host_cpu_pct_avg', '—')}%）。"
            ),
            (
                f"- 按 {limit} 核 × 60% 利用率、以每请求 CPU 秒估算："
                f"**≈ {capacity['estimated_qps_8c']} QPS**"
                f"（约 {capacity['estimated_qps_8c'] * 60:.0f} 次/分钟；"
                f"CPU 口径上限 {capacity.get('raw_cpu_qps', '—')} QPS，"
                f"已按实测 {basis['rps']} RPS 封顶）。"
            ),
            f"- {capacity['memory_note']}。",
            (
                f"- 按每日 4 小时集中访问、平均利用率 20% 估算，单实例日承载约 "
                f"**{capacity['estimated_daily_4h_peak']} 次问答**（理论上限，仅作量级参考）。"
            ),
            "",
            (
                "> 估算假设：CPU 是主瓶颈，检索/精排耗时可线性分摊；未计入真实网关与公网时延。"
                "单进程 DB 连接池仅 10（pool 5 + overflow 5），真实 LLM 时延也会把吞吐压到远低于上表；"
                "若 30,000 RPM（≈500 QPS）需全部落到 RAG 问答，单实例不足，需横向扩容"
                "或由网关承担缓存/轻量路由。"
            ),
        ]
    else:
        lines += ["", "## 4. 容量结论（外推至 8C16G）", "", "没有满足 P95/错误率阈值的资源样本，无法外推。"]

    micro = report.get("micro") or {}
    micro_cpu = (micro.get("stages") or {}).get("retrieve_cpu_s") or {}
    if micro_cpu.get("n"):
        cpu_s = micro_cpu["p50"]
        est = round(8 * 0.6 / cpu_s, 1) if cpu_s > 0 else 0
        lines += [
            "",
            (
                f"- 检索链单次 CPU 成本 P50 **{cpu_s}s**（P95 {micro_cpu['p95']}s）；"
                f"仅按检索外推，8C×60% 约 **{est} QPS**，尚未计入 LLM 与数据库写入。"
            ),
        ]

    lines += [
        "",
        "## 5. 测量边界",
        "",
        (
            "- LLM 使用 **mock**：首 token / 全程不含真实大模型与网关时延；"
            "真实链路时延需在接入网关后复测（网关侧 SSE 首块此前实测 4ms）。"
        ),
        "- 鉴权关闭、限流关闭：测的是后端处理能力，不代表生产限流策略下的表现。",
        (
            "- 本机为共享开发机（DoubaoWork/WorkBuddy/Qoder 等常驻），主机 CPU 在部分档位接近饱和，"
            "墙钟时延会偏大；容量按每请求 CPU 秒外推，交付前应在 8C16G 目标机复跑本工具。"
        ),
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------- 入口
def parse_args(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description="性能与容量验证（任务 5.3）")
    ap.add_argument("--all", action="store_true", help="检索微基准 + HTTP 压测")
    ap.add_argument("--micro-only", action="store_true")
    ap.add_argument("--http-only", action="store_true")
    ap.add_argument("--base-url", default="", help="复用已启动服务；缺省自启 127.0.0.1:8099")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--stages", default="1,4,8,16,32", help="并发梯度")
    ap.add_argument("--duration", type=float, default=10.0, help="每档持续秒数")
    ap.add_argument("--rounds", type=int, default=5, help="微基准问题轮数")
    ap.add_argument("--role", default="本科生")
    ap.add_argument("--target-concurrency", type=int, default=16)
    ap.add_argument("--affinity", type=int, default=0, help="把服务绑定到前 N 个逻辑核（0=不绑定）")
    ap.add_argument("--skip-scale", action="store_true", help="跳过克隆切片规模模拟")
    ap.add_argument("--report", default=str(ARTIFACTS / "perf-report.md"))
    args = ap.parse_args(argv)
    args.stages = [int(x) for x in str(args.stages).split(",") if x.strip()]
    if not (args.all or args.micro_only or args.http_only):
        args.all = True
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    args = parse_args(argv)
    from app.config import settings

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    host = host_info(settings.store_label)
    print(
        f"主机：{host.cpu_count} 核 / {host.ram_total_mb:.0f} MB · "
        f"存储 {host.store} · Python {host.python}"
    )

    micro = None
    http_rows: list[dict] = []
    proc: subprocess.Popen | None = None
    report_path = Path(args.report)
    json_path = report_path.with_suffix(".json")
    try:
        if not args.http_only:
            micro = micro_benchmark(args.rounds, args.skip_scale)

        if not args.micro_only:
            if args.base_url:
                base_url = args.base_url.rstrip("/")
                if not _health(base_url):
                    raise SystemExit(f"服务不可达：{base_url}/api/health")
                match = re.search(r":(\d+)(?:/|$)", base_url)
                pid = find_listener_pid(int(match.group(1))) if match else None
            else:
                base_url = f"http://127.0.0.1:{args.port}"
                log_path = ARTIFACTS / "perf-service.log"
                print(f"启动独立服务：{base_url}（LLM mock，日志 {log_path}）")
                proc = start_server(args.port, log_path, args.affinity)
                pid = find_listener_pid(args.port) or proc.pid
            print(f"服务进程 PID：{pid or '未知'}（资源采样对象）")
            print("HTTP 压测（每档先预热，再闭环打满）…")
            http_rows = asyncio.run(
                http_benchmark(base_url, args.stages, args.duration, args.role, pid)
            )
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()

    report, markdown = build_report(host, micro, http_rows, args)
    report["capacity"] = (
        _capacity(
            http_rows,
            host.cpu_count,
            args.target_concurrency,
            cores_limit=args.affinity or 8,
            affinity=args.affinity,
        )
        if http_rows
        else None
    )
    # capacity 参与 Markdown 渲染，重新渲染一次
    markdown = _render_markdown(report)
    report_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n报告：{report_path}")
    print(f"原始数据：{json_path}")

    verdict = report["verdict"]
    if verdict.get("passed") is None:
        return 0
    print(
        f"验收判定：{'PASS' if verdict['passed'] else 'FAIL'} "
        f"（目标 {verdict['target_concurrency']} 并发，"
        f"错误率 {verdict['error_rate'] * 100:.2f}%，P95 {verdict['p95_ms']:.0f}ms）"
    )
    return 0 if verdict["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
