#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 macOS 服务器上从 stdin 读取 JSON 凭据并部署 launchd 常驻服务（不打印、不落明文日志）。

与交互录入版的区别：凭据来自管道而不是交互录入，便于从 Windows 一条命令完成迁移
（见 migrate_creds_205.py）。stdin 格式：{"user": ..., "bot": ..., "glm": ...}
迁移后更新凭据：在本机重新录入（--save-credentials / --save-llm-key）后重跑迁移脚本。
"""
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

APP = Path.home() / "chatwoot-odoo-bot"
BOT = APP / "chatwoot_test_bot.py"
PLIST = Path.home() / "Library" / "LaunchAgents" / "com.chatwoot.odoo.poller.plist"
LOG = APP / "launchd.log"
LABEL = "com.chatwoot.odoo.poller"
# 官网浮窗所在收件箱 ID，按你的 Chatwoot 实例修改（后台 设置 → 收件箱 列表里查看）
INBOX_ID = "7"


def main():
    if not BOT.exists():
        raise SystemExit(f"未找到 {BOT}，请先同步机器人文件。")
    # 凭据从 stdin 读入（经 SSH 加密通道），不经过终端回显、不写任何明文文件
    try:
        record = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (ValueError, UnicodeError):
        raise SystemExit("传入的凭据不是合法 JSON。")
    values = {"user": record.get("user"), "bot": record.get("bot"), "glm": record.get("glm")}
    # 形状校验：迁移途中拿到截断值就中止，而不是带着坏凭据进运行期表现为 401
    for name, value in values.items():
        if not isinstance(value, str) or len(value) < 20 or not value.isascii() or any(ch.isspace() for ch in value):
            raise SystemExit(f"凭据 {name} 缺失或形状异常（过短/含空白/非 ASCII），迁移已中止。")
    print("收到 3 项凭据，形状校验通过（内容不显示）。")

    config = {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/python3", str(BOT), "--polling", "--inbox", INBOX_ID],
        # macOS 无 DPAPI，凭据以环境变量注入进程；plist 已 chmod 600 仅本用户可读
        "EnvironmentVariables": {
            "CHATWOOT_USER_TOKEN": values["user"],
            "CHATWOOT_BOT_TOKEN": values["bot"],
            "GLM_API_KEY": values["glm"],
        },
        "RunAtLoad": True,     # 登录后自动启动
        "KeepAlive": True,     # 进程退出（含崩溃）后 launchd 立即拉起
        "StandardOutPath": str(LOG),
        "StandardErrPath": str(LOG),
    }
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_bytes(plistlib.dumps(config))
    os.chmod(PLIST, 0o600)
    # 重新加载：先卸载旧的（不存在时忽略），再加载新的
    subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
    result = subprocess.run(["launchctl", "load", str(PLIST)], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"launchctl load 失败: {result.stderr.strip()}")
    print(f"部署完成：{LABEL}（RunAtLoad + KeepAlive，开机自启、崩溃自动拉起）")
    print(f"日志：{LOG} 与 {APP}/chatwoot_test_bot.log")


if __name__ == "__main__":
    main()
