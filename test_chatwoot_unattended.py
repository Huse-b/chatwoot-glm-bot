# -*- coding: utf-8 -*-
"""常驻（无人值守）运行相关测试：凭据来源优先级、DPAPI 加密落盘、日志落盘。

沿用项目测试风格：用临时 STATE_FILE 隔离，不触碰正式 sqlite，不使用真实令牌。
"""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("unattended_bot", ROOT / "chatwoot_test_bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

USER_ENV = "CHATWOOT_USER_TOKEN"
BOT_ENV = "CHATWOOT_BOT_TOKEN"


class UnattendedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self._state = bot.STATE_FILE
        bot.STATE_FILE = Path(self.temporary.name) / "bot.sqlite3"   # 凭据/日志文件随之落到临时目录
        # 清掉可能干扰的环境变量，记录原值以便还原
        self._saved_env = {key: os.environ.pop(key, None) for key in (USER_ENV, BOT_ENV)}

    def tearDown(self):
        # 先关闭日志 handler，否则 Windows 下临时目录里的 bot.log 被占用删不掉
        for handler in list(bot.logger.handlers):
            handler.close()
        bot.logger.handlers.clear()
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        bot.STATE_FILE = self._state
        self.temporary.cleanup()

    # —— 凭据来源优先级：环境变量 → DPAPI 文件 → 交互回退 ——
    def test_env_credentials_bypass_interactive(self):
        with patch.dict(os.environ, {USER_ENV: "env-user", BOT_ENV: "env-bot"}), \
                patch.object(bot.getpass, "getpass", side_effect=AssertionError("环境变量已提供令牌，不应再交互输入")):
            self.assertEqual(bot.resolve_credentials(), ("env-user", "env-bot"))

    def test_env_credentials_reject_dirty_token(self):
        # 环境变量里的脏令牌也要在联网前被 validate_access_token 拒绝
        with patch.dict(os.environ, {USER_ENV: "\x16bad", BOT_ENV: "env-bot"}):
            with self.assertRaises(SystemExit):
                bot.resolve_credentials()

    def test_partial_env_falls_through_to_interactive(self):
        # 只设一个环境变量视为配置不完整 → 回退交互输入
        with patch.dict(os.environ, {USER_ENV: "only-user"}), \
                patch.object(bot.getpass, "getpass", side_effect=["user-tok", "bot-tok"]) as getpass:
            self.assertEqual(bot.resolve_credentials(), ("user-tok", "bot-tok"))
        self.assertEqual(getpass.call_count, 2)

    def test_interactive_fallback_when_no_env_or_file(self):
        with patch.object(bot.getpass, "getpass", side_effect=["user-tok", "bot-tok"]) as getpass:
            self.assertEqual(bot.resolve_credentials(), ("user-tok", "bot-tok"))
        self.assertEqual(getpass.call_count, 2)

    def test_interactive_rejects_ctrl_v(self):
        with patch.object(bot.getpass, "getpass", return_value="\x16") as getpass:
            with self.assertRaisesRegex(SystemExit, "Ctrl\\+V"):
                bot.resolve_credentials()
        self.assertEqual(getpass.call_count, 1)

    # —— DPAPI 加密凭据文件（仅 Windows）——
    @unittest.skipUnless(sys.platform == "win32", "DPAPI 仅在 Windows 可用")
    def test_dpapi_roundtrip_and_no_plaintext(self):
        secret = "令牌-secret-123".encode("utf-8")
        cipher = bot._dpapi_transform(secret, protect=True)
        self.assertNotEqual(cipher, secret)                              # 密文 != 明文
        self.assertEqual(bot._dpapi_transform(cipher, protect=False), secret)  # 可还原

    @unittest.skipUnless(sys.platform == "win32", "DPAPI 仅在 Windows 可用")
    def test_save_then_load_and_precedence_over_interactive(self):
        with patch.object(bot.getpass, "getpass", side_effect=["saved-user", "saved-bot"]):
            bot.save_credentials()
        path = bot.credential_file()
        self.assertTrue(path.exists())
        self.assertNotIn(b"saved-user", path.read_bytes())               # 落盘是密文，不含明文令牌
        self.assertEqual(bot.load_stored_credentials(), ("saved-user", "saved-bot"))
        # 无环境变量时应使用加密文件，且不再交互输入
        with patch.object(bot.getpass, "getpass", side_effect=AssertionError("已有加密凭据，不应交互")):
            self.assertEqual(bot.resolve_credentials(), ("saved-user", "saved-bot"))

    @unittest.skipUnless(sys.platform == "win32", "DPAPI 仅在 Windows 可用")
    def test_env_takes_precedence_over_stored_file(self):
        with patch.object(bot.getpass, "getpass", side_effect=["saved-user", "saved-bot"]):
            bot.save_credentials()
        with patch.dict(os.environ, {USER_ENV: "env-user", BOT_ENV: "env-bot"}):
            self.assertEqual(bot.resolve_credentials(), ("env-user", "env-bot"))

    # —— 日志落盘（步骤2：监控可观测性）——
    def test_setup_logging_writes_to_rotating_file(self):
        bot.setup_logging()
        bot.log("心跳自检-ABC")
        for handler in bot.logger.handlers:
            handler.flush()
        self.assertIn("心跳自检-ABC", bot.log_file().read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
