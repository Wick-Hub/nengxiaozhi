"""渲染 Nginx 生产配置（任务 5.1）。

把模板里的端口、域名、证书目录、上游地址替换为实际值，避免交付时手工改配置漏项::

    python deploy/nginx/render_nginx_conf.py --out deploy/nginx/nengxiaozhi.conf \
        --server-name kb.example.edu.cn --cert-dir deploy/certs

Docker 验收时可把上游换成 ``host.docker.internal:8000``。
"""

from __future__ import annotations

import argparse
from pathlib import Path

TEMPLATE = Path(__file__).with_name("nengxiaozhi.conf.template")


def main() -> int:
    parser = argparse.ArgumentParser(description="渲染 kb-service Nginx 配置")
    parser.add_argument("--out", required=True)
    parser.add_argument("--server-name", default="kb.example.edu.cn")
    parser.add_argument("--cert-dir", required=True)
    parser.add_argument("--upstream", default="127.0.0.1:8000")
    parser.add_argument("--http-port", type=int, default=80)
    parser.add_argument("--https-port", type=int, default=443)
    args = parser.parse_args()

    # 不要 resolve()：容器内路径 /etc/nginx/certs 在 Windows 上会被解析成 E:\etc\...
    cert_dir = args.cert_dir.replace("\\", "/").rstrip("/")
    conf = TEMPLATE.read_text(encoding="utf-8")
    replacements = {
        "__SERVER_NAME__": args.server_name,
        "__CERT_DIR__": cert_dir,
        "__UPSTREAM__": args.upstream,
        "__HTTP_PORT__": str(args.http_port),
        "__HTTPS_PORT__": str(args.https_port),
    }
    for key, value in replacements.items():
        conf = conf.replace(key, value)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(conf, encoding="utf-8", newline="\n")
    print(f"Nginx 配置已生成：{out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
