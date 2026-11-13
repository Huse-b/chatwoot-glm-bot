# -*- coding: utf-8 -*-
"""GLM 轮询接线（双模式）的离线集成测试：loopback HTTP + 全打桩，不访问真实 Chatwoot/GLM。

复用 test_chatwoot_polling 的 FakeChatwoot（补一个能力：messages 接口允许不带 after 的
GET——build_llm_history 拉最近一页就是这种请求）。覆盖：
- 有密钥：任意非空公开访客消息 → 拉会话最近一页拼上下文 → llm_generate_reply 生成 → POST 动态内容；
  生成失败回退兜底文案；私有/空白消息不回复；上下文 = system + 最近 N 条 user/assistant，超限截断。
- 无密钥：非问候消息不回复、问候语走固定 REPLY（旧行为回归，保证未配密钥部署不受影响）。
"""
import importlib.util
import os
import sqlite3
import tempfile
import unittest
from email.utils import formatdate
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from test_chatwoot_polling import FakeChatwoot, PREFIX, START, conversation, message

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("glm_polling_bot", ROOT / "chatwoot_test_bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)


class GlmFakeChatwoot(FakeChatwoot):
    def do_GET(self):
        # build_llm_history 用不带 after 的 messages GET 拉最近一页；补 after=0 复用父类逻辑。
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        if parsed.path.startswith(PREFIX + "/") and parsed.path.endswith("/messages") and "after" not in query:
            self.path = parsed.path + "?after=0"
        return super().do_GET()


class GlmPollingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), GlmFakeChatwoot)
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
        # 隔离凭据/密钥/提示词环境变量，防止宿主机残留值改变测试路径。
        self._saved_env = {key: os.environ.pop(key, None) for key in ("CHATWOOT_USER_TOKEN", "CHATWOOT_BOT_TOKEN", bot.LLM_API_KEY_ENV, bot.LLM_SYSTEM_PROMPT_ENV)}
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

    def poll(self, llm_key=None):
        bot.poll_once(self.db, 7, "fake-user-token", "fake-bot-token", llm_key)

    def cursor(self, conversation_id=10):
        row = self.db.execute("SELECT message_id FROM poll_cursors WHERE inbox_id=7 AND conversation_id=?", (conversation_id,)).fetchone()
        return row[0] if row else None

    def claims(self):
        return [row[0] for row in self.db.execute("SELECT message_id FROM attempts ORDER BY message_id")]

    def test_llm_replies_to_any_public_message(self):
        self.add_conversation(messages=[message(101, content="请问 Odoo 实施怎么收费？")])
        with patch.object(bot, "llm_generate_reply", return_value="您好，实施费用取决于所选模块。") as generate:
            self.poll("k1")
        self.assertEqual(self.state["posts"], [(PREFIX + "/10/messages", {
            "content": "您好，实施费用取决于所选模块。", "message_type": "outgoing", "private": False,
        }, 201)])
        self.assertEqual(generate.call_args[0][1], "k1")   # 密钥原样传给生成函数
        self.assertEqual(self.claims(), [101])
        self.assertEqual(self.cursor(), 101)

    def test_llm_failure_uses_fallback_text(self):
        self.add_conversation(messages=[message(101, content="报价多少")])
        with patch.object(bot, "llm_generate_reply", return_value=None):
            self.poll("k1")
        self.assertEqual([post[1]["content"] for post in self.state["posts"]], [bot.LLM_FALLBACK])
        self.assertEqual(self.claims(), [101])

    def test_history_built_from_recent_page(self):
        rows = [
            message(101, message_type=1, content="您好，我是 Odoo 客服助手"),
            message(102, content="请问报价"),
        ]
        self.add_conversation(messages=rows)
        expected = [
            {"role": "system", "content": bot.LLM_SYSTEM_PROMPT},
            {"role": "assistant", "content": "您好，我是 Odoo 客服助手"},
            {"role": "user", "content": "请问报价"},
        ]
        with patch.object(bot, "llm_generate_reply", return_value="报价说明") as generate:
            self.poll("k1")
        self.assertEqual(generate.call_args[0][0], expected)   # 上下文含 system 与多轮消息
        # 无 after 的最近一页请求确实发出过（子类会补 after=0 转交父类处理）
        self.assertIn(PREFIX + "/10/messages?after=0", self.state["reads"])

    def test_system_prompt_env_override_and_default(self):
        # 环境变量注入自定义提示词时优先使用；未设置/空白时回退代码默认值。
        self.assertEqual(bot.resolve_system_prompt(), bot.LLM_SYSTEM_PROMPT)
        os.environ[bot.LLM_SYSTEM_PROMPT_ENV] = "你是测试店铺客服"
        try:
            self.assertEqual(bot.resolve_system_prompt(), "你是测试店铺客服")
        finally:
            os.environ.pop(bot.LLM_SYSTEM_PROMPT_ENV, None)
        os.environ[bot.LLM_SYSTEM_PROMPT_ENV] = "   "
        try:
            self.assertEqual(bot.resolve_system_prompt(), bot.LLM_SYSTEM_PROMPT)
        finally:
            os.environ.pop(bot.LLM_SYSTEM_PROMPT_ENV, None)

    def test_history_truncates_to_context_limit(self):
        rows = [message(101 + i, content=f"第{i}句", message_type=1 if i == 1 else 0) for i in range(6)]
        self.state["messages"][10] = rows
        old_limit = bot.LLM_CONTEXT_LIMIT
        bot.LLM_CONTEXT_LIMIT = 3
        try:
            history = bot.build_llm_history(10, "fake-user-token")
        finally:
            bot.LLM_CONTEXT_LIMIT = old_limit
        self.assertEqual(history[0]["role"], "system")
        self.assertEqual([turn["role"] for turn in history[1:]], ["user", "user", "user"])   # 只留最近 3 条
        self.assertEqual([turn["content"] for turn in history[1:]], ["第3句", "第4句", "第5句"])

    def test_no_key_skips_non_greeting_keeps_legacy_reply(self):
        # 无密钥：非问候消息一律不回复（旧行为）；问候语仍回固定文案。
        rows = [message(101, content="请问报价"), message(102, content="有其他选择吗")]
        self.add_conversation(messages=rows)
        with patch.object(bot, "llm_generate_reply", side_effect=AssertionError("无密钥时不应调用 GLM")):
            self.poll(None)
        self.assertFalse(self.state["posts"])
        self.assertEqual(self.claims(), [])
        self.assertEqual(self.cursor(), 102)
        rows.append(message(103, content="你好"))
        self.state["conversations"][0].update(messages=[rows[-1]], last_non_activity_message=rows[-1])
        self.poll(None)
        self.assertEqual([post[1]["content"] for post in self.state["posts"]], [bot.REPLY])
        self.assertEqual(self.claims(), [103])

    def test_private_and_blank_not_replied_with_key(self):
        rows = [message(101, private=True), message(102, content="   "), message(103, content="真问题")]
        self.add_conversation(messages=rows)
        with patch.object(bot, "llm_generate_reply", return_value="ok") as generate:
            self.poll("k1")
        self.assertEqual(generate.call_count, 1)   # 只有 103 触发生成
        self.assertEqual(self.claims(), [103])
        self.assertEqual(self.cursor(), 103)


if __name__ == "__main__":
    unittest.main(verbosity=2)
