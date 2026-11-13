# -*- coding: utf-8 -*-
"""云端预留接口测试：Chatwoot 目标地址解析、TLS 切换与环境变量配置。

背景：Chatwoot 通常先部署在内网（默认 http 主机见代码常量），后续会迁到
云端（https 域名）。这组用例验证切换接口的行为：内网默认用法零变化、
https 配置能正确生效、非法配置在启动期就被拒绝而不是带进运行期。
"""
import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("cloud_bot", ROOT / "chatwoot_test_bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

# 模块默认目标（代码常量），所有用例结束后必须恢复成它，避免污染其他测试模块。
DEFAULTS = ("http", "192.168.0.205", 3000)
FAKE_TOKEN = "token-1234567890abcdefghij"


class CloudTestCase(unittest.TestCase):
    def setUp(self):
        # 备份模块级目标配置与相关环境变量，统一恢复为默认内网值。
        self._saved_cfg = (bot.CHATWOOT_SCHEME, bot.CHATWOOT_HOST, bot.CHATWOOT_PORT)
        self._saved_env = {
            key: os.environ.pop(key, None)
            for key in ("CHATWOOT_SCHEME", "CHATWOOT_HOST", "CHATWOOT_PORT")
        }
        bot.CHATWOOT_SCHEME, bot.CHATWOOT_HOST, bot.CHATWOOT_PORT = DEFAULTS

    def tearDown(self):
        bot.CHATWOOT_SCHEME, bot.CHATWOOT_HOST, bot.CHATWOOT_PORT = self._saved_cfg
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class EndpointParsingTests(CloudTestCase):
    def test_host_only_keeps_scheme_and_default_port(self):
        # 只写 host：scheme 为 None（沿用现值），端口按 http 惯例默认 3000
        self.assertEqual(bot.parse_chatwoot_endpoint("10.0.0.9"), (None, "10.0.0.9", 3000))

    def test_host_with_port(self):
        self.assertEqual(bot.parse_chatwoot_endpoint("10.0.0.9:4000"), (None, "10.0.0.9", 4000))

    def test_https_url_defaults_to_443(self):
        # https 且未写端口：自动取 443，迁移云端无需手动补端口号
        self.assertEqual(
            bot.parse_chatwoot_endpoint("https://app.chatwoot.cloud"),
            ("https", "app.chatwoot.cloud", 443),
        )

    def test_https_url_with_port_and_trailing_slash(self):
        # 尾部斜杠是粘贴域名时的常见输入，应被容忍
        self.assertEqual(
            bot.parse_chatwoot_endpoint("https://app.chatwoot.cloud:8443/"),
            ("https", "app.chatwoot.cloud", 8443),
        )

    def test_invalid_values_raise(self):
        for bad in ("", "ftp://x", "host:oops", "https://", ":3000"):
            with self.assertRaises(ValueError):
                bot.parse_chatwoot_endpoint(bad)

    def test_apply_without_scheme_keeps_scheme(self):
        # 内网换 IP 场景：不带 scheme 只换地址端口，协议保持 http
        bot.apply_chatwoot_endpoint("10.0.0.9:4000")
        self.assertEqual(
            (bot.CHATWOOT_SCHEME, bot.CHATWOOT_HOST, bot.CHATWOOT_PORT),
            ("http", "10.0.0.9", 4000),
        )

    def test_apply_https_switches_scheme(self):
        bot.apply_chatwoot_endpoint("https://app.chatwoot.cloud")
        self.assertEqual(
            (bot.CHATWOOT_SCHEME, bot.CHATWOOT_HOST, bot.CHATWOOT_PORT),
            ("https", "app.chatwoot.cloud", 443),
        )


class EnvConfigTests(CloudTestCase):
    def test_full_env_points_to_cloud(self):
        # 云端迁移场景：三个环境变量整体指定 https 目标
        os.environ.update({
            "CHATWOOT_SCHEME": "https",
            "CHATWOOT_HOST": "app.chatwoot.cloud",
            "CHATWOOT_PORT": "443",
        })
        bot.apply_chatwoot_env()
        self.assertEqual(
            (bot.CHATWOOT_SCHEME, bot.CHATWOOT_HOST, bot.CHATWOOT_PORT),
            ("https", "app.chatwoot.cloud", 443),
        )

    def test_host_only_env_keeps_http_and_default_port(self):
        os.environ["CHATWOOT_HOST"] = "10.0.0.9"
        bot.apply_chatwoot_env()
        self.assertEqual(
            (bot.CHATWOOT_SCHEME, bot.CHATWOOT_HOST, bot.CHATWOOT_PORT),
            ("http", "10.0.0.9", 3000),
        )

    def test_no_env_keeps_defaults(self):
        # 未设任何环境变量时零变化——保证既有部署不受影响
        bot.apply_chatwoot_env()
        self.assertEqual((bot.CHATWOOT_SCHEME, bot.CHATWOOT_HOST, bot.CHATWOOT_PORT), DEFAULTS)

    def test_bad_port_env_exits_without_changing_config(self):
        # 非法端口必须在启动期报错，而不是悄悄带着错误端口进运行期
        os.environ["CHATWOOT_PORT"] = "oops"
        with self.assertRaises(SystemExit):
            bot.apply_chatwoot_env()
        self.assertEqual((bot.CHATWOOT_SCHEME, bot.CHATWOOT_HOST, bot.CHATWOOT_PORT), DEFAULTS)

    def test_bad_scheme_env_exits(self):
        os.environ["CHATWOOT_SCHEME"] = "ftp"
        with self.assertRaises(SystemExit):
            bot.apply_chatwoot_env()
        self.assertEqual((bot.CHATWOOT_SCHEME, bot.CHATWOOT_HOST, bot.CHATWOOT_PORT), DEFAULTS)


class ConnectionSwitchTests(CloudTestCase):
    def test_https_scheme_uses_tls_connection(self):
        # scheme=https 时必须走 HTTPSConnection（云端 TLS），且目标与超时正确传入
        bot.CHATWOOT_SCHEME, bot.CHATWOOT_HOST, bot.CHATWOOT_PORT = "https", "app.chatwoot.cloud", 443
        with patch.object(bot.http.client, "HTTPSConnection") as tls, patch.object(
            bot.http.client, "HTTPConnection"
        ) as plain:
            bot.chatwoot_request("GET", "conversations", FAKE_TOKEN)
        tls.assert_called_once_with("app.chatwoot.cloud", 443, timeout=bot.CHATWOOT_TIMEOUT_SECONDS)
        plain.assert_not_called()

    def test_http_scheme_keeps_plain_connection(self):
        # 默认 http 时保持原有明文连接，路径/参数与改造前一致
        bot.CHATWOOT_SCHEME, bot.CHATWOOT_HOST, bot.CHATWOOT_PORT = "http", "127.0.0.1", 3000
        with patch.object(bot.http.client, "HTTPConnection") as plain, patch.object(
            bot.http.client, "HTTPSConnection"
        ) as tls:
            bot.chatwoot_request("GET", "conversations", FAKE_TOKEN)
        plain.assert_called_once_with("127.0.0.1", 3000, timeout=bot.CHATWOOT_TIMEOUT_SECONDS)
        tls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
