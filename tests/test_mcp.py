"""MCP server, tools, stdio bridge and Codex config (Linux-runnable; no GUI, no real models)."""

import io
import json
import sqlite3
import stat
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from sona.audio_library import AudioLibrary
from sona.codex_config import codex_config
from sona.database import ModelRepository
from sona.mcp_bridge import Bridge, BridgeError, serve
from sona.mcp_server import MCPService, read_connection
from sona.mcp_tools import SonaTools
from sona.transcription.repository import TaskRepository

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}


def complete(database, identifier, text, segments=()):
    with sqlite3.connect(database) as db:
        db.execute("UPDATE transcription_tasks SET status='completed' WHERE audio_id=?", (identifier,))
        db.execute("""INSERT OR REPLACE INTO transcription_results
            (audio_id,text,segments_json,language,duration,model_id,engine,model_revision,device)
            VALUES (?,?,?,?,?,?,?,?,?)""", (identifier, text, json.dumps(list(segments), ensure_ascii=False),
                                             'zh', 75.0, 'whisper-small', 'mlx-whisper', 'r', 'Apple GPU'))


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.paths = Mock(database=self.root / "sona.sqlite3", data_dir=self.root)
        ModelRepository(self.paths.database, ())
        self.library = AudioLibrary(self.paths.database, self.root / "audio")
        self.addCleanup(self.library.close)
        self.podcasts = Mock()
        self.transcription = Mock()
        self.transcription.repository = TaskRepository(self.paths.database)
        self.tools = SonaTools(self.paths, self.library, self.podcasts, self.transcription, sleep=lambda _: None)

    def media(self, name="会议.m4a", data=b"fake audio bytes"):
        path = self.root / name
        path.write_bytes(data)
        return path


class SonaToolsTests(Fixture):
    def test_local_file_is_copied_queued_with_language_and_original_untouched(self):
        source = self.media()
        result = self.tools.transcribe(str(source), "EN")
        self.assertEqual(result["kind"], "file")
        self.assertEqual(result["language"], "en")
        self.assertEqual(source.read_bytes(), b"fake audio bytes")
        self.assertEqual(self.transcription.repository.language(result["task_id"]), "en")
        status = self.tools.get_status(result["task_id"])
        self.assertIn(status["state"], ("queued", "waiting_model"))
        self.assertFalse(status["done"])
        self.assertEqual(status["name"], "会议.m4a")

    def test_file_url_and_auto_language(self):
        result = self.tools.transcribe(self.media("a b.mp3").as_uri())
        self.assertEqual(result["language"], "auto")
        self.assertIsNone(self.transcription.repository.language(result["task_id"]))

    def test_invalid_sources_and_languages_are_rejected(self):
        for source, message in (("relative.mp3", "绝对路径"), (str(self.root / "missing.mp3"), "找不到"),
                                (str(self.media("notes.txt")), "支持的音频")):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, message):
                self.tools.transcribe(source)
        with self.assertRaisesRegex(ValueError, "不支持的识别语言"):
            self.tools.transcribe(str(self.media()), "klingon")
        self.assertEqual(self.library.list_files(), [])

    def test_link_uses_import_queue_and_existing_link_keeps_language(self):
        self.podcasts.create.return_value = {"id": "11111111-1111-4111-8111-111111111111", "existing": False}
        result = self.tools.transcribe("https://www.youtube.com/watch?v=abc", "ja", prefer_subtitles=False)
        self.podcasts.create.assert_called_once_with("https://www.youtube.com/watch?v=abc", "transcribe")
        self.assertEqual(self.transcription.repository.language(result["task_id"]), "ja")
        self.podcasts.create.return_value = {"id": "22222222-2222-4222-8222-222222222222", "existing": True}
        result = self.tools.transcribe("https://b23.tv/x", "en")
        self.assertTrue(result["existing"])
        self.assertIsNone(self.transcription.repository.language(result["task_id"]))

    def test_transcript_timestamps_pagination_and_search(self):
        identifier = self.tools.transcribe(str(self.media()))["task_id"]
        with self.assertRaisesRegex(ValueError, "尚未完成"):
            self.tools.get_transcript(identifier)
        segments = [{"start": 0, "end": 2, "text": "大家好"}, {"start": 3661.5, "end": 3663, "text": "量子计算的未来"}]
        complete(self.paths.database, identifier, "大家好量子计算的未来", segments)
        status = self.tools.get_status(identifier, wait_seconds=5)
        self.assertEqual((status["state"], status["done"], status["has_transcript"]), ("completed", True, True))
        plain = self.tools.get_transcript(identifier)
        self.assertEqual(plain["text"], "大家好量子计算的未来")
        self.assertIsNone(plain["next_offset"])
        timed = self.tools.get_transcript(identifier, timestamps=True)
        self.assertEqual(timed["text"], "[00:00:00] 大家好\n[01:01:01] 量子计算的未来")
        long_text = "长" * 2500
        complete(self.paths.database, identifier, long_text)
        page = self.tools.get_transcript(identifier, max_chars=1000)
        self.assertEqual((len(page["text"]), page["next_offset"], page["total_chars"]), (1000, 1000, 2500))
        last = self.tools.get_transcript(identifier, offset=2000, max_chars=1000)
        self.assertEqual((len(last["text"]), last["next_offset"]), (500, None))
        # The index follows result replacement: old text is gone, new text is found.
        complete(self.paths.database, identifier, "讨论量子计算和中文分词")
        found = self.tools.search("量子计算")["results"]
        self.assertEqual([item["task_id"] for item in found], [identifier])
        self.assertIn("量子计算", found[0]["snippet"])
        self.assertEqual(self.tools.search("分词")["results"][0]["id"], identifier)  # 2-char LIKE fallback
        self.assertEqual(self.tools.search("不存在的内容")["results"], [])
        self.library.delete_file(identifier)
        self.assertEqual(self.tools.search("量子计算")["results"], [])

    def test_status_long_poll_returns_on_timeout(self):
        identifier = self.tools.transcribe(str(self.media()))["task_id"]
        ticks = iter([0, 0, 10, 60, 60])
        tools = SonaTools(self.paths, self.library, self.podcasts, self.transcription,
                          clock=lambda: next(ticks), sleep=lambda _: None)
        self.assertFalse(tools.get_status(identifier, wait_seconds=999)["done"])
        with self.assertRaisesRegex(ValueError, "找不到任务"):
            self.tools.get_status("missing")

    def test_history_paginates_newest_first(self):
        ids = [self.tools.transcribe(str(self.media(f"{index}.wav")))["task_id"] for index in range(3)]
        page = self.tools.list_history(limit=2)
        self.assertEqual(page["total"], 3)
        self.assertEqual(page["next_offset"], 2)
        self.assertEqual({item["task_id"] for item in page["items"]} | {self.tools.list_history(2, 2)["items"][0]["task_id"]}, set(ids))
        self.assertEqual(len(self.tools.active_tasks()), 3)


class ServerAndBridgeTests(Fixture):
    def setUp(self):
        super().setUp()
        self.service = MCPService(self.root, self.tools)
        self.service.start()
        self.addCleanup(self.service.close)
        self.assertTrue(self.service.running, self.service.error)

    def post(self, message, token=None, host=None):
        record = read_connection(self.root)
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if host:
            headers["Host"] = host
        request = urllib.request.Request(f"http://127.0.0.1:{record['port']}/mcp", data=json.dumps(message).encode(),
                                         headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def test_connection_file_is_private_and_removed_on_close(self):
        path = self.root / "mcp.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        record = read_connection(self.root)
        self.assertEqual(record["token"], self.service.token)
        self.assertGreaterEqual(len(record["token"]), 40)
        self.service.close()
        self.assertFalse(path.exists())

    def test_requests_without_valid_token_are_rejected(self):
        for token in (None, "", "wrong", self.service.token + "x"):
            with self.subTest(token=token):
                status, body = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token)
                self.assertEqual(status, 401)
                self.assertNotIn(self.service.token.encode(), body)
        self.assertEqual(self.service.status()["request_count"], 0)

    def test_foreign_host_header_is_rejected_even_with_token(self):
        status, _ = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, self.service.token,
                              host="evil.example:80")
        self.assertIn(status, (400, 403, 421))

    def test_tools_are_annotated_read_only_or_write(self):
        status, body = self.post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, self.service.token)
        self.assertEqual(status, 200)
        tools = {tool["name"]: tool for tool in json.loads(body)["result"]["tools"]}
        self.assertEqual(set(tools), {"transcribe", "get_status", "get_transcript", "list_history", "search"})
        self.assertFalse(tools["transcribe"]["annotations"]["readOnlyHint"])
        self.assertFalse(tools["transcribe"]["annotations"]["destructiveHint"])
        for name in ("get_status", "get_transcript", "list_history", "search"):
            self.assertTrue(tools[name]["annotations"]["readOnlyHint"], name)

    def test_stdio_bridge_end_to_end(self):
        source = self.media("播客.mp3")
        messages = [INIT, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "transcribe", "arguments": {"source": str(source), "language": "zh"}}}]
        stdout = io.BytesIO()
        serve(Bridge(self.root), io.BytesIO(b"\n".join(json.dumps(m).encode() for m in messages)), stdout)
        responses = {item["id"]: item for item in map(json.loads, stdout.getvalue().splitlines())}
        self.assertEqual(set(responses), {1, 3})  # Notifications produce no output.
        self.assertEqual(responses[1]["result"]["serverInfo"]["name"], "sona")
        self.assertIn("transcribe", responses[1]["result"]["instructions"])
        payload = json.loads(responses[3]["result"]["content"][0]["text"])
        self.assertEqual(payload["kind"], "file")
        self.assertEqual(self.transcription.repository.language(payload["task_id"]), "zh")
        self.assertGreaterEqual(self.service.status()["request_count"], 2)

    def test_tool_errors_are_reported_to_the_client(self):
        status, body = self.post({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                  "params": {"name": "get_status", "arguments": {"task_id": "nope"}}},
                                 self.service.token)
        result = json.loads(body)["result"]
        self.assertTrue(result["isError"])
        self.assertIn("找不到任务", result["content"][0]["text"])


class BridgeConnectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Neither a client connection nor a request retry may start the GUI.
        process_patch = patch("subprocess.Popen")
        self.process = process_patch.start()
        self.addCleanup(process_patch.stop)
        self.addCleanup(self.process.assert_not_called)

    def run_bridge(self, bridge, message=INIT):
        stdout = io.BytesIO()
        serve(bridge, io.BytesIO(json.dumps(message).encode() + b"\n"), stdout)
        return json.loads(stdout.getvalue())

    def test_unavailable_app_returns_error_for_initialization_discovery_and_tool_calls(self):
        messages = [INIT, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "list_history", "arguments": {}}}]
        for message in messages:
            with self.subTest(method=message["method"]):
                response = self.run_bridge(Bridge(self.root), message)
                self.assertEqual(response["id"], message["id"])
                self.assertIn("请手动打开 Sona", response["error"]["message"])

    def test_stale_connection_file_returns_error(self):
        (self.root / "mcp.json").write_text(json.dumps({"port": 1, "token": "t", "pid": 1}))
        with patch("sona.mcp_bridge._port_open", return_value=False), self.assertRaisesRegex(BridgeError, "请手动打开"):
            Bridge(self.root).ensure_running()

    def test_app_exiting_during_request_returns_error_and_manual_reopen_recovers(self):
        opener = Mock(side_effect=urllib.error.URLError("connection closed"))
        bridge = Bridge(self.root, opener=opener)
        record = {"port": 12345, "token": "old"}
        with patch.object(bridge, "_connection", side_effect=[record, None]):
            self.assertIn("请手动打开", self.run_bridge(bridge)["error"]["message"])
        opener.assert_called_once()

        response = Mock(status=200, headers={"Content-Type": "application/json"})
        response.read.return_value = b'{"jsonrpc":"2.0","id":1,"result":{}}'
        opener.side_effect = None
        opener.return_value.__enter__ = Mock(return_value=response)
        opener.return_value.__exit__ = Mock(return_value=False)
        with patch.object(bridge, "_connection", return_value={"port": 12346, "token": "new"}):
            self.assertEqual(self.run_bridge(bridge)["result"], {})
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:12346/mcp")
        self.assertEqual(request.get_header("Authorization"), "Bearer new")

    def test_notifications_after_exit_produce_no_response(self):
        stdout = io.BytesIO()
        serve(Bridge(self.root), io.BytesIO(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'), stdout)
        self.assertEqual(stdout.getvalue(), b"")

    def test_invalid_json_gets_parse_error(self):
        stdout = io.BytesIO()
        serve(Bridge(self.root), io.BytesIO(b"{not json\n"), stdout)
        self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], -32700)


class CodexConfigTests(unittest.TestCase):
    def test_frozen_app_snippet_has_no_token(self):
        config = codex_config("/Applications/Sona.app/Contents/MacOS/Sona", frozen=True, environ={})
        self.assertEqual(config["snippet"], '[mcp_servers.sona]\ncommand = "/Applications/Sona.app/Contents/MacOS/Sona"\n'
                         'args = ["mcp"]\nstartup_timeout_sec = 60\ntool_timeout_sec = 120\n')
        self.assertEqual(config["warning"], "")
        self.assertNotIn("token", config["snippet"].lower())

    def test_snippet_is_valid_toml_and_warns_about_translocation(self):
        import tomllib

        config = codex_config('/private/var/folders/x/AppTranslocation/Y/d/Sona.app/Contents/MacOS/Sona', frozen=True,
                              environ={})
        self.assertIn("应用程序", config["warning"])
        source = codex_config('/Users/me/my "env"/bin/python', frozen=False,
                              environ={"SONA_ENV": "development", "SONA_DATA_DIR": "/tmp/d"})
        parsed = tomllib.loads(source["snippet"])["mcp_servers"]["sona"]
        self.assertEqual(parsed["command"], '/Users/me/my "env"/bin/python')
        self.assertEqual(parsed["args"], ["-m", "sona", "mcp"])
        self.assertEqual(parsed["env"], {"SONA_ENV": "development", "SONA_DATA_DIR": "/tmp/d"})


if __name__ == "__main__":
    unittest.main()
