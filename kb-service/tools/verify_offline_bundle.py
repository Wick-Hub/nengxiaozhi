"""离线交付包验收（任务 5.2）。

默认复用 ``dist/offline`` 下最新的离线包，在临时安装目录执行：
校验包完整性 → 全新安装 → 导入/pip check → 启动 + 健康检查 →
写入共享数据 → 升级 → 回滚 → 数据留存检查。

用法::

    uv run --no-sync python tools/verify_offline_bundle.py
    uv run --no-sync python tools/verify_offline_bundle.py --bundle dist/offline/<包名> --keep

Windows 走 ``install.ps1`` / ``upgrade.ps1`` / ``rollback.ps1``；
Linux 只做静态检查（动态安装需在目标 Linux 机执行）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
DIST_DIR = SERVICE_DIR / "dist" / "offline"
BUILD_TOOL = SERVICE_DIR / "tools" / "build_offline_bundle.py"


class Checks:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []
        self.warned = 0

    def ok(self, label: str, detail: str = "") -> None:
        self.passed += 1
        print(f"PASS  {label}" + (f"  [{detail}]" if detail else ""))

    def fail(self, label: str, detail: str = "") -> None:
        self.failed.append(label)
        print(f"FAIL  {label}" + (f"  [{detail}]" if detail else ""))

    def check(self, condition: bool, label: str, detail: str = "") -> bool:
        if condition:
            self.ok(label, detail)
        else:
            self.fail(label, detail)
        return condition

    def warn(self, label: str) -> None:
        self.warned += 1
        print(f"WARN  {label}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _run(command: list[str], timeout: int = 900, cwd: Path | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        command,
        cwd=cwd or SERVICE_DIR,
        capture_output=True,
        timeout=timeout,
    )
    output = _decode(proc.stdout + proc.stderr)
    return proc.returncode, output


def _find_bundle(explicit: str) -> Path:
    if explicit:
        path = Path(explicit).resolve()
        if not path.is_dir():
            raise SystemExit(f"离线包目录不存在：{path}")
        return path
    candidates = [
        item for item in DIST_DIR.glob("nengxiaozhi-offline-*")
        if item.is_dir() and (item / "SHA256SUMS").is_file()
        and (item / "payload" / "wheels").is_dir()
    ]
    if not candidates:
        raise SystemExit(
            f"{DIST_DIR} 下没有包含 wheels 的离线包；"
            f"先执行：uv run --no-sync python {BUILD_TOOL.relative_to(SERVICE_DIR)}"
        )
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _health(port: int, timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health", timeout=timeout
        ) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _wait_health(port: int, timeout: int = 90) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = _health(port)
        if payload is not None:
            return payload
        time.sleep(1)
    return None


def _current_release(root: Path) -> str:
    marker = root / "current-release.txt"
    if not marker.is_file():
        return ""
    return marker.read_text(encoding="utf-8").strip()


def _write_verify_env(path: Path, port: int) -> None:
    path.write_text(
        "\n".join(
            [
                "APP_HOST=127.0.0.1",
                f"APP_PORT={port}",
                "DEBUG=0",
                "KB_STORE=json",
                "STORE_PATH=./data/store.json",
                "LLM_PROVIDER=gateway",
                "LLM_BASE_URL=http://127.0.0.1:8080/v1",
                "LLM_API_KEY=offline-verify-key",
                "LLM_MOCK=1",
                "LLM_TIMEOUT=10",
                "HF_ENDPOINT=",
                "AUTH_ENABLED=false",
                "RATE_LIMIT_ENABLED=0",
                "SENSITIVE_CHECK_ENABLED=0",
                "RERANK_ENABLED=0",
                "GOV_ENABLED=1",
                "DQ_ENABLED=0",
                "DATA_CENTER_ENABLED=0",
                "ASR_ENABLED=0",
                "SUPPORT_ENABLED=1",
                "SUPPORT_AUTO_SEED=0",
                "KNOWLEDGE_DIR=../knowledge-base",
                "UPLOAD_DIR=./uploads",
                "MODEL_DIR=./models",
                "",
            ]
        ),
        encoding="utf-8",
    )


def static_checks(bundle: Path, checks: Checks) -> dict:
    info_path = bundle / "BUILD-INFO.json"
    if not info_path.is_file():
        checks.fail("BUILD-INFO.json 存在")
        return {}
    info = json.loads(info_path.read_text(encoding="utf-8"))
    checks.ok("BUILD-INFO.json 存在", f"version={info.get('version')}")

    payload = bundle / "payload"
    checks.check(
        (payload / "kb-service" / "app" / "main.py").is_file(),
        "应用源码完整",
    )
    checks.check(
        (payload / "kb-service" / "app" / "models" / "schemas.py").is_file(),
        "app/models 未被误排除",
    )
    checks.check(
        (payload / "database" / "schema.sql").is_file(),
        "初始化 SQL 存在",
    )
    checks.check(
        (payload / "knowledge-base").is_dir(),
        "示例知识库存在",
    )
    checks.check((bundle / "OFFLINE-INSTALL.md").is_file(), "安装手册存在")

    wheels = sorted((payload / "wheels").glob("*.whl")) if (payload / "wheels").is_dir() else []
    project_wheels = [w for w in wheels if w.name.startswith("kb_service-")]
    checks.check(bool(project_wheels), "应用 wheel 存在", project_wheels[0].name if project_wheels else "")
    checks.check(len(wheels) >= 30, "运行时 wheel 数量", f"{len(wheels)} 个")
    banned = [
        w.name for w in wheels
        if w.name.lower().startswith(("torch-", "sentence_transformers-", "sentence-transformers-"))
    ]
    checks.check(not banned, "未混入 PyTorch / sentence-transformers", ", ".join(banned))

    requirements = bundle / "scripts" / "requirements-runtime.txt"
    if requirements.is_file():
        text = requirements.read_text(encoding="utf-8")
        names = []
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                names.append(re.split(r"[<>=!~\[;\s]", line, maxsplit=1)[0].lower())
        banned_deps = [
            name for name in names
            if name == "torch" or name.startswith(("sentence-transformers", "sentence_transformers"))
        ]
        checks.check(not banned_deps, "运行时清单不含 PyTorch 依赖", ", ".join(banned_deps))
        checks.check("onnxruntime" in names, "运行时清单含 onnxruntime")
    else:
        checks.fail("scripts/requirements-runtime.txt 存在")

    for name in ("common", "install", "upgrade", "rollback"):
        ps1 = bundle / "scripts" / f"{name}.ps1"
        sh = bundle / "scripts" / f"{name}.sh"
        checks.check(ps1.is_file(), f"{name}.ps1 存在")
        checks.check(sh.is_file(), f"{name}.sh 存在")
    for ps1 in (bundle / "scripts").glob("*.ps1"):
        data = ps1.read_bytes()
        checks.check(
            data.startswith(b"\xef\xbb\xbf"),
            f"{ps1.name} 带 UTF-8 BOM",
        )

    for forbidden in (
        payload / "kb-service" / ".env",
        payload / "kb-service" / ".venv",
        payload / "kb-service" / "campus-ai-gateway",
        payload / "kb-service" / "data" / "store.json",
    ):
        checks.check(not forbidden.exists(), f"未打包敏感/运行产物：{forbidden.name}")
    pycache = list((payload / "kb-service").rglob("__pycache__"))
    checks.check(not pycache, "未打包 __pycache__", f"{len(pycache)} 个")

    sums = bundle / "SHA256SUMS"
    bad: list[str] = []
    count = 0
    if sums.is_file():
        for line in sums.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            expected, _, rel = line.partition("  ")
            target = bundle / rel
            count += 1
            if not target.is_file() or _sha256(target) != expected:
                bad.append(rel)
    checks.check(sums.is_file() and not bad, "SHA256SUMS 全部匹配", f"{count} 个文件")

    if info.get("includesModels"):
        for name in ("bge-small-zh-v1.5", "bge-reranker-v2-m3-ONNX"):
            checks.check(
                (payload / "models" / name).is_dir(),
                f"模型随包：{name}",
            )
    else:
        checks.warn("离线包未包含模型（--skip-models）；正式交付包必须包含模型")

    checks.check(bool(info.get("includesWheels")), "BUILD-INFO 标记包含 wheels")
    return info


def _ps(script: Path, *args: str) -> tuple[int, str]:
    command = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        *args,
    ]
    return _run(command)


def dynamic_checks(
    bundle: Path, info: dict, checks: Checks, keep: bool, link_models: bool
) -> None:
    if os.name != "nt":
        checks.warn("非 Windows：跳过安装/升级/回滚动态验收（需在目标 Linux 机按手册执行）")
        return

    target_python = str(info.get("targetPython") or "")
    actual = f"{sys.version_info.major}.{sys.version_info.minor}"
    if actual != target_python:
        checks.fail("验收机 Python 与包目标一致", f"当前 {actual}，包 {target_python}")
        return
    checks.ok("验收机 Python 与包目标一致", actual)

    build_dir = SERVICE_DIR / ".build"
    build_dir.mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="offline-verify-", dir=build_dir))
    env_file = root / "verify.env"
    port = _free_port()
    _write_verify_env(env_file, port)
    scripts = bundle / "scripts"
    current_process_release = ""
    try:
        code, output = _ps(
            scripts / "install.ps1",
            "-BundleDir", str(bundle),
            "-InstallDir", str(root),
            "-PythonExe", sys.executable,
            "-EnvFile", str(env_file),
            "-NoStart",
            *(["-LinkModels"] if link_models else []),
        )
        if not checks.check(code == 0, "离线安装（--no-index）", output.strip().splitlines()[-1] if output.strip() else ""):
            print(output[-4000:])
            return

        release1 = _current_release(root)
        current_process_release = release1
        checks.check(bool(release1), "安装后 current-release.txt 有效", release1)
        current_link = root / "current"
        checks.check(current_link.exists(), "current 联接可用")
        release_dir = root / "releases" / release1
        checks.check((release_dir / "kb-service").is_dir(), "版本目录展开")

        venv_python = release_dir / "kb-service" / ".venv" / "Scripts" / "python.exe"
        checks.check(venv_python.is_file(), "venv Python 存在")
        code, output = _run([
            str(venv_python), "-c",
            "import app.main, fastapi, onnxruntime, pymysql; print('import-ok')",
        ], timeout=120)
        checks.check(code == 0 and "import-ok" in output, "安装环境导入 app.main", output.strip()[-200:])
        code, output = _run([str(venv_python), "-m", "pip", "check"], timeout=120)
        checks.check(code == 0, "pip check 无依赖冲突", output.strip()[-300:])

        env_text = (release_dir / "kb-service" / ".env").read_text(encoding="utf-8")
        model_line = next((l for l in env_text.splitlines() if l.startswith("MODEL_DIR=")), "")
        checks.check(
            model_line.startswith("MODEL_DIR=") and Path(model_line.split("=", 1)[1]).is_absolute(),
            "MODEL_DIR 已改写为绝对路径",
            model_line,
        )
        checks.check(
            "KNOWLEDGE_DIR=../knowledge-base" in env_text,
            "KNOWLEDGE_DIR 指向共享知识库",
        )

        if info.get("includesModels"):
            code, output = _run(
                [
                    str(venv_python),
                    "-c",
                    "from app.core.embedder import get_embedder;"
                    " v=get_embedder().encode_one('校园网怎么开通'); print(len(v))",
                ],
                timeout=240,
                cwd=release_dir / "kb-service",
            )
            checks.check(
                code == 0 and output.strip().endswith("512"),
                "BGE 嵌入模型可加载（512 维）",
                output.strip()[-200:],
            )

        start_ps1 = release_dir / "kb-service" / "deploy" / "windows" / "start.ps1"
        stop_ps1 = release_dir / "kb-service" / "deploy" / "windows" / "stop.ps1"
        code, output = _ps(start_ps1)
        checks.check(code == 0, "服务启动脚本", output.strip().splitlines()[-1] if output.strip() else "")
        health = _wait_health(port)
        checks.check(bool(health), "健康检查 /api/health", json.dumps(health, ensure_ascii=False) if health else "超时")
        if health:
            checks.check(health.get("store") == "json", "存储模式为 json", str(health.get("store")))
            checks.check(health.get("llm") == "mock", "LLM 为 mock", str(health.get("llm")))

        marker = root / "shared" / "data" / "offline-verify-marker.txt"
        marker.write_text(release1, encoding="utf-8")
        checks.ok("写入共享数据标记")

        time.sleep(1.2)
        code, output = _ps(
            scripts / "upgrade.ps1",
            "-BundleDir", str(bundle),
            "-InstallDir", str(root),
            "-PythonExe", sys.executable,
            "-SkipDbBackup",
        )
        if not checks.check(code == 0, "离线升级", output.strip().splitlines()[-1] if output.strip() else ""):
            print(output[-4000:])
            return
        release2 = _current_release(root)
        checks.check(release2 not in ("", release1), "升级切换到新版本", release2)
        health = _wait_health(port)
        checks.check(bool(health), "升级后健康检查", str(health.get("store") if health else "超时"))
        checks.check(marker.read_text(encoding="utf-8") == release1, "升级后共享数据保留")

        code, output = _ps(scripts / "rollback.ps1", "-InstallDir", str(root))
        if not checks.check(code == 0, "离线回滚", output.strip().splitlines()[-1] if output.strip() else ""):
            print(output[-4000:])
            return
        release3 = _current_release(root)
        checks.check(release3 == release1, "回滚恢复原版本", release3)
        health = _wait_health(port)
        checks.check(bool(health), "回滚后健康检查", str(health.get("store") if health else "超时"))
        checks.check(marker.read_text(encoding="utf-8") == release1, "回滚后共享数据保留")

        history_path = root / "INSTALL-INFO.json"
        if history_path.is_file():
            history = json.loads(history_path.read_text(encoding="utf-8")).get("history", [])
            actions = [row.get("action") for row in history]
            checks.check(
                actions[:3] == ["install", "upgrade", "rollback"],
                "安装历史完整",
                " → ".join(actions),
            )
        else:
            checks.fail("INSTALL-INFO.json 存在")
    finally:
        stop_script = None
        if current_process_release:
            candidate = root / "releases" / current_process_release / "kb-service" / "deploy" / "windows" / "stop.ps1"
            if candidate.is_file():
                stop_script = candidate
        if stop_script:
            try:
                _ps(stop_script)
            except Exception:
                pass
        if keep:
            print(f"保留验收目录：{root}")
        else:
            time.sleep(1)
            shutil.rmtree(root, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="离线交付包验收（任务 5.2）")
    parser.add_argument("--bundle", default="", help="离线包目录；默认取 dist/offline 下最新")
    parser.add_argument("--keep", action="store_true", help="保留临时安装目录")
    parser.add_argument("--link-models", action="store_true", help="安装时链接模型目录（加速验收）")
    args = parser.parse_args()

    bundle = _find_bundle(args.bundle)
    print(f"离线包：{bundle}")
    checks = Checks()
    info = static_checks(bundle, checks)
    if info:
        dynamic_checks(bundle, info, checks, args.keep, args.link_models)

    print()
    print(
        f"结果：{checks.passed} PASS / {len(checks.failed)} FAIL / {checks.warned} WARN"
    )
    if checks.failed:
        print("失败项：" + "；".join(checks.failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
