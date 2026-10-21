"""生成自签 TLS 证书（任务 5.1，测试/内网交付用）。

正式交付应替换为学校 CA 签发的证书；自签证书用于先跑通 HTTPS 部署链路。
生成 ``fullchain.pem``（证书）与 ``privkey.pem``（私钥），可直接给 Nginx 使用::

    python deploy/scripts/gen_selfsigned_cert.py --out-dir deploy/certs \
        --hostname kb.example.edu.cn
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def _san_entries(names: list[str]) -> list[x509.GeneralName]:
    out: list[x509.GeneralName] = []
    for name in names:
        try:
            out.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            out.append(x509.DNSName(name))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 kb-service 自签证书")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--hostname", default="localhost",
                        help="逗号分隔的 DNS/IP，默认 localhost,127.0.0.1")
    parser.add_argument("--days", type=int, default=397)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    names = [n.strip() for n in args.hostname.split(",") if n.strip()]
    if "127.0.0.1" not in names:
        names.append("127.0.0.1")
    if "localhost" not in names:
        names.append("localhost")

    out_dir = Path(args.out_dir).resolve()
    cert_path = out_dir / "fullchain.pem"
    key_path = out_dir / "privkey.pem"
    if not args.force and (cert_path.exists() or key_path.exists()):
        print(f"证书已存在：{out_dir}（加 --force 覆盖）")
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, names[0]),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "NengXiaoZhi"),
        ]
    )
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=args.days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(x509.SubjectAlternativeName(_san_entries(names)), critical=False)
        .sign(key, hashes.SHA256())
    )

    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    if os.name == "posix":
        key_path.chmod(0o600)

    print(f"证书：{cert_path}")
    print(f"私钥：{key_path}")
    print(f"域名：{', '.join(names)}")
    print(f"有效期至：{cert.not_valid_after_utc:%Y-%m-%d}")
    print(f"SHA256：{cert.fingerprint(hashes.SHA256()).hex()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
