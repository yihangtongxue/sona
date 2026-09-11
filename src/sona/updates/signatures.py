"""Publisher authentication independent of Apple membership.

The trust anchor ships inside Sona, never comes from the update server. Signing
tools are separate from the app; the app only contains verification code.
"""

import base64
import hashlib
import json
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ..version import BUNDLE_ID

PUBLIC_KEY_PATH = Path(__file__).with_name("update-public-key.json")


class SignatureError(ValueError):
    pass


def decode64(value, length=None):
    if not isinstance(value, str) or len(value) > 16384:
        raise SignatureError("更新签名格式无效。")
    try:
        decoded = base64.b64decode(value, validate=True)
    except ValueError:
        raise SignatureError("更新签名格式无效。") from None
    if length is not None and len(decoded) != length:
        raise SignatureError("更新签名长度无效。")
    return decoded


def read_public_key(path=PUBLIC_KEY_PATH):
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError()
        record = json.loads(raw)
        public = decode64(record["publicKey"], 32)
        if record.get("algorithm") != "ed25519" or record.get("keyId") != hashlib.sha256(public).hexdigest():
            raise ValueError()
        return public
    except (OSError, ValueError, KeyError, TypeError):
        raise SignatureError("应用未配置有效的更新公钥，请先完成发布配置。") from None


def artifact_payload(version, asset):
    return {
        "schemaVersion": 1, "appId": BUNDLE_ID, "channel": "stable", "version": version,
        **{key: asset[key] for key in ("platform", "architecture", "packageType", "fileName", "size", "sha256")},
    }


def verify_signature(version, asset, public_key=None):
    public = read_public_key() if public_key is None else public_key
    signature = asset.get("updateSignature")
    if not isinstance(signature, dict):
        raise SignatureError("更新包缺少发布者签名，已拒绝更新。")
    try:
        if (signature.get("algorithm") != "ed25519"
                or signature.get("keyId") != hashlib.sha256(public).hexdigest()):
            raise SignatureError("更新包不是由受信任的发布密钥签名。")
        payload = decode64(signature.get("payload"))
        Ed25519PublicKey.from_public_bytes(public).verify(decode64(signature.get("signature"), 64), payload)
        # Compare signed bytes' meaning, not a reserialized cross-language JSON.
        expected = artifact_payload(version, asset)
        signed = json.loads(payload)
        if (not isinstance(signed, dict) or signed != expected
                or type(signed.get("schemaVersion")) is not int or type(signed.get("size")) is not int):
            raise SignatureError("更新清单与签名不匹配，已拒绝更新。")
    except SignatureError:
        raise
    except (InvalidSignature, ValueError, TypeError, KeyError):
        raise SignatureError("更新包签名验证失败，已拒绝更新。") from None


def verify_archive(path, version, asset):
    verify_signature(version, asset)
    digest, size = hashlib.sha256(), 0
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(256 * 1024), b""):
            size += len(chunk)
            if size > asset["size"]:
                raise SignatureError("更新包大小已变化，已拒绝安装。")
            digest.update(chunk)
    if size != asset["size"] or digest.hexdigest() != asset["sha256"]:
        raise SignatureError("更新包已损坏或被修改，已拒绝安装。")
