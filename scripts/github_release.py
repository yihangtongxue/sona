"""Validate, prepare and publish complete GitHub Releases. Never creates tags."""

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

from release_support import PUBLIC_KEY, ROOT, check_version
from release_assets import describe_asset, expected_assets
from sona.updates.protocol import parse_manifest, version_tuple
from sona.updates.signatures import read_public_key, verify_signature
from sona.version import RELEASE_REPOSITORY, VERSION

REPOSITORY = RELEASE_REPOSITORY.removeprefix("https://github.com/")


def gh(*arguments):
    return subprocess.run(["gh", *arguments], check=True, text=True, capture_output=True).stdout


def check():
    check_version(VERSION)
    read_public_key(PUBLIC_KEY)
    if os.environ.get("GITHUB_EVENT_NAME") == "push":
        if os.environ.get("GITHUB_REF") != f"refs/tags/v{VERSION}":
            raise ValueError("推送标签必须与代码版本一致，例如 v1.0.0。")
        if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
            raise ValueError("发布仓库与客户端固定更新源不一致。")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as stream:
            stream.write(f"version={VERSION}\n")
    print(f"发行版本：{VERSION}；更新源：{RELEASE_REPOSITORY}")


def prepare(directory):
    check_version(VERSION)
    actual = {path.name for path in directory.iterdir() if path.suffix in {".zip", ".dmg", ".exe"}}
    if actual != set(expected_assets()):
        raise ValueError("双平台产物不齐全或包含额外版本，停止发布。")
    assets = []
    for name in expected_assets():
        path = directory / name
        asset = describe_asset(path)
        sidecar = json.loads(path.with_name(name + ".sig.json").read_text(encoding="utf-8"))
        asset["updateSignature"] = {key: sidecar[key] for key in ("algorithm", "keyId", "payload", "signature")}
        verify_signature(VERSION, asset)
        asset["downloadUrl"] = f"{RELEASE_REPOSITORY}/releases/download/v{VERSION}/{quote(name)}"
        assets.append(asset)
    notes_file = ROOT / f"docs/releases/{VERSION}.md"
    notes = notes_file.read_text(encoding="utf-8") if notes_file.is_file() else f"Sona {VERSION}，包含 Windows x64 和 Apple 芯片 Mac 安装包。"
    if len(notes) > 20000:
        raise ValueError("更新说明超过客户端长度限制。")
    manifest = {"schemaVersion": 1, "channel": "stable", "version": VERSION, "tag": f"v{VERSION}",
                "notes": notes, "assets": assets}
    for platform, architecture in (("macos", "arm64"), ("windows", "x64")):
        _, release = parse_manifest(manifest, architecture, platform=platform)
        if release is None:
            raise ValueError("更新清单缺少对应平台的更新包。")
    for name, content in (("stable.json", json.dumps(manifest, ensure_ascii=False, indent=2)),
                          ("SHA256SUMS.txt", "".join(f"{asset['sha256']}  {asset['fileName']}\n" for asset in assets)),
                          ("release-notes.md", notes)):
        with (directory / name).open("x", encoding="utf-8") as stream:
            stream.write(content)


def publish(directory):
    check()
    if os.environ.get("GITHUB_EVENT_NAME") != "push" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("自动发布只能由 GitHub Actions 的正式版本标签触发。")
    # A later rerun or out-of-order tag must never change the latest release backwards.
    pages = json.loads(gh("api", "--paginate", "--slurp", f"repos/{REPOSITORY}/releases?per_page=100"))
    existing = None
    for release in (item for page in pages for item in page):
        tag = release.get("tag_name", "")
        if tag == f"v{VERSION}":
            existing = release
        if not release["draft"] and not release["prerelease"] and re.fullmatch(r"v\d+\.\d+\.\d+", tag):
            if version_tuple(tag[1:]) >= version_tuple(VERSION):
                raise ValueError("已存在相同或更高的正式版本，不覆盖或倒退 latest。")
    if existing is not None:
        raise ValueError("此标签已有 Release 草稿，请核对并删除失败草稿后再重跑；不会覆盖已有附件。")
    # Recheck signed hashes immediately before any remote mutation.
    manifest = json.loads((directory / "stable.json").read_text(encoding="utf-8"))
    if (len(manifest.get("assets", [])) != len(expected_assets())
            or {asset.get("fileName") for asset in manifest["assets"]} != set(expected_assets())):
        raise ValueError("发布清单必须完整包含当前版本的四个安装包。")
    for platform, architecture in (("macos", "arm64"), ("windows", "x64")):
        _, release = parse_manifest(manifest, architecture, platform=platform)
        if release is None or release.version != VERSION:
            raise ValueError("发布清单版本或平台不匹配。")
    for asset in manifest["assets"]:
        local = describe_asset(directory / asset["fileName"])
        if any(asset.get(key) != value for key, value in local.items()):
            raise ValueError("安装包在签名后发生变化。")
        if asset.get("downloadUrl") != f"{RELEASE_REPOSITORY}/releases/download/v{VERSION}/{quote(asset['fileName'])}":
            raise ValueError("更新包下载地址必须指向当前 GitHub Release。")
        verify_signature(VERSION, asset)
    names = [*expected_assets(), *(name + ".sig.json" for name in expected_assets()), "stable.json", "SHA256SUMS.txt"]
    gh("release", "create", f"v{VERSION}", "--repo", REPOSITORY, "--verify-tag", "--draft",
       "--title", f"Sona {VERSION}", "--notes-file", str(directory / "release-notes.md"))
    draft = json.loads(gh("release", "view", f"v{VERSION}", "--repo", REPOSITORY, "--json", "databaseId,isDraft"))
    if not draft.get("isDraft") or type(draft.get("databaseId")) is not int:
        raise ValueError("未取得新建 Release 草稿，停止上传。")
    gh("release", "upload", f"v{VERSION}", "--repo", REPOSITORY, *(str(directory / name) for name in names))
    # Use the numeric ID: the public 'by tag' endpoint is for published releases.
    uploaded = json.loads(gh("api", f"repos/{REPOSITORY}/releases/{draft['databaseId']}"))["assets"]
    remote = {asset["name"]: asset for asset in uploaded}
    if len(uploaded) != len(names) or set(remote) != set(names):
        raise ValueError("远端附件不完整，Release 保持草稿状态。")
    for name in names:
        with (directory / name).open("rb") as stream:
            digest = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
        if (remote[name]["size"] != (directory / name).stat().st_size
                or remote[name].get("state") != "uploaded" or remote[name].get("digest") != digest):
            raise ValueError("远端附件大小或哈希不匹配，Release 保持草稿状态。")
    gh("release", "edit", f"v{VERSION}", "--repo", REPOSITORY, "--draft=false", "--latest")
    print(f"已发布：{RELEASE_REPOSITORY}/releases/tag/v{VERSION}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "prepare", "publish"))
    parser.add_argument("--directory", type=Path, default=ROOT / "release-assets")
    args = parser.parse_args()
    if args.command == "check":
        check()
    elif args.command == "prepare":
        prepare(args.directory)
    else:
        publish(args.directory)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError:
        raise SystemExit("GitHub 操作失败；检查权限、网络及 Release 草稿状态后重试。") from None
    except (ValueError, OSError, KeyError) as error:
        raise SystemExit(f"发布未完成：{error}") from None
