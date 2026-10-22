"""构建能小智离线交付包（任务 5.2）。

产物目录结构::

    nengxiaozhi-offline-<version>-<stamp>/
      payload/kb-service/        # 应用源码（不含 venv/数据/日志）
      payload/database/          # schema.sql / docker-compose.yml
      payload/knowledge-base/    # 示例知识（交付时由学校文档替换）
      payload/models/            # 可选：BGE 嵌入 + 精排模型
      payload/wheels/            # 可选：目标平台全部 wheel
      payload/docker/            # 可选：MySQL / Nginx 镜像 tar
      scripts/                   # 安装 / 升级 / 回滚（Windows + Linux）
      OFFLINE-INSTALL.md
      BUILD-INFO.json
      MANIFEST.json
      SHA256SUMS

构建机需要联网（下载 wheel / 可选拉取镜像）；目标机全程离线。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
WORKSPACE = SERVICE_DIR.parent
OFFLINE_DIR = SERVICE_DIR / "deploy" / "offline"
MODEL_DIRS = ("bge-small-zh-v1.5", "bge-reranker-v2-m3-ONNX")
SOURCE_ITEMS = (
    "app",
    "static",
    "tools",
    "deploy",
    "README.md",
    "pyproject.toml",
    "uv.lock",
    ".env.example",
    "KB-SERVICE-PLAN.md",
    "LLM_MODEL可省略-变更说明.md",
    "四批开发总计划表.md",
    "批1-开发任务单.md",
    "批2-4-开发任务单.md",
)
DATABASE_ITEMS = ("schema.sql", "docker-compose.yml", "prototype-extra.sql")
IGNORE_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".build",
}


def _log(message: str) -> None:
    print(f"[offline] {message}", flush=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_info() -> dict:
    """记录构建对应的源码提交，便于交付包与仓库对账。"""
    try:
        commit = subprocess.run(
            ["git", "-C", str(WORKSPACE), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "-C", str(WORKSPACE), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return {"commit": "", "commitShort": "", "commitDirty": None}
    return {"commit": commit, "commitShort": commit[:12], "commitDirty": dirty}


def _link_or_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _copy_tree(src: Path, dst: Path, *, skip: set[str] | None = None) -> None:
    skip = skip or set()
    dst.mkdir(parents=True, exist_ok=True)
    for item in sorted(src.iterdir()):
        if item.name in skip or item.name in IGNORE_NAMES:
            continue
        target = dst / item.name
        if item.is_dir():
            _copy_tree(item, target, skip=skip)
        else:
            shutil.copy2(item, target)


def _copy_project(dest: Path) -> None:
    for name in SOURCE_ITEMS:
        src = SERVICE_DIR / name
        if not src.exists():
            raise SystemExit(f"缺少源码项：{src}")
        if src.is_dir():
            _copy_tree(src, dest / name)
        else:
            shutil.copy2(src, dest / name)


def _copy_database(dest: Path) -> None:
    src_dir = WORKSPACE / "database"
    dest.mkdir(parents=True, exist_ok=True)
    for name in DATABASE_ITEMS:
        src = src_dir / name
        if not src.exists():
            raise SystemExit(f"缺少数据库文件：{src}")
        shutil.copy2(src, dest / name)


def _copy_knowledge(dest: Path) -> None:
    src = WORKSPACE / "knowledge-base"
    if not src.exists():
        raise SystemExit(f"缺少知识库目录：{src}")
    _copy_tree(src, dest)


def _copy_models(dest: Path, *, hardlink: bool) -> list[dict]:
    rows: list[dict] = []
    for name in MODEL_DIRS:
        src = SERVICE_DIR / "models" / name
        if not src.exists():
            raise SystemExit(f"缺少模型目录：{src}（离线交付必须随包）")
        total = sum(p.stat().st_size for p in src.rglob("*") if p.is_file())
        target = dest / name
        if hardlink:
            for item in src.rglob("*"):
                if item.is_file():
                    _link_or_copy(item, target / item.relative_to(src))
        else:
            shutil.copytree(src, target)
        rows.append({"name": name, "bytes": total})
        _log(f"模型 {name}: {total / 1024 / 1024:.1f} MB")
    return rows


def _ensure_seed_venv(path: Path, python_version: str) -> Path:
    python = path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if python.exists():
        return python
    _log(f"创建构建用 venv：{path}")
    subprocess.run(
        ["uv", "venv", str(path), "--python", python_version, "--seed"],
        check=True,
        cwd=SERVICE_DIR,
    )
    if not python.exists():
        raise SystemExit(f"构建 venv 创建失败：{python}")
    return python


def _download_wheels(dest: Path, python_version: str, platform: str, *, force: bool) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.iterdir()) and not force:
        _log(f"复用已有 wheels：{dest}")
        return
    if dest.exists():
        shutil.rmtree(dest)
        dest.mkdir(parents=True)
    python = _ensure_seed_venv(SERVICE_DIR / ".build" / "offline-build-venv", python_version)
    command = [
        str(python),
        "-m",
        "pip",
        "download",
        "--only-binary=:all:",
        "--dest",
        str(dest),
        "--requirement",
        str(OFFLINE_DIR / "requirements-runtime.txt"),
    ]
    if platform:
        abi = f"cp{python_version.replace('.', '')}"
        command += [
            "--platform",
            platform,
            "--python-version",
            python_version,
            "--implementation",
            "cp",
            "--abi",
            abi,
        ]
    _log("下载运行时 wheels（仅二进制）…")
    subprocess.run(command, check=True, cwd=SERVICE_DIR)
    _build_project_wheel(dest)


def _build_project_wheel(dest: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="nxz-wheel-") as tmp:
        subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", tmp],
            check=True,
            cwd=SERVICE_DIR,
        )
        wheels = list(Path(tmp).glob("*.whl"))
        if not wheels:
            raise SystemExit("uv build 未产出 wheel")
        for wheel in wheels:
            shutil.copy2(wheel, dest / wheel.name)
            _log(f"项目 wheel：{wheel.name}")


def _copy_wheels(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    for item in src.iterdir():
        if item.is_file():
            _link_or_copy(item, dest / item.name)
            count += 1
    if count == 0:
        raise SystemExit(f"复用目录中没有 wheel：{src}")


def _save_docker_images(images: list[str], dest: Path) -> list[dict]:
    if not images:
        return []
    if not shutil.which("docker"):
        raise SystemExit("指定了 --with-docker-images，但本机没有 docker")
    dest.mkdir(parents=True, exist_ok=True)
    rows = []
    for image in images:
        _log(f"拉取并导出镜像：{image}")
        subprocess.run(["docker", "pull", image], check=True)
        name = image.replace("/", "_").replace(":", "_") + ".tar"
        target = dest / name
        subprocess.run(["docker", "save", "-o", str(target), image], check=True)
        rows.append({"image": image, "file": name, "bytes": target.stat().st_size})
    return rows


def _copy_scripts(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for item in OFFLINE_DIR.iterdir():
        if item.name in {"__pycache__"}:
            continue
        target = dest / item.name
        if item.is_dir():
            _copy_tree(item, target)
        else:
            shutil.copy2(item, target)
    for sh in dest.glob("*.sh"):
        sh.chmod(0o755)
    # Windows PowerShell 5.1 以 ANSI 解析无 BOM 脚本，中文会破坏语法
    for ps1 in dest.glob("*.ps1"):
        data = ps1.read_bytes()
        if not data.startswith(b"\xef\xbb\xbf"):
            ps1.write_bytes(b"\xef\xbb\xbf" + data)


def _write_manifest(root: Path) -> int:
    files = []
    for item in sorted(root.rglob("*")):
        if not item.is_file():
            continue
        rel = item.relative_to(root).as_posix()
        if rel in {"MANIFEST.json", "SHA256SUMS"}:
            continue
        files.append({"path": rel, "bytes": item.stat().st_size, "sha256": _sha256(item)})
    payload = {
        "generatedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "fileCount": len(files),
        "totalBytes": sum(f["bytes"] for f in files),
        "files": files,
    }
    (root / "MANIFEST.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [f"{f['sha256']}  {f['path']}" for f in files]
    (root / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(files)


def _project_version() -> str:
    with (SERVICE_DIR / "pyproject.toml").open("rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


def build(args: argparse.Namespace) -> Path:
    version = _project_version()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    label = f"-{args.label}" if args.label else ""
    root = Path(args.out).resolve() / f"nengxiaozhi-offline-{version}-{stamp}{label}"
    if root.exists():
        if not args.force:
            raise SystemExit(f"目录已存在：{root}（加 --force 覆盖）")
        shutil.rmtree(root)
    payload = root / "payload"
    payload.mkdir(parents=True)

    _log(f"构建目录：{root}")
    _copy_project(payload / "kb-service")
    _copy_database(payload / "database")
    _copy_knowledge(payload / "knowledge-base")

    models: list[dict] = []
    if not args.skip_models:
        models = _copy_models(payload / "models", hardlink=not args.copy_models)

    wheels_dir = payload / "wheels"
    if args.reuse_wheels:
        _copy_wheels(Path(args.reuse_wheels).resolve(), wheels_dir)
        _log(f"复用 wheels：{args.reuse_wheels}")
    elif not args.skip_wheels:
        _download_wheels(
            wheels_dir,
            args.python_version,
            args.platform,
            force=args.force,
        )
    wheels = sorted(p.name for p in wheels_dir.glob("*.whl")) if wheels_dir.exists() else []

    docker_rows = _save_docker_images(args.with_docker_images, payload / "docker")
    _copy_scripts(root / "scripts")
    shutil.copy2(OFFLINE_DIR / "OFFLINE-INSTALL.md", root / "OFFLINE-INSTALL.md")

    git = _git_info()
    if git["commitDirty"]:
        _log("警告：工作区有未提交改动，交付包内容与 commit 不完全一致")
    info = {
        "product": "能小智校园 AI 助手平台",
        "version": version,
        "bundleLabel": args.label or "",
        "builtAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": git["commit"],
        "commitShort": git["commitShort"],
        "commitDirty": git["commitDirty"],
        "targetPython": args.python_version,
        "targetPlatform": args.platform or "current",
        "includesModels": bool(models),
        "models": models,
        "includesWheels": bool(wheels),
        "wheelCount": len(wheels),
        "dockerImages": docker_rows,
    }
    (root / "BUILD-INFO.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if git["commit"]:
        (root / "COMMIT.txt").write_text(f"{git['commit']}\n", encoding="utf-8")
    count = _write_manifest(root)
    total = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
    _log(f"完成：{count} 个文件，{total / 1024 / 1024:.0f} MB")
    return root


def main() -> None:
    parser = argparse.ArgumentParser(description="构建离线交付包（任务 5.2）")
    parser.add_argument("--out", default=str(SERVICE_DIR / "dist" / "offline"))
    parser.add_argument("--label", default="", help="附加到目录名的标识（如 rc1）")
    parser.add_argument("--skip-models", action="store_true", help="不打包 BGE 模型")
    parser.add_argument("--copy-models", action="store_true", help="复制模型（默认同盘硬链接）")
    parser.add_argument("--skip-wheels", action="store_true", help="不下载 wheel")
    parser.add_argument("--reuse-wheels", default="", help="复用已有 wheels 目录")
    parser.add_argument("--python-version", default="3.12")
    parser.add_argument("--platform", default="win_amd64", help="pip 目标平台；Linux 用 manylinux2014_x86_64")
    parser.add_argument(
        "--with-docker-images",
        nargs="*",
        default=[],
        metavar="IMAGE",
        help="同时导出 Docker 镜像（如 mysql:8.0 nginx:1.27-alpine）",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.reuse_wheels and args.skip_wheels:
        parser.error("--reuse-wheels 与 --skip-wheels 不能同时使用")
    root = build(args)
    print(root)


if __name__ == "__main__":
    main()
