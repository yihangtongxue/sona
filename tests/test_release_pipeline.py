"""Release transaction regression cases; GitHub calls are always mocked."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import github_release
from test_updates import TEST_PUBLIC, sign_asset


class ReleasePipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        key = patch("sona.updates.signatures.read_public_key", return_value=TEST_PUBLIC)
        key.start()
        self.addCleanup(key.stop)
        describe = patch("github_release.describe_asset", side_effect=self.describe)
        describe.start()
        self.addCleanup(describe.stop)
        environment = patch.dict(os.environ, {
            "GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "push",
            "GITHUB_REF": f"refs/tags/v{github_release.VERSION}", "GITHUB_REPOSITORY": github_release.REPOSITORY,
        })
        environment.start()
        self.addCleanup(environment.stop)
        # The metadata command writes Actions output; these tests never do.
        check = patch("github_release.check")
        check.start()
        self.addCleanup(check.stop)
        for name in github_release.expected_assets():
            path = self.directory / name
            path.write_bytes(name.encode())
            asset = sign_asset(github_release.VERSION, self.describe(path))
            (self.directory / (name + ".sig.json")).write_text(json.dumps(asset["updateSignature"]))

    def describe(self, path):
        platform, architecture, package = github_release.expected_assets()[path.name]
        content = path.read_bytes()
        return {"platform": platform, "architecture": architecture, "packageType": package,
                "fileName": path.name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}

    def github(self, *arguments):
        if "--paginate" in arguments:
            return "[]"
        if arguments[:2] == ("release", "view"):
            return json.dumps({"databaseId": 123, "isDraft": True})
        if arguments[:1] == ("api",):
            assets = []
            for path in self.directory.iterdir():
                if path.name == "release-notes.md":
                    continue
                assets.append({"name": path.name, "size": path.stat().st_size, "state": "uploaded",
                               "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()})
            return json.dumps({"assets": assets})
        return ""

    def test_missing_platform_stops_manifest_creation(self):
        (self.directory / f"Sona-{github_release.VERSION}-windows-x64.zip").unlink()
        with self.assertRaisesRegex(ValueError, "不齐全"):
            github_release.prepare(self.directory)
        self.assertFalse((self.directory / "stable.json").exists())

    def test_modified_artifact_cannot_enter_manifest(self):
        path = self.directory / next(iter(github_release.expected_assets()))
        path.write_bytes(b"tampered")
        with self.assertRaises(ValueError):
            github_release.prepare(self.directory)
        self.assertFalse((self.directory / "stable.json").exists())

    def test_all_assets_uploaded_before_release_becomes_public(self):
        github_release.prepare(self.directory)
        with patch("github_release.gh", side_effect=self.github) as gh:
            github_release.publish(self.directory)
        mutations = [call.args[:2] for call in gh.call_args_list
                     if call.args[0] == "release" and call.args[1] in {"create", "upload", "edit"}]
        self.assertEqual(mutations, [("release", "create"), ("release", "upload"), ("release", "edit")])
        self.assertIn("--draft", next(call.args for call in gh.call_args_list if call.args[:2] == ("release", "create")))

    def test_upload_failure_never_publishes_draft(self):
        github_release.prepare(self.directory)

        def github(*args):
            if args[:2] == ("release", "upload"):
                raise subprocess.CalledProcessError(1, "gh")
            return self.github(*args)

        with patch("github_release.gh", side_effect=github) as gh, self.assertRaises(subprocess.CalledProcessError):
            github_release.publish(self.directory)
        self.assertFalse(any(call.args[:2] == ("release", "edit") for call in gh.call_args_list))

    def test_existing_version_blocks_any_remote_mutation(self):
        github_release.prepare(self.directory)
        for release in ({"tag_name": "v99.0.0", "draft": False, "prerelease": False},
                        {"tag_name": f"v{github_release.VERSION}", "draft": True, "prerelease": False}):
            with self.subTest(release=release), patch("github_release.gh", return_value=json.dumps([[release]])) as gh:
                with self.assertRaises(ValueError):
                    github_release.publish(self.directory)
                self.assertEqual(gh.call_count, 1)

    def test_incomplete_remote_upload_stays_draft(self):
        github_release.prepare(self.directory)

        def github(*args):
            if args[:1] == ("api",) and "--paginate" not in args:
                return json.dumps({"assets": []})
            return self.github(*args)

        with patch("github_release.gh", side_effect=github) as gh, self.assertRaisesRegex(ValueError, "远端附件"):
            github_release.publish(self.directory)
        self.assertFalse(any(call.args[:2] == ("release", "edit") for call in gh.call_args_list))


if __name__ == "__main__":
    unittest.main()
