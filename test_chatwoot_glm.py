# -*- coding: utf-8 -*-
"""GLM（智谱开放平台）多轮生成回复相关函数的离线单元测试：全部打桩，不发真实网络请求、不使用真实密钥。

覆盖：
- llm_generate_reply：请求构造（HTTPS/主机/路径/方法/Authorization 头/模型/消息体）、
  正常解析、非 200、网络异常、坏 JSON、空内容 → None；以及密钥绝不进日志。
- send_reply：动态 content 参数与默认固定 REPLY 向后兼容。
- resolve_llm_key / save_llm_key：环境变量优先级、DPAPI 加密往返（不落明文）。
"""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("glm_bot", ROOT / "chatwoot_test_bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)


class _FakeResponse:
    """伪造 http.client 响应对象：只提供 status 与 read()。"""

    def __init__(self, status, body):
        self.status = status
        self._body = body.encode("utf-8") if isinstance(body, str) else body

    def read(self):
        return self._body


class LlmClientTests(unittest.TestCase):
    def setUp(self):
        # 隔离环境变量，避免本机真实 GLM_API_KEY 干扰（即便有也不影响：本组测试全部显式传 key）。
        self._saved = {k: os.environ.pop(k, None) for k in (bot.LLM_API_KEY_ENV,)}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    @staticmethod
    def _ok(text):
        return _FakeResponse(200, json.dumps({"choices": [{"message": {"content": text}}]}))

    def test_builds_openai_request_and_returns_content(self):
        history = [{"role": "system", "content": "s"}, {"role": "user", "content": "怎么报价"}]
        with patch.object(bot.http.client, "HTTPSConnection") as conn_cls:
            conn = conn_cls.return_value
            conn.getresponse.return_value = self._ok("您好，报价取决于模块数量。")
            out = bot.llm_generate_reply(history, "test-key-123")
        self.assertEqual(out, "您好，报价取决于模块数量。")
        conn_cls.assert_called_once_with(bot.LLM_HOST, 443, timeout=bot.LLM_TIMEOUT_SECONDS)
        method, path, body, headers = conn.request.call_args[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, bot.LLM_PATH)
        self.assertEqual(headers["Authorization"], "Bearer test-key-123")
        payload = json.loads(body)
        self.assertEqual(payload["model"], bot.LLM_MODEL)
        self.assertEqual(payload["messages"], history)

    def test_non_200_returns_none(self):
        with patch.object(bot.http.client, "HTTPSConnection") as conn_cls:
            conn_cls.return_value.getresponse.return_value = _FakeResponse(401, '{"error":"unauthorized"}')
            self.assertIsNone(bot.llm_generate_reply([{"role": "user", "content": "x"}], "bad-key"))

    def test_network_error_returns_none(self):
        with patch.object(bot.http.client, "HTTPSConnection") as conn_cls:
            conn_cls.side_effect = OSError("boom")
            self.assertIsNone(bot.llm_generate_reply([{"role": "user", "content": "x"}], "k"))

    def test_malformed_body_returns_none(self):
        with patch.object(bot.http.client, "HTTPSConnection") as conn_cls:
            conn_cls.return_value.getresponse.return_value = _FakeResponse(200, "not json")
            self.assertIsNone(bot.llm_generate_reply([{"role": "user", "content": "x"}], "k"))

    def test_empty_content_returns_none(self):
        with patch.object(bot.http.client, "HTTPSConnection") as conn_cls:
            conn_cls.return_value.getresponse.return_value = self._ok("   ")
            self.assertIsNone(bot.llm_generate_reply([{"role": "user", "content": "x"}], "k"))

    def test_key_not_leaked_in_logs(self):
        records = []
        with patch.object(bot, "log", side_effect=lambda m: records.append(m)):
            with patch.object(bot.http.client, "HTTPSConnection") as conn_cls:
                conn_cls.side_effect = OSError("net")
                bot.llm_generate_reply([{"role": "user", "content": "hi"}], "SECRET-KEY-999")
        self.assertTrue(records)  # 确实记录了失败日志
        self.assertTrue(all("SECRET-KEY-999" not in r for r in records))


class SendReplyContentTests(unittest.TestCase):
    def test_send_reply_posts_given_content(self):
        with patch.object(bot, "chatwoot_request", return_value=(201, b"{}", None)) as req:
            status = bot.send_reply(55, "bot-token", content="动态回复文本")
        self.assertEqual(status, 201)
        method, route, token, payload = req.call_args[0]
        self.assertEqual((method, route, token), ("POST", "conversations/55/messages", "bot-token"))
        self.assertEqual(payload["content"], "动态回复文本")
        self.assertEqual(payload["message_type"], "outgoing")
        self.assertFalse(payload["private"])

    def test_send_reply_default_content_is_fixed_reply(self):
        with patch.object(bot, "chatwoot_request", return_value=(201, b"{}", None)) as req:
            bot.send_reply(55, "bot-token")
        self.assertEqual(req.call_args[0][3]["content"], bot.REPLY)


class LlmKeyResolutionTests(unittest.TestCase):
    def setUp(self):
        # STATE_FILE 指到临时目录：credential_file() 由它派生，测试间完全隔离、不碰真实凭据。
        self.tmp = tempfile.TemporaryDirectory()
        self._old_state = bot.STATE_FILE
        bot.STATE_FILE = Path(self.tmp.name) / "bot.sqlite3"
        self._saved = {k: os.environ.pop(k, None) for k in (bot.LLM_API_KEY_ENV,)}

    def tearDown(self):
        bot.STATE_FILE = self._old_state
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_no_env_no_file_returns_none(self):
        self.assertIsNone(bot.resolve_llm_key())

    def test_env_takes_precedence_over_nothing(self):
        os.environ[bot.LLM_API_KEY_ENV] = "env-key-1"
        self.assertEqual(bot.resolve_llm_key(), "env-key-1")

    @unittest.skipUnless(bot._dpapi_available(), "DPAPI 仅在 Windows 可用")
    def test_save_llm_key_roundtrip_and_precedence(self):
        # 模拟密钥用合规长度（≥20 字符，形如 id.secret），过短的会被形状校验拒收。
        with patch.object(bot.getpass, "getpass", return_value="1234567890abcdef.secret1234567890"):
            bot.save_llm_key()
        # 磁盘上不应含明文密钥
        self.assertNotIn(b"1234567890abcdef.secret1234567890", bot.credential_file().read_bytes())
        # 解密能拿回来
        self.assertEqual(bot.resolve_llm_key(), "1234567890abcdef.secret1234567890")
        # 环境变量优先级更高
        os.environ[bot.LLM_API_KEY_ENV] = "env-key-2"
        self.assertEqual(bot.resolve_llm_key(), "env-key-2")

    @unittest.skipUnless(bot._dpapi_available(), "DPAPI 仅在 Windows 可用")
    def test_save_llm_key_rejects_short_or_malformed_key(self):
        # 隐藏输入粘贴失败时可能只录到零星字符；形状校验应当场拒绝，避免带坏密钥进运行期表现为 401。
        for bad in ("a", "too-short", "has space inside", "非ascii的密钥"):
            with self.subTest(bad=bad), \
                    patch.object(bot.getpass, "getpass", return_value=bad), \
                    self.assertRaisesRegex(SystemExit, "形状异常"):
                bot.save_llm_key()


if __name__ == "__main__":
    unittest.main()
