# -*- coding: utf-8 -*-
"""把本机 DPAPI 加密的凭据迁移到 205 并部署 launchd 常驻服务（一条命令完成）。

安全约定：
- 解密只发生在本进程内存，凭据不打印、不写任何本地临时文件；
- 凭据经 SSH 加密通道（stdin）送进 205 的 deploy_creds.py，远端只写 chmod 600 的 plist；
- 终端与日志只出现状态信息（字段名、校验结果），不含凭据内容。

用法：python migrate_creds_205.py
前置：已运行 --save-credentials 与 --save-llm-key；205 的 ~/chatwoot-odoo-bot/ 已有
chatwoot_test_bot.py 与 deploy_creds.py；本机到 205 的 SSH 免密可用。
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

BOT_PATH = Path(__file__).resolve().parent / "chatwoot_test_bot.py"
# SSH 目标默认指向内网 205；可用环境变量覆盖（如更换主机或用户名），避免在代码里固定账号信息
REMOTE = os.environ.get("CHATWOOT_MIGRATE_REMOTE", "mac5@192.168.0.205")
REMOTE_CMD = "python3 ~/chatwoot-odoo-bot/deploy_creds.py"

# 加载正式机器人模块，复用它同一套 DPAPI 与凭据文件路径逻辑，保证读到的就是运行时用的凭据
spec = importlib.util.spec_from_file_location("migrate_bot", BOT_PATH)
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

if not bot._dpapi_available():
    raise SystemExit("本机不是 Windows 或 DPAPI 不可用，无法解密凭据文件。")

path = bot.credential_file()
if not path.exists():
    raise SystemExit(f"未找到凭据文件 {path}，请先运行 --save-credentials / --save-llm-key。")
try:
    record = json.loads(bot._dpapi_transform(path.read_bytes(), protect=False).decode("utf-8"))
except (OSError, ValueError, UnicodeError):
    raise SystemExit("凭据文件解密失败（需与保存时同一 Windows 账户运行）。")

# 三项凭据必须齐全且非空，缺哪个都直接中止，避免远端带坏配置启动
missing = [name for name in ("user", "bot", "glm") if not isinstance(record.get(name), str) or not record[name].strip()]
if missing:
    raise SystemExit(f"凭据文件缺少字段：{', '.join(missing)}，请补录后再迁移。")
print("本机凭据解密成功：user / bot / glm 三项齐全（内容不显示）。")

# 凭据只在内存中组装，经 stdin 走 SSH 加密通道；输出与错误原样转发远端状态信息
payload = json.dumps({name: record[name] for name in ("user", "bot", "glm")}).encode("utf-8")
result = subprocess.run(["ssh", REMOTE, REMOTE_CMD], input=payload, capture_output=True)
sys.stdout.write(result.stdout.decode("utf-8", "replace"))
if result.returncode != 0:
    sys.stderr.write(result.stderr.decode("utf-8", "replace"))
    raise SystemExit(f"远端部署失败（exit {result.returncode}）。")
print("迁移与部署完成：205 已接管自动回复。")
