"""Untrusted public metadata: bounded reads, exact targets, no publisher token."""

import hashlib
import ipaddress
import json
import logging
import re
import socket
import ssl
import platform
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from ..version import UPDATE_MANIFEST_URL, VERSION
from .signatures import SignatureError, verify_signature

logger = logging.getLogger(__name__)

MAX_PACKAGE_BYTES = 4 * 1024**3
MAX_MANIFEST_BYTES = 256 * 1024
# TUN proxies can resolve public hostnames into this synthetic address range.
# Accept it only as a DNS result, never as a literal address in an update URL.
PROXY_DNS_RANGE = ipaddress.ip_network("198.18.0.0/15")


class UpdateError(ValueError):
    pass


def current_target():
    machine = platform.machine().lower()
    architecture = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64"}.get(machine, machine)
    return {"darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform), architecture


def version_tuple(value):
    if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value):
        raise UpdateError("版本号格式无效，需要三段数字。")
    return tuple(map(int, value.split(".")))


def public_url(value, *, resolve=False):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.fragment
                or any(k.lower() in {"access_token", "token", "authorization"} for k, _ in parse_qsl(parsed.query))):
            raise ValueError()
        host = parsed.hostname.lower()
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError()
        if resolve:
            addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            if not addresses:
                raise UpdateError("无法解析更新服务器地址，请检查网络或代理设置。")
            for item in addresses:
                resolved = ipaddress.ip_address(item[4][0])
                if not resolved.is_global and resolved not in PROXY_DNS_RANGE:
                    raise UpdateError("更新服务器解析到了非公网地址，请检查网络或代理设置。")
    except UpdateError:
        raise
    except OSError:
        raise UpdateError("无法解析更新服务器地址，请检查网络或代理设置。") from None
    except (TypeError, ValueError):
        raise UpdateError("更新地址必须是公开的 HTTPS 地址，且不能携带发布凭据。") from None
    return value


class SafeRedirect(HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, request, response, code, message, headers, newurl):
        public_url(newurl, resolve=True)
        return super().redirect_request(request, response, code, message, headers, newurl)


def open_public(url):
    # Release metadata/signing tools also import version_tuple from this module,
    # but do not make network requests or install the desktop dependencies.
    import certifi

    public_url(url, resolve=True)
    request = Request(url, headers={"User-Agent": f"Sona/{VERSION}", "Accept": "application/json, application/octet-stream"})
    # Keep the original hostname for proxy routing and TLS certificate validation,
    # including when DNS returns a synthetic proxy address. Redirects are revalidated.
    # Frozen Python's default CA path can point at the build machine. Add the
    # bundled CA roots while retaining available system/custom trust roots.
    # Certificate and hostname verification remain enabled for every redirect.
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    return build_opener(SafeRedirect(), HTTPSHandler(context=context)).open(request, timeout=20)


@dataclass(frozen=True)
class Release:
    version: str
    notes: str
    size: int
    sha256: str
    url: str
    architecture: str
    file_name: str = "Sona.zip"
    update_signature: dict | None = None
    platform: str = "macos"

    def signed_asset(self):
        return {"platform": self.platform, "architecture": self.architecture, "packageType": "zip",
                "fileName": self.file_name, "size": self.size, "sha256": self.sha256,
                "updateSignature": self.update_signature}


def parse_manifest(data, architecture="arm64", *, platform="macos"):
    if (not isinstance(data, dict) or type(data.get("schemaVersion")) is not int
            or data["schemaVersion"] != 1 or data.get("channel") != "stable"):
        raise UpdateError("不支持的更新清单格式，请检查 GitHub Release 发布配置。")
    version = data.get("version")
    version_tuple(version)
    if data.get("tag") != f"v{version}":
        raise UpdateError("更新清单的版本与标签不一致。")
    notes = data.get("notes", "")
    if not isinstance(notes, str) or len(notes) > 20000:
        raise UpdateError("更新说明格式无效。")
    assets = data.get("assets")
    if not isinstance(assets, list) or not 1 <= len(assets) <= 20:
        raise UpdateError("更新清单缺少安装包。")
    targets = set()
    candidates = {}
    for asset in assets:
        if not isinstance(asset, dict):
            raise UpdateError("安装包信息无效。")
        target = tuple(asset.get(key) for key in ("platform", "architecture", "packageType"))
        if not all(isinstance(value, str) for value in target) or target in targets:
            raise UpdateError("更新清单包含无效或重复的构建目标。")
        targets.add(target)
        name, size, checksum = asset.get("fileName"), asset.get("size"), asset.get("sha256")
        if (not isinstance(name, str) or not name or len(name) > 255 or re.search(r"[\\/\x00-\x1f]", name)
                or not name.lower().endswith("." + target[2]) or type(size) is not int
                or not 0 < size <= MAX_PACKAGE_BYTES or not isinstance(checksum, str)
                or not re.fullmatch(r"[0-9a-fA-F]{64}", checksum)):
            raise UpdateError("安装包的文件名、大小或校验值无效。")
        public_url(asset.get("downloadUrl"))
        if target[0] == platform and target[2] == "zip":
            candidates[target[1]] = Release(version, notes, size, checksum.lower(), asset["downloadUrl"], target[1],
                                            name, asset.get("updateSignature"), platform)
    # Exact architecture first, then an explicitly declared universal app.
    release = candidates.get(architecture)
    if release is None and platform == "macos":
        release = candidates.get("universal")
    if release is not None:
        try:
            verify_signature(version, release.signed_asset())
        except SignatureError as error:
            raise UpdateError(str(error)) from None
    return version, release


def fetch_manifest():
    try:
        with open_public(UPDATE_MANIFEST_URL) as response:
            raw = response.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise UpdateError("更新清单过大。")
        manifest = json.loads(raw)
        if not isinstance(manifest, dict) or "schemaVersion" not in manifest:
            raise UpdateError("未读取到有效的更新清单。")
        return manifest
    except HTTPError as error:
        logger.warning("更新清单 HTTP 请求失败 status=%s", error.code)
        if error.code == 404:
            return None
        if error.code == 401:
            raise UpdateError("更新源暂不允许公开访问，请联系作者；无需登录或填写令牌。") from None
        if error.code in (403, 429):
            raise UpdateError("检查更新被限流或拒绝，请稍后重试。") from None
        raise UpdateError("版本服务器暂时不可用，请稍后重试。") from None
    except UpdateError:
        raise
    except Exception as error:
        reason = error.reason if isinstance(error, URLError) else error
        if isinstance(reason, ssl.SSLCertVerificationError):
            category = "update_tls_certificate"
            message = "无法验证更新服务器的 HTTPS 证书，请检查系统时间、代理证书或联系作者。"
        elif isinstance(reason, ssl.SSLError):
            category = "update_tls_connection"
            message = "无法与更新服务器建立安全连接，请检查网络或代理设置。"
        elif isinstance(reason, TimeoutError):
            category = "update_timeout"
            message = "连接更新服务器超时，请检查网络或代理设置后重试。"
        elif isinstance(reason, socket.gaierror):
            category = "update_dns"
            message = "无法解析更新服务器地址，请检查网络或代理设置。"
        elif isinstance(error, (json.JSONDecodeError, UnicodeError)):
            category = "update_manifest_json"
            message = "更新服务器返回的内容不是有效的 JSON 清单，请检查代理或联系作者。"
        elif isinstance(error, URLError):
            category = "update_connection"
            message = "无法连接 GitHub 更新服务器，请检查网络或代理设置后重试。"
        else:
            category = "update_unknown"
            message = "检查更新失败，请导出日志并联系作者。"
        # Fixed categories only: exception text may contain URLs/proxy secrets.
        logger.warning("更新清单读取失败 category=%s", category)
        raise UpdateError(message) from None


def download(release: Release, destination: Path, cancelled, progress):
    try:
        verify_signature(release.version, release.signed_asset())
    except SignatureError as error:
        raise UpdateError(str(error)) from None
    digest = hashlib.sha256()
    received = 0
    started = time.monotonic()
    try:
        with open_public(release.url) as response, destination.open("xb") as output:
            while True:
                if cancelled() or time.monotonic() - started > 30 * 60:
                    raise UpdateError("下载已取消或超时，请重试。")
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > release.size:
                    raise UpdateError("下载文件大于清单记录，已停止更新。")
                output.write(chunk)
                digest.update(chunk)
                progress(received, release.size)
        if received != release.size or digest.hexdigest() != release.sha256:
            raise UpdateError("更新包校验失败，请重新下载。")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
