"""Encrypted Ed25519 keys and release signing, including isolated GitHub CI."""

import argparse
import base64
import getpass
import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sona.updates.signatures import artifact_payload, read_public_key
from sona.version import VERSION
from release_support import PUBLIC_KEY, check_release_context, check_version
from release_assets import describe_asset


def write_new(path, content, mode=0o600):
    # Never replace an existing key or signature file.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)


def public_record(key):
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {"algorithm": "ed25519", "keyId": hashlib.sha256(public).hexdigest(),
            "publicKey": base64.b64encode(public).decode("ascii")}


def create_keys(directory):
    directory = directory.expanduser().resolve()
    if directory.is_relative_to(ROOT) or any((parent / ".git").exists() for parent in (directory, *directory.parents)):
        raise ValueError("密钥必须保存在 Git 仓库之外。")
    if directory.exists():
        raise ValueError("请选择尚不存在的专用密钥目录，不覆盖已有密钥。")
    password = getpass.getpass("设置私钥密码（至少 12 个字符，不显示）：")
    if len(password) < 12 or password != getpass.getpass("再次输入私钥密码："):
        raise ValueError("密码长度不足或两次输入不一致。")
    key = Ed25519PrivateKey.generate()
    encrypted = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.BestAvailableEncryption(password.encode("utf-8")))
    directory.mkdir(parents=True, mode=0o700)
    write_new(directory / "update-private.pem", encrypted)
    record = public_record(key)
    write_new(directory / "update-public-key.json", json.dumps(record, indent=2).encode(), 0o644)
    print(f"密钥已保存到 {directory}。请离线备份私钥及密码；不要上传或发送私钥。")
    print(f"公钥指纹：{record['keyId']}")


def sign(args):
    check_version(args.confirm_version)
    artifacts = [path.expanduser().resolve() for path in args.artifacts]
    if len(set(artifacts)) != len(artifacts):
        raise ValueError("安装包路径重复。")
    if args.ci:
        check_release_context()
        pem = os.environ.pop("SONA_UPDATE_PRIVATE_KEY_PEM", "").encode("utf-8")
        password = os.environ.pop("SONA_UPDATE_KEY_PASSWORD", "").encode("utf-8")
        if not pem or not password:
            raise ValueError("请在 release 环境配置更新私钥和密码两个 GitHub Secrets。")
    else:
        private_path = args.private_key.expanduser().resolve()
        if os.name != "nt" and private_path.stat().st_mode & 0o077:
            raise ValueError("私钥权限过宽，请先设置为仅当前用户可读写（600）。")
        with private_path.open("rb") as stream:
            pem = stream.read(16385)
        password = getpass.getpass("输入私钥密码（不显示）：").encode("utf-8")
    if len(pem) > 16384:
        raise ValueError("私钥文件过大。")
    if b"-----BEGIN ENCRYPTED PRIVATE KEY-----" not in pem:
        raise ValueError("需要加密的 PKCS8 私钥。")
    key = serialization.load_pem_private_key(pem, password=password)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("需要 Ed25519 私钥。")
    record = public_record(key)
    public = read_public_key(args.public_key)
    if base64.b64decode(record["publicKey"]) != public or public != read_public_key(PUBLIC_KEY):
        raise ValueError("私钥与打包公钥不匹配。")
    prepared = []
    for artifact in artifacts:
        output = artifact.with_name(artifact.name + ".sig.json")
        if output.exists():
            raise ValueError(f"签名文件已存在：{output.name}。不要覆盖已发布产物。")
        asset = describe_asset(artifact)
        payload = json.dumps(artifact_payload(VERSION, asset), sort_keys=True, separators=(",", ":")).encode("utf-8")
        sidecar = {**record, "payload": base64.b64encode(payload).decode("ascii"),
                   "signature": base64.b64encode(key.sign(payload)).decode("ascii")}
        prepared.append((output, json.dumps(sidecar, indent=2).encode("utf-8")))
    for output, content in prepared:
        write_new(output, content, 0o644)
        print(f"已生成签名：{output}")
    print("签名已生成，可生成 GitHub 更新清单；不要修改已签名安装包。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate")
    generate.add_argument("--directory", type=Path, required=True)
    signer = commands.add_parser("sign")
    source = signer.add_mutually_exclusive_group(required=True)
    source.add_argument("--private-key", type=Path)
    source.add_argument("--ci", action="store_true")
    signer.add_argument("--public-key", type=Path, default=PUBLIC_KEY)
    signer.add_argument("--confirm-version", required=True)
    signer.add_argument("artifacts", type=Path, nargs="+")
    args = parser.parse_args()
    if args.command == "generate":
        create_keys(args.directory)
    else:
        sign(args)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, zipfile.BadZipFile) as error:
        raise SystemExit(f"操作未完成：{error}") from None
