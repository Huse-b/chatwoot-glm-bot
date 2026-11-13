"""Offline integration tests using v4.19.1 REST response shapes and loopback HTTP."""

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("polling_bot", ROOT / "chatwoot_test_bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)
START = 1700000000
PREFIX = "/api/v1/accounts/1/conversations"


def message(message_id, conversation_id=10, **changes):
    return {
        "id": message_id, "conversation_id": conversation_id, "inbox_id": 7,
        "message_type": 0, "private": False, "content": "您好",
        "created_at": START + 1, **changes,
    }


def conversation(conversation_id, messages, **changes):
    return {
        "id": conversation_id, "inbox_id": 7, "status": "pending", "can_reply": True,
        "muted": False, "meta": {"channel": "Channel::WebWidget"},
        "messages": [messages[-1]], "last_non_activity_message": messages[-1],
        **changes,
    }


class FakeChatwoot(BaseHTTPRequestHandler):
    def date_time_string(self, timestamp=None):
        return formatdate(START, usegmt=True)

    def respond(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        state = self.server.state
        state["reads"].append(self.path)
        if self.headers.get("api-access-token") != "fake-user-token":
            return self.respond(401, {})
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == PREFIX:
            if state["list_status"] != 200:
                return self.respond(state["list_status"], {})
            if query.get("inbox_id") != ["7"] or query.get("status") != ["all"]:
                return self.respond(400, {})
            page = int(query["page"][0])
            rows = state["conversations"][(page - 1) * 2:page * 2]
            return self.respond(200, {"data": {"meta": {"all_count": len(state["conversations"])}, "payload": rows}})
        if parsed.path.startswith(PREFIX + "/") and parsed.path.endswith("/messages"):
            if state["messages_status"] != 200:
                return self.respond(state["messages_status"], {})
            conversation_id = int(parsed.path.split("/")[-2])
            after = int(query["after"][0])
            rows = [row for row in state["messages"][conversation_id] if row["id"] > after][:100]
            meta = {"assignee": {"id": 9}} if state["human_on_message_read"] else {}
            return self.respond(200, {"meta": meta, "payload": rows})
        self.respond(404, {})

    def do_POST(self):
        state = self.server.state
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("api-access-token") != "fake-bot-token":
            return self.respond(403, {})
        if not self.path.startswith(PREFIX + "/") or not self.path.endswith("/messages"):
            return self.respond(404, {})
        status = state["post_status"]
        state["posts"].append((self.path, body, status))
        if state["drop_post_response"]:
            self.close_connection = True
            return
        self.respond(status, {"id": 999999})

    def log_message(self, *_args):
        pass


class PollingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeChatwoot)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        bot.CHATWOOT_HOST = "127.0.0.1"
        bot.CHATWOOT_PORT = cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        bot.STATE_FILE = Path(self.temporary.name) / "bot.sqlite3"
        # 隔离环境变量凭据：宿主机或复用 shell 若残留 CHATWOOT_*_TOKEN，会让依赖交互输入的
        # 用例（如 ctrl_v 拒绝）走环境变量路径、跳过 getpass，从而进入真实轮询循环卡死。
        self._saved_env = {key: os.environ.pop(key, None) for key in ("CHATWOOT_USER_TOKEN", "CHATWOOT_BOT_TOKEN")}
        self.db = sqlite3.connect(bot.STATE_FILE)
        bot.initialize_polling(self.db)
        self.server.state = self.state = {
            "conversations": [], "messages": {}, "reads": [], "posts": [],
            "list_status": 200, "messages_status": 200, "post_status": 201,
            "human_on_message_read": False, "drop_post_response": False,
        }

    def tearDown(self):
        self.db.close()
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def add_conversation(self, conversation_id=10, messages=None, **changes):
        messages = messages if messages is not None else [message(101, conversation_id)]
        record = conversation(conversation_id, messages, **changes)
        self.state["conversations"].append(record)
        self.state["messages"][conversation_id] = messages
        return record

    def poll(self):
        bot.poll_once(self.db, 7, "fake-user-token", "fake-bot-token")

    def cursor(self, conversation_id=10):
        row = self.db.execute("SELECT message_id FROM poll_cursors WHERE inbox_id=7 AND conversation_id=?", (conversation_id,)).fetchone()
        return row[0] if row else None

    def claims(self):
        return [row[0] for row in self.db.execute("SELECT message_id FROM attempts ORDER BY message_id")]

    def test_rest_payload_numeric_incoming_and_bot_identity(self):
        self.add_conversation(meta={"channel": "Channel::WebWidget", "assignee_type": "AgentBot", "assignee": {"id": 3}})
        self.poll()
        self.assertEqual(self.state["posts"], [(PREFIX + "/10/messages", {
            "content": bot.REPLY, "message_type": "outgoing", "private": False,
        }, 201)])
        self.assertEqual(self.claims(), [101])
        self.assertEqual(self.cursor(), 101)
        self.poll()
        self.assertEqual(len(self.state["posts"]), 1)

    def test_history_baseline_and_restart(self):
        self.add_conversation(messages=[message(90, created_at=START - 100)])
        self.poll()
        self.assertEqual(self.cursor(), 90)
        self.assertFalse(self.state["posts"])
        self.db.close()
        self.db = sqlite3.connect(bot.STATE_FILE)
        bot.initialize_polling(self.db)
        self.assertEqual(bot.poll_started_at(self.db, 7, formatdate(START + 500, usegmt=True)), START)
        incoming = message(102, created_at=START + 20)
        self.state["messages"][10].append(incoming)
        self.state["conversations"][0].update(messages=[incoming], last_non_activity_message=incoming)
        self.poll()
        self.assertEqual(self.claims(), [102])

    def test_pagination_processes_every_conversation(self):
        for number in range(10, 15):
            self.add_conversation(number, [message(100 + number, number)])
        self.poll()
        self.assertEqual(len(self.state["posts"]), 5)
        self.assertTrue(any("page=3" in path for path in self.state["reads"]))

    def test_messages_backlog_uses_after_and_preserves_pages(self):
        rows = [message(number, content="普通问题") for number in range(1001, 1106)]
        rows[0]["content"] = "您好"
        rows[-1]["content"] = "你好"
        self.add_conversation(messages=rows)
        self.poll()
        self.assertEqual(self.cursor(), 1100)
        self.assertEqual(self.claims(), [1001])
        self.poll()
        self.assertEqual(self.cursor(), 1105)
        self.assertEqual(self.claims(), [1001, 1105])
        self.assertIn(PREFIX + "/10/messages?after=1100", self.state["reads"])

    def test_filters_each_message_instead_of_only_latest_summary(self):
        rows = [
            message(101, message_type=1), message(102, private=True),
            message(103, content="其他问题"), message(104, content=None),
            message(105, created_at=START), message(106, inbox_id=8),
            message(107, conversation_id=11), message(108, content=" 你好 "),
            message(109, message_type=2, content="Conversation updated"),
        ]
        self.add_conversation(messages=rows)
        self.poll()
        self.assertEqual(self.claims(), [108])
        self.assertEqual(self.cursor(), 109)

    def test_website_only(self):
        for changes in ({"inbox_id": 8}, {"meta": {"channel": "Channel::Email"}}):
            with self.subTest(changes=changes):
                self.state["conversations"] = []
                self.add_conversation(**changes)
                self.poll()
        self.assertFalse(self.state["posts"])

    def test_human_resolved_snoozed_muted_and_cannot_reply(self):
        for number, changes in enumerate((
            {"meta": {"channel": "Channel::WebWidget", "assignee_type": "User", "assignee": {"id": 9}}},
            {"meta": {"channel": "Channel::WebWidget", "assignee": {"id": 9}}},
            {"status": "resolved"}, {"status": "snoozed"}, {"muted": True}, {"can_reply": False},
        ), start=10):
            self.add_conversation(number, [message(number + 100, number)], **changes)
        self.poll()
        self.assertFalse(self.state["posts"])
        self.assertEqual(self.cursor(10), 110)

    def test_open_unassigned_conversation_can_reply(self):
        self.add_conversation(status="open")
        self.poll()
        self.assertEqual(len(self.state["posts"]), 1)

    def test_human_takeover_between_list_and_message_read(self):
        self.add_conversation()
        self.state["human_on_message_read"] = True
        self.poll()
        self.assertFalse(self.state["posts"])
        self.assertEqual(self.cursor(), 101)
        self.state["human_on_message_read"] = False
        self.poll()
        self.assertFalse(self.state["posts"])

    def test_read_failure_keeps_progress_and_recovers(self):
        self.add_conversation()
        self.state["messages_status"] = 503
        with self.assertRaisesRegex(RuntimeError, "503"):
            self.poll()
        self.assertIsNone(self.cursor())
        self.state["messages_status"] = 200
        self.poll()
        self.assertEqual(self.claims(), [101])

    def test_rate_limit_retries_current_message_only(self):
        self.add_conversation(messages=[message(100, content="其他问题"), message(101)])
        self.state["post_status"] = 429
        with self.assertRaisesRegex(RuntimeError, "429"):
            self.poll()
        self.assertEqual(self.cursor(), 100)
        self.assertEqual(self.claims(), [])
        self.state["post_status"] = 201
        self.poll()
        self.poll()
        self.assertEqual(self.claims(), [101])
        self.assertEqual([post[2] for post in self.state["posts"]], [429, 201])

    def test_ambiguous_post_retains_claim_without_duplicate(self):
        self.add_conversation()
        self.state["drop_post_response"] = True
        self.poll()
        self.assertEqual(self.claims(), [101])
        self.assertEqual(self.cursor(), 101)
        self.state["drop_post_response"] = False
        self.poll()
        self.assertEqual(len(self.state["posts"]), 1)

    def test_definite_connect_failure_releases_claim_for_retry(self):
        self.add_conversation()
        with patch.object(bot, "send_reply", side_effect=ConnectionRefusedError):
            with self.assertRaisesRegex(RuntimeError, "连接失败"):
                self.poll()
        self.assertFalse(self.claims())
        self.assertIsNone(self.cursor())
        self.poll()
        self.assertEqual(len(self.state["posts"]), 1)

    def test_authentication_errors_are_fatal_and_do_not_skip_message(self):
        self.add_conversation()
        with self.assertRaisesRegex(SystemExit, "401"):
            bot.poll_once(self.db, 7, "wrong-user-token", "fake-bot-token")
        self.assertIsNone(self.cursor())
        with self.assertRaisesRegex(SystemExit, "403"):
            bot.poll_once(self.db, 7, "fake-user-token", "wrong-bot-token")
        self.assertIsNone(self.cursor())
        self.assertFalse(self.claims())

    def test_server_write_error_is_not_blindly_retried(self):
        self.add_conversation()
        self.state["post_status"] = 500
        self.poll()
        self.state["post_status"] = 201
        self.poll()
        self.assertEqual(len(self.state["posts"]), 1)
        self.assertEqual(self.claims(), [101])

    def test_concurrent_workers_share_atomic_claim(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(bot.send_polled_reply, 101, 10, "fake-bot-token") for _ in range(2)]
            for future in futures:
                future.result()
        self.assertEqual(len(self.state["posts"]), 1)

    def test_polling_cli_bypasses_webhook_setup(self):
        with patch.object(sys, "argv", ["bot", "--polling", "--inbox", "7"]), \
                patch.object(bot, "poll_main") as polling, \
                patch.object(bot, "local_ip", side_effect=AssertionError("Should not locate webhook interface")), \
                patch.object(bot, "make_server", side_effect=AssertionError("Should not listen")):
            bot.main()
            polling.assert_called_once_with(7)

    def test_setup_and_polling_are_mutually_exclusive(self):
        with patch.object(sys, "argv", ["bot", "--polling", "--setup"]), patch("sys.stderr"), self.assertRaises(SystemExit) as error:
            bot.main()
        self.assertEqual(error.exception.code, 2)

    def test_invalid_token_characters_never_reach_http(self):
        for token in ("", "\x16", "\x16fake-token", "fake\r\ntoken", "fake token", "令牌", "fake\x00token"):
            with self.subTest(token_type="empty" if not token else "malformed"), \
                    patch.object(bot.http.client, "HTTPConnection") as connection, \
                    self.assertRaises(SystemExit):
                bot.chatwoot_request("GET", "conversations", token)
            connection.assert_not_called()

    def test_hidden_input_rejects_ctrl_v_before_second_prompt(self):
        with patch.object(bot.getpass, "getpass", return_value="\x16") as getpass, \
                patch.object(bot, "poll_once") as poll_once, self.assertRaisesRegex(SystemExit, "Ctrl\\+V"):
            bot.poll_main(7)
        self.assertEqual(getpass.call_count, 1)
        poll_once.assert_not_called()

    def test_http400_names_endpoint_and_stops_without_advancing_state(self):
        self.add_conversation()
        self.state["list_status"] = 400
        with self.assertRaisesRegex(SystemExit, "HTTP 400；接口 conversations\\?"):
            self.poll()
        self.assertIsNone(self.cursor())
        self.assertFalse(self.claims())

    def test_api_error_details_redact_token_before_truncating(self):
        token = "fake-private-token"
        detail = bot.api_error_detail(json.dumps({"error": "Bad parameter " + token, "errors": ["inbox_id invalid"], "access_token": "do-not-log"}).encode(), token)
        self.assertIn("Bad parameter [redacted]", detail)
        self.assertIn("inbox_id invalid", detail)
        self.assertNotIn(token, detail)
        self.assertNotIn("do-not-log", detail)
        self.assertLessEqual(len(bot.api_error_detail(json.dumps({"error": "x" * 1000}).encode(), token)), 240)
        self.assertNotIn("<html>", bot.api_error_detail(b"<html>Bad Request</html>", token))


if __name__ == "__main__":
    unittest.main(verbosity=2)
