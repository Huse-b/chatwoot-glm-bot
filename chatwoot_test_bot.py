"""在 Windows 上测试 Chatwoot Agent Bot 的固定回复。使用 Python 3，无需额外依赖。

两种运行方式：
  python chatwoot_test_bot.py            回调模式（监听 18787，接收 Chatwoot 推送）
  python chatwoot_test_bot.py --polling  轮询模式（主动查询新消息并自动回复，无需回调地址）
"""

import argparse
import datetime
import getpass
import hmac
import http.client
import json
import logging
import logging.handlers
import os
import secrets
import socket
import sqlite3
import sys
import time
from contextlib import closing
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


CHATWOOT_HOST = "192.168.0.205"
CHATWOOT_PORT = 3000
# —— 云端预留：Chatwoot 迁到公网/云端时，只需通过环境变量或 --chatwoot-host 换目标 ——
CHATWOOT_SCHEME = "http"                    # 连接协议：内网明文 http，云端 TLS 用 https
CHATWOOT_TIMEOUT_SECONDS = 10               # 单次 Chatwoot 请求超时（公网往返比内网慢，放宽到 10 秒）
CHATWOOT_SCHEME_ENV = "CHATWOOT_SCHEME"     # 环境变量名：连接协议（http/https）
CHATWOOT_HOST_ENV = "CHATWOOT_HOST"         # 环境变量名：Chatwoot 服务器地址
CHATWOOT_PORT_ENV = "CHATWOOT_PORT"         # 环境变量名：Chatwoot 端口
ACCOUNT_ID = 1
BOT_PORT = 18787
BOT_NAME = "Odoo 测试机器人"
STATE_FILE = Path(__file__).with_suffix(".sqlite3")
REPLY = "您好，我是 Odoo 测试机器人。已收到您的消息！"
TEST_GREETINGS = {"您好", "你好"}
POLL_INTERVAL_SECONDS = 3
POLL_RETRY_SECONDS = 15
# —— GLM（智谱开放平台）多轮生成回复相关配置：仅在提供了密钥时启用 ——
LLM_API_KEY_ENV = "GLM_API_KEY"             # 环境变量名：智谱 GLM API Key
LLM_SYSTEM_PROMPT_ENV = "LLM_SYSTEM_PROMPT"  # 环境变量名：自定义客服系统提示词（开源默认使用通用示例）
LLM_HOST = "open.bigmodel.cn"               # 智谱开放平台主机
LLM_PATH = "/api/paas/v4/chat/completions"  # 对话补全接口路径（OpenAI 兼容风格）
LLM_MODEL = "glm-4-flash"                   # 生成模型（免费），可用 --llm-model 覆盖为 glm-4-air / glm-4-plus 等
LLM_TIMEOUT_SECONDS = 20                    # 调用 GLM 的网络超时（秒）
LLM_CONTEXT_LIMIT = 12                      # 拼接多轮上下文时取的最近消息条数上限
# 开源默认提示词：通用客服助手示例，不含任何具体企业信息。
# 自部署时通过环境变量 LLM_SYSTEM_PROMPT 注入你自己的业务介绍，详见 README。
LLM_SYSTEM_PROMPT = (
    "你是网站在线客服助手。请始终使用访客消息所用的语言回复：访客用英文就用英文，"
    "用中文就用中文，其他语言同理；语气保持简洁、专业、友好。不确定的信息（尤其是"
    "具体价格、项目周期等）不要编造，可建议访客留下联系方式由人工跟进。"
)
LLM_FALLBACK = "抱歉，我这边暂时无法回答，已记录您的问题，人工稍后会跟进。"
# —— 无人值守（常驻）运行相关配置 ——
USER_TOKEN_ENV = "CHATWOOT_USER_TOKEN"      # 环境变量名：用户（客服）令牌，读取会话用
BOT_TOKEN_ENV = "CHATWOOT_BOT_TOKEN"        # 环境变量名：机器人令牌，发送回复用
HEARTBEAT_SECONDS = 300                     # 每隔多少秒记录一次心跳日志，便于监控存活
LOG_MAX_BYTES = 1_000_000                   # 单个日志文件字节上限，超过则滚动
LOG_BACKUP_COUNT = 3                        # 保留的历史日志文件个数
logger = logging.getLogger("chatwoot_bot")  # 模块级日志器：同时输出到控制台和滚动文件


def local_ip():
    # UDP connect selects the local interface without sending a packet.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect((CHATWOOT_HOST, CHATWOOT_PORT))
        return route.getsockname()[0]


def initialize_tables(db):
    db.execute("CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS attempts (message_id INTEGER PRIMARY KEY)")


def initialize_state():
    with closing(sqlite3.connect(STATE_FILE)) as db, db:
        initialize_tables(db)
        db.execute(
            "INSERT OR IGNORE INTO settings VALUES ('webhook_key', ?)",
            (secrets.token_urlsafe(24),),
        )
        key = db.execute("SELECT value FROM settings WHERE name = 'webhook_key'").fetchone()[0]
    return f"/chatwoot/{key}"


def claim_message(message_id):
    # Persist the claim before sending so webhook retries cannot duplicate the reply.
    with closing(sqlite3.connect(STATE_FILE)) as db, db:
        result = db.execute("INSERT OR IGNORE INTO attempts VALUES (?)", (message_id,))
        return result.rowcount == 1


def valid_signature(headers, body, secret):
    timestamp = headers.get("X-Chatwoot-Timestamp", "")
    signature = headers.get("X-Chatwoot-Signature", "")
    if not secret:
        return not timestamp and not signature
    if not timestamp.isascii() or not timestamp.isdecimal() or len(timestamp) > 12:
        return False
    if abs(time.time() - int(timestamp)) > 300 or not signature.isascii():
        return False
    signed_body = timestamp.encode("ascii") + b"." + body
    expected = "sha256=" + hmac.new(secret.encode(), signed_body, "sha256").hexdigest()
    return hmac.compare_digest(signature, expected)


def reply_target(event, inbox_id):
    if event.get("event") != "message_created" or event.get("message_type") != "incoming":
        return None
    if event.get("private") or (event.get("content") or "").strip() not in TEST_GREETINGS:
        return None
    account, inbox, conversation = event["account"], event["inbox"], event["conversation"]
    if account["id"] != ACCOUNT_ID or inbox["id"] != inbox_id:
        return None
    if conversation["status"] != "pending":
        return None
    message_id, conversation_id = event["id"], conversation["id"]
    if type(message_id) is not int or type(conversation_id) is not int:
        return None
    return message_id, conversation_id


def validate_access_token(token):
    if not token:
        raise SystemExit("请填写 Access Token。")
    if any(not 33 <= ord(char) <= 126 for char in token):
        raise SystemExit(
            "Access Token 含有控制字符、空白或非 ASCII 字符，尚未发送请求。"
            "部分 Windows 隐藏输入会把 Ctrl+V 读成控制字符；"
            "请重启程序，用鼠标右键粘贴或 Shift+Insert，只填写令牌本身。"
        )


def _dpapi_available():
    # DPAPI 是 Windows 专有的数据保护接口，非 Windows 平台不可用。
    return sys.platform == "win32"


def _dpapi_transform(data, protect):
    # 调用 Windows DPAPI（crypt32.dll）对字节做加密(protect=True)或解密(protect=False)。
    # 采用用户级密钥：密文与当前 Windows 账户绑定，同账户可解密，其他账户或机器无法解密，
    # 因此凭据文件即使被拷走也还原不出令牌，且磁盘上不含明文。
    import ctypes
    import ctypes.wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = DataBlob()
    function = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not function(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("Windows DPAPI 调用失败，请确认以保存时的同一 Windows 账户运行。")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def load_stored_credentials():
    # 读取 DPAPI 加密凭据文件，返回 (user_token, bot_token)；文件不存在返回 None。
    # 文件存在却解密/解析失败时明确退出，避免无人值守时静默卡死在交互输入。
    path = credential_file()
    if not path.exists():
        return None
    if not _dpapi_available():
        raise SystemExit("检测到加密凭据文件，但当前不是 Windows 系统，无法解密。请改用环境变量或交互输入。")
    try:
        record = json.loads(_dpapi_transform(path.read_bytes(), protect=False).decode("utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise SystemExit(
            f"无法解密凭据文件 {path.name}。请以保存时的同一 Windows 账户运行，"
            "或删除该文件后用 --save-credentials 重新保存。"
        ) from None
    user, bot_token = record.get("user"), record.get("bot")
    if not (isinstance(user, str) and isinstance(bot_token, str) and user and bot_token):
        raise SystemExit(f"凭据文件 {path.name} 内容不完整，请用 --save-credentials 重新保存。")
    return user, bot_token


def save_credentials():
    # 一次性录入：交互隐藏输入两个令牌 → 校验 → DPAPI 加密 → 写入凭据文件（不落明文）。
    if not _dpapi_available():
        raise SystemExit("--save-credentials 依赖 Windows DPAPI，仅支持 Windows。")
    user = getpass.getpass("用户 Access Token（个人资料设置里，读会话用，粘贴时不显示）: ").strip()
    validate_access_token(user)
    bot_token = getpass.getpass("机器人 Access Token（发回复用，粘贴时不显示）: ").strip()
    validate_access_token(bot_token)
    plaintext = json.dumps({"user": user, "bot": bot_token}).encode("utf-8")
    path = credential_file()
    try:
        path.write_bytes(_dpapi_transform(plaintext, protect=True))
    except OSError:
        raise SystemExit(f"无法写入凭据文件 {path}。请检查脚本目录的写入权限。") from None
    print(f"凭据已用 Windows DPAPI 加密保存到 {path.name}（不含明文，仅当前 Windows 账户可解密）。")


def save_llm_key():
    # 录入并用 DPAPI 加密保存智谱 GLM API Key；合并进现有凭据文件，保留 user/bot 不变。
    # 密钥只在内存中出现，写入的是密文，绝不打印明文；仅当前 Windows 账户可解密。
    if not _dpapi_available():
        raise SystemExit("--save-llm-key 依赖 Windows DPAPI，仅支持 Windows；Linux 请用环境变量 GLM_API_KEY。")
    key = getpass.getpass("智谱 GLM API Key（粘贴时不显示）: ").strip()
    if not key:
        raise SystemExit("未输入 GLM API Key。")
    # 智谱密钥形如 id.secret（纯 ASCII、无空白、长度约 36+）。太短说明隐藏输入粘贴失败、
    # 只录到零星字符——当场拒绝并提示重新粘贴，避免带坏密钥进运行期才表现为 HTTP 401。
    if len(key) < 20 or not key.isascii() or any(char.isspace() for char in key):
        raise SystemExit("GLM API Key 形状异常（过短或含空白/非 ASCII），大概率粘贴不完整。请重新运行本命令，用鼠标右键或 Shift+Insert 粘贴完整密钥。")
    path = credential_file()
    record = {}
    if path.exists():
        try:
            record = json.loads(_dpapi_transform(path.read_bytes(), protect=False).decode("utf-8"))
        except (OSError, ValueError, UnicodeError):
            record = {}
    record["glm"] = key
    try:
        path.write_bytes(_dpapi_transform(json.dumps(record).encode("utf-8"), protect=True))
    except OSError:
        raise SystemExit(f"无法写入凭据文件 {path}。请检查脚本目录的写入权限。") from None
    print(f"GLM 密钥已用 Windows DPAPI 加密保存到 {path.name}（磁盘仅密文，不会打印明文）。")


def resolve_credentials():
    # 令牌来源优先级：环境变量 → DPAPI 加密凭据文件 → 交互隐藏输入回退。
    # 三条路径都复用 validate_access_token；令牌绝不写入日志或明文配置。
    env_user = os.environ.get(USER_TOKEN_ENV, "").strip()
    env_bot = os.environ.get(BOT_TOKEN_ENV, "").strip()
    if env_user and env_bot:
        validate_access_token(env_user)
        validate_access_token(env_bot)
        return env_user, env_bot
    stored = load_stored_credentials()
    if stored is not None:
        user, bot_token = stored
        validate_access_token(user)
        validate_access_token(bot_token)
        return user, bot_token
    user = getpass.getpass("用户 Access Token（个人资料设置里，读会话用，粘贴时不显示）: ").strip()
    validate_access_token(user)
    bot_token = getpass.getpass("机器人 Access Token（发回复用，粘贴时不显示）: ").strip()
    validate_access_token(bot_token)
    return user, bot_token


def parse_chatwoot_endpoint(value):
    # 解析 Chatwoot 目标地址，支持三种写法：host / host:port / scheme://host[:port]。
    # 返回 (scheme, host, port)：scheme 未写时为 None（沿用当前协议不改动）；
    # 端口未写时 http 按惯例取 3000、https 取 443，迁移云端无需手动补端口号。
    # 解析失败抛 ValueError，由调用方决定如何向用户提示。
    text = value.strip()
    if "://" in text:
        scheme, _, rest = text.partition("://")
        # 先剥离 scheme 再去尾部斜杠：对整串 rstrip 会把 "https://" 吃成 "https:"，
        # 导致「只有协议没有主机」的输入漏检，测试里专门放了 "https://" 这个边界用例
        rest = rest.rstrip("/")
        if scheme not in ("http", "https"):
            raise ValueError
        default_port = 443 if scheme == "https" else 3000
    else:
        scheme, rest, default_port = None, text.rstrip("/"), 3000
    host, _, port = rest.partition(":")
    if not host:
        raise ValueError
    if port:
        if not port.isdigit():
            raise ValueError
        return scheme, host, int(port)
    return scheme, host, default_port


def apply_chatwoot_endpoint(value):
    # 把解析结果写回模块级配置。scheme 省略时保持现值不变，方便内网换 IP 不改协议。
    global CHATWOOT_SCHEME, CHATWOOT_HOST, CHATWOOT_PORT
    scheme, host, port = parse_chatwoot_endpoint(value)
    CHATWOOT_HOST = host
    CHATWOOT_PORT = port
    if scheme:
        CHATWOOT_SCHEME = scheme


def apply_chatwoot_env():
    # 云端预留：从 CHATWOOT_SCHEME / CHATWOOT_HOST / CHATWOOT_PORT 三个环境变量读取目标。
    # 只有显式设置的变量才生效，全部未设置时保持模块默认，既有部署零变化；
    # 给了 host 时按「scheme://host:port」拼好后统一走解析，缺项由解析器补默认值。
    global CHATWOOT_SCHEME, CHATWOOT_HOST, CHATWOOT_PORT
    scheme = os.environ.get(CHATWOOT_SCHEME_ENV, "").strip().lower()
    host = os.environ.get(CHATWOOT_HOST_ENV, "").strip()
    port = os.environ.get(CHATWOOT_PORT_ENV, "").strip()
    if not (scheme or host or port):
        return
    if host:
        value = f"{scheme}://{host}" if scheme else host
        if port:
            value += f":{port}"
        try:
            apply_chatwoot_endpoint(value)
        except ValueError:
            raise SystemExit(f"环境变量指定的 Chatwoot 地址无法解析：{value}") from None
        return
    # 只给了端口或协议的情况：单项覆盖，其余保持现值
    if port:
        if not port.isdigit():
            raise SystemExit(f"环境变量 {CHATWOOT_PORT_ENV}={port} 不是合法端口。") from None
        CHATWOOT_PORT = int(port)
    if scheme:
        if scheme not in ("http", "https"):
            raise SystemExit(f"环境变量 {CHATWOOT_SCHEME_ENV}={scheme} 应为 http 或 https。") from None
        CHATWOOT_SCHEME = scheme


def chatwoot_request(method, route, token, payload=None):
    validate_access_token(token)
    endpoint = f"/api/v1/accounts/{ACCOUNT_ID}/{route}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"api-access-token": token}
    if body is not None:
        headers["Content-Type"] = "application/json"
    # 云端预留：根据 CHATWOOT_SCHEME 选择明文 HTTP 或 TLS 连接。模块属性可被环境变量
    # 或 --chatwoot-host 在启动时改写，离线测试也能直接赋值切换目标，逻辑保持单一入口。
    connection_cls = (
        http.client.HTTPSConnection if CHATWOOT_SCHEME == "https" else http.client.HTTPConnection
    )
    with closing(connection_cls(CHATWOOT_HOST, CHATWOOT_PORT, timeout=CHATWOOT_TIMEOUT_SECONDS)) as chatwoot:
        chatwoot.request(method, endpoint, body, headers)
        response = chatwoot.getresponse()
        return response.status, response.read(), response.getheader("Date")


def send_reply(conversation_id, token, content=REPLY):
    payload = {"content": content, "message_type": "outgoing", "private": False}
    status, _, _ = chatwoot_request("POST", f"conversations/{conversation_id}/messages", token, payload)
    return status


def resolve_llm_key():
    # GLM 密钥来源优先级：环境变量 GLM_API_KEY → DPAPI 凭据文件里的 glm 字段 → 无（禁用 LLM）。
    # 未提供密钥时返回 None，调用方据此退回「固定回复」旧行为，保证向后兼容与离线测试不受影响。
    key = os.environ.get(LLM_API_KEY_ENV, "").strip()
    if key:
        return key
    path = credential_file()
    if path.exists() and _dpapi_available():
        try:
            record = json.loads(_dpapi_transform(path.read_bytes(), protect=False).decode("utf-8"))
        except (OSError, ValueError, UnicodeError):
            return None
        value = record.get("glm")
        if isinstance(value, str) and value:
            return value
    return None


def resolve_system_prompt():
    # 系统提示词来源：环境变量 LLM_SYSTEM_PROMPT（自部署注入业务介绍）→ 代码内通用开源默认值。
    # 空白环境变量视为未设置，回退默认，保证行为可预期。
    custom = os.environ.get(LLM_SYSTEM_PROMPT_ENV, "").strip()
    if custom:
        return custom
    return LLM_SYSTEM_PROMPT


def llm_generate_reply(history, key):
    # 用标准库 HTTPS 调智谱开放平台的对话补全接口，根据会话上下文 history 生成回复文本。
    # history 形如 [{"role":"system|user|assistant","content":...}, ...]。任何网络/解析失败都返回
    # None，由调用方回退兜底文案；密钥只进请求头，绝不写日志。
    payload = json.dumps({"model": LLM_MODEL, "messages": history}).encode("utf-8")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        with closing(http.client.HTTPSConnection(LLM_HOST, 443, timeout=LLM_TIMEOUT_SECONDS)) as conn:
            conn.request("POST", LLM_PATH, payload, headers)
            response = conn.getresponse()
            status, body = response.status, response.read()
    except (OSError, http.client.HTTPException):
        log("GLM 请求失败，本条改用兜底文案。")
        return None
    if status != 200:
        log(f"GLM 返回 HTTP {status}，本条改用兜底文案。")
        return None
    try:
        content = json.loads(body)["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        log("GLM 响应无法解析，本条改用兜底文案。")
        return None
    content = content.strip() if isinstance(content, str) else ""
    return content or None


def log(message):
    # 统一走日志器：控制台可见，同时落滚动文件，便于无人值守时事后排查与监控。
    logger.info(message)


def credential_file():
    # DPAPI 加密凭据文件路径，由 STATE_FILE 派生（测试用临时 STATE_FILE 时自动隔离）。
    return STATE_FILE.with_suffix(".credentials")


def log_file():
    # 滚动日志文件路径，同样由 STATE_FILE 派生。
    return STATE_FILE.with_suffix(".log")


def setup_logging():
    # 常驻运行时终端可能不可见，日志需同时写入滚动文件。多次调用保持幂等（先关闭并清空 handler）。
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        handler.close()
    logger.handlers.clear()
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
    logger.addHandler(console)
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            log_file(), maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
        logger.addHandler(file_handler)
    except OSError:
        # 日志文件不可写（如只读目录）时仅保留控制台输出，不影响轮询主功能。
        pass


def release_claim(message_id):
    # Retry only when the server could not have accepted the reply.
    with closing(sqlite3.connect(STATE_FILE)) as db, db:
        db.execute("DELETE FROM attempts WHERE message_id = ?", (message_id,))


def api_error_detail(body, token):
    # Keep diagnostics limited to JSON error fields, without credentials or HTML dumps.
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError):
        return "服务端未返回可解析的 JSON 错误说明。"
    if not isinstance(data, dict):
        return "服务端未返回错误说明。"
    messages = []
    for key in ("error", "errors", "message"):
        value = data.get(key)
        if isinstance(value, str):
            messages.append(value)
        elif isinstance(value, list):
            messages.extend(item for item in value if isinstance(item, str))
    detail = " ".join(" ".join(messages).split())
    if token:
        detail = detail.replace(token, "[redacted]")
    return detail[:240] or "服务端未返回错误说明。"


def poll_get(route, user_token):
    status, body, server_date = chatwoot_request("GET", route, user_token)
    if status in (401, 403):
        raise SystemExit(
            f"Chatwoot 拒绝读取（HTTP {status}；接口 {route}）。"
            f"{api_error_detail(body, user_token)} 请核对客服令牌和收件箱权限。"
        )
    if status == 400:
        raise SystemExit(
            f"Chatwoot 读取失败（HTTP 400；接口 {route}）。"
            f"{api_error_detail(body, user_token)} 请核对请求参数和令牌输入，修正后重启程序。"
        )
    if status != 200:
        raise RuntimeError(f"Chatwoot 读取失败（HTTP {status}；接口 {route}）。{api_error_detail(body, user_token)}")
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("Expected a Chatwoot JSON object")
    return data, server_date


def list_conversations(user_token, inbox_id):
    conversations = {}
    server_date = None
    page = 1
    while True:
        response, date = poll_get(f"conversations?inbox_id={inbox_id}&status=all&page={page}", user_token)
        if page == 1:
            server_date = date
        data = response["data"]
        rows = data["payload"]
        if not isinstance(rows, list):
            raise ValueError("Expected a Chatwoot conversation list")
        previous_count = len(conversations)
        for conversation in rows:
            conversations[conversation["id"]] = conversation
        total = (data.get("meta") or {}).get("all_count")
        if len(conversations) == previous_count or (type(total) is int and len(conversations) >= total):
            return conversations.values(), server_date
        page += 1


def initialize_polling(db):
    with db:
        initialize_tables(db)
        db.execute("""
            CREATE TABLE IF NOT EXISTS poll_cursors (
                inbox_id INTEGER NOT NULL,
                conversation_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                PRIMARY KEY (inbox_id, conversation_id)
            )
        """)


def poll_started_at(db, inbox_id, server_date):
    key = f"poll_started_at_{inbox_id}"
    row = db.execute("SELECT value FROM settings WHERE name = ?", (key,)).fetchone()
    if row is not None:
        return int(row[0])
    try:
        start = int(parsedate_to_datetime(server_date).timestamp()) if server_date else int(time.time())
    except (ValueError, TypeError, OverflowError):
        start = int(time.time())
    with db:
        db.execute("INSERT OR IGNORE INTO settings VALUES (?, ?)", (key, start))
        return int(db.execute("SELECT value FROM settings WHERE name = ?", (key,)).fetchone()[0])


def save_cursor(db, inbox_id, conversation_id, message_id):
    with db:
        db.execute("""
            INSERT INTO poll_cursors VALUES (?, ?, ?)
            ON CONFLICT (inbox_id, conversation_id)
            DO UPDATE SET message_id = MAX(poll_cursors.message_id, excluded.message_id)
        """, (inbox_id, conversation_id, message_id))


def send_polled_reply(message_id, conversation_id, bot_token, history=None, llm_key=None):
    # 先去重（与旧逻辑一致）：同一 message_id 只处理一次，服务端重试也不会重复回复。
    if not claim_message(message_id):
        return
    # 双模式：有 GLM 密钥时按多轮上下文生成回复，生成失败回退兜底文案；无密钥时保持固定 REPLY。
    content = REPLY
    if llm_key:
        generated = llm_generate_reply(history or [], llm_key)
        content = generated if generated else LLM_FALLBACK
    try:
        status = send_reply(conversation_id, bot_token, content)
    except (ConnectionRefusedError, socket.gaierror):
        release_claim(message_id)
        raise RuntimeError("发送回复前连接失败，稍后重试。") from None
    except (OSError, http.client.HTTPException) as error:
        log(f"会话 #{conversation_id} 的回复结果未知（{type(error).__name__}），已保留去重记录，请在 Chatwoot 核对。")
        return

    if status == 429:
        release_claim(message_id)
        raise RuntimeError("Chatwoot 回复接口限流（HTTP 429），稍后重试。")
    if status in (401, 403):
        release_claim(message_id)
        raise SystemExit(f"Chatwoot 拒绝发送回复（HTTP {status}）。请核对机器人令牌和权限。")
    if not 200 <= status < 300:
        log(f"会话 #{conversation_id} 的回复返回 HTTP {status}，已保留去重记录，请在 Chatwoot 核对。")
        return
    if llm_key:
        log(f"已向会话 #{conversation_id} 发送 GLM 生成的回复。")
    else:
        log(f"已向会话 #{conversation_id} 发送测试回复。")


def build_llm_history(conversation_id, user_token):
    # 回复前拉取该会话最近一页消息，拼成 GLM 多轮上下文：system 提示 + 最近 N 条对话。
    # 只保留访客(user)与外发(assistant)的非私有文本消息；活动/模板等系统消息不进上下文。
    # 单独用不带 after 的 GET（与增量拉取区分），保证跨轮次对话历史完整。
    data, _ = poll_get(f"conversations/{conversation_id}/messages", user_token)
    rows = data["payload"]
    if not isinstance(rows, list):
        raise ValueError("Expected a Chatwoot message list")
    turns = []
    for row in sorted(rows, key=lambda item: item["id"]):
        content = row.get("content")
        if row.get("private") or not isinstance(content, str) or not content.strip():
            continue
        if row.get("message_type") == 0:
            turns.append({"role": "user", "content": content.strip()})
        elif row.get("message_type") == 1:
            turns.append({"role": "assistant", "content": content.strip()})
    return [{"role": "system", "content": resolve_system_prompt()}] + turns[-LLM_CONTEXT_LIMIT:]


def poll_conversation(db, conversation, inbox_id, started_at, user_token, bot_token, llm_key=None):
    meta = conversation.get("meta") or {}
    if conversation.get("inbox_id") != inbox_id or meta.get("channel") != "Channel::WebWidget":
        return
    conversation_id = conversation["id"]
    row = db.execute(
        "SELECT message_id FROM poll_cursors WHERE inbox_id = ? AND conversation_id = ?",
        (inbox_id, conversation_id),
    ).fetchone()
    cursor = row[0] if row else 0
    summary = (conversation.get("messages") or []) + [conversation.get("last_non_activity_message")]
    latest = max((message for message in summary if message), key=lambda message: message["id"], default=None)
    if latest is None or latest["id"] <= cursor:
        return

    assignee_type = meta.get("assignee_type")
    assigned_elsewhere = assignee_type not in (None, "AgentBot") or (
        assignee_type is None and bool(meta.get("assignee"))
    )
    if (
        latest["created_at"] <= started_at
        or conversation.get("status") not in ("open", "pending")
        or conversation.get("can_reply") is False
        or conversation.get("muted")
        or assigned_elsewhere
    ):
        save_cursor(db, inbox_id, conversation_id, latest["id"])
        return

    data, _ = poll_get(f"conversations/{conversation_id}/messages?after={cursor}", user_token)
    messages = data["payload"]
    if not isinstance(messages, list):
        raise ValueError("Expected a Chatwoot message list")
    if (data.get("meta") or {}).get("assignee"):
        # The message API reports the human assignee if the conversation was taken over.
        last_id = max((message["id"] for message in messages), default=latest["id"])
        save_cursor(db, inbox_id, conversation_id, max(latest["id"], last_id))
        return

    processed = cursor
    try:
        for message in sorted(messages, key=lambda item: item["id"]):
            message_id = message["id"]
            if message_id <= processed:
                continue
            content = message.get("content")
            # 公开访客入站消息的基本条件（与旧逻辑一致），再按是否有 GLM 密钥决定回复范围：
            # 有密钥 → 所有非空公开消息都生成回复（真·多轮）；无密钥 → 仅问候语走固定回复（旧行为）。
            eligible = (
                message["created_at"] > started_at
                and message.get("inbox_id", inbox_id) == inbox_id
                and message.get("conversation_id", conversation_id) == conversation_id
                and message.get("message_type") == 0
                and not message.get("private")
                and isinstance(content, str)
                and bool(content.strip())
            )
            if eligible and (llm_key or content.strip() in TEST_GREETINGS):
                history = build_llm_history(conversation_id, user_token) if llm_key else None
                send_polled_reply(message_id, conversation_id, bot_token, history, llm_key)
            processed = message_id
    finally:
        if processed > cursor:
            save_cursor(db, inbox_id, conversation_id, processed)


def poll_once(db, inbox_id, user_token, bot_token, llm_key=None):
    conversations, server_date = list_conversations(user_token, inbox_id)
    started_at = poll_started_at(db, inbox_id, server_date)
    for conversation in conversations:
        poll_conversation(db, conversation, inbox_id, started_at, user_token, bot_token, llm_key)


def poll_main(inbox_id=None):
    print("轮询模式。通过 Chatwoot API 读取和回复网站访客消息。")
    try:
        if inbox_id is None:
            inbox_id = int(input("Odoo 网站收件箱 ID（设置页网址末尾的数字）: ").strip())
    except ValueError:
        raise SystemExit("收件箱 ID 需要填数字。") from None
    if inbox_id < 1:
        raise SystemExit("收件箱 ID 需要填大于零的数字。")
    # 令牌来源：环境变量 → DPAPI 加密文件 → 交互输入（详见 resolve_credentials）。
    user_token, bot_token = resolve_credentials()
    # GLM 密钥可选：有则启用多轮智能回复，无则维持「仅问候语固定回复」旧行为。
    llm_key = resolve_llm_key()
    setup_logging()   # 凭据就绪后再开日志文件：启动期若报错退出就不会残留文件句柄
    if llm_key:
        log(f"GLM 已启用（模型 {LLM_MODEL}）：将对所有公开访客消息生成智能回复。")
    else:
        log("未配置 GLM 密钥：仅对「您好/你好」回复固定文案；用 --save-llm-key 或环境变量 GLM_API_KEY 启用智能回复。")

    try:
        with closing(sqlite3.connect(STATE_FILE)) as db:
            initialize_polling(db)
            log(f"开始轮询收件箱 #{inbox_id}。Ctrl+C 停止。")
            ready = False
            started = time.monotonic()   # 本次进程启动时刻，用于心跳统计运行时长
            last_heartbeat = started     # 上次心跳时间
            cycles = 0                   # 成功轮询轮数
            while True:
                try:
                    poll_once(db, inbox_id, user_token, bot_token, llm_key)
                except RuntimeError as error:
                    log(str(error))
                except sqlite3.Error:
                    log("本地状态文件暂时无法读写，稍后重试。")
                except (OSError, http.client.HTTPException):
                    log("暂时无法连接 Chatwoot，稍后重试。")
                except (ValueError, KeyError, TypeError, AttributeError):
                    log("Chatwoot 返回了无法解析的数据，已保留处理进度。")
                else:
                    cycles += 1
                    if not ready:
                        log("首次同步已完成。请在 Odoo 新会话发送「您好」或「你好」测试。")
                        ready = True
                    now = time.monotonic()
                    if now - last_heartbeat >= HEARTBEAT_SECONDS:
                        # 心跳：证明进程存活且在正常轮询，便于监控与事后排查。
                        processed = db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
                        log(f"心跳：已运行 {int(now - started)} 秒，完成 {cycles} 轮轮询，累计回复 {processed} 条。")
                        last_heartbeat = now
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue
                time.sleep(POLL_RETRY_SECONDS)
    except KeyboardInterrupt:
        print("\n轮询已停止。")
    except sqlite3.Error:
        raise SystemExit("无法初始化轮询状态文件，请检查脚本目录的写入权限。") from None
    finally:
        # 关闭日志文件句柄并清空 handler：既保证干净退出，也避免 Windows 下文件被占用无法删除。
        for handler in list(logger.handlers):
            handler.close()
        logger.handlers.clear()


def agent_bots_request(method, admin_token, payload=None):
    status, body, _ = chatwoot_request(method, "agent_bots", admin_token, payload)
    if status in (401, 403):
        raise RuntimeError(f"Chatwoot 拒绝了访问（HTTP {status}）。请使用管理员个人资料里的 Access Token。")
    if not 200 <= status < 300:
        raise RuntimeError(f"Chatwoot 机器人接口返回 HTTP {status}。")
    return json.loads(body)


def register_agent_bot(webhook_url, admin_token):
    # Serialize setup for this file and check Chatwoot before creating a record on retry.
    with closing(sqlite3.connect(STATE_FILE, timeout=10)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        bots = agent_bots_request("GET", admin_token)
        if not isinstance(bots, list):
            raise RuntimeError("Chatwoot 返回了无法识别的机器人列表。")
        named_bots = [bot for bot in bots if bot.get("name") == BOT_NAME and not bot.get("system_bot")]
        bot = next((bot for bot in named_bots if bot.get("outgoing_url") == webhook_url), None)
        if bot is None and named_bots:
            raise RuntimeError(f"已存在名为「{BOT_NAME}」的机器人，但它的回调地址不同。请先核对该机器人。")
        if bot is None:
            bot = agent_bots_request("POST", admin_token, {"name": BOT_NAME, "outgoing_url": webhook_url})

    token = bot.get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("Chatwoot 已返回机器人信息，但其中没有机器人的 Access Token。")
    return token, bot.get("secret") or ""


def setup_credentials(webhook_url):
    admin_token = getpass.getpass("Chatwoot 管理员个人资料里的 Access Token（隐藏输入）: ").strip()
    if not admin_token:
        raise SystemExit("请填写管理员个人资料里的 Access Token。")
    try:
        token, secret = register_agent_bot(webhook_url, admin_token)
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
    except sqlite3.Error:
        raise SystemExit("本地配置文件暂时无法打开。请关闭其他机器人窗口后再试。") from None
    except OSError as error:
        code = getattr(error, "winerror", None) or error.errno
        detail = f"（错误 {code}）" if code is not None else ""
        raise SystemExit(f"程序无法连接 Chatwoot 的 {CHATWOOT_SCHEME}://{CHATWOOT_HOST}:{CHATWOOT_PORT}{detail}。") from None
    except (http.client.HTTPException, ValueError, TypeError, AttributeError):
        raise SystemExit("Chatwoot 返回了无法读取的机器人配置。") from None
    print(f"Chatwoot 机器人「{BOT_NAME}」已就绪。")
    return token, secret


def make_server(listen_ip, port, webhook_path, inbox_id, token, secret):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            # The callback path contains a private key. Keep it out of request logs.
            pass

        def finish_request(self, status):
            self.send_response(status)
            if status != 204:
                self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            self.finish_request(204 if self.path == "/health" else 404)

        def do_POST(self):
            if self.path != webhook_path:
                if self.path.startswith("/chatwoot/"):
                    print("收到回调，但路径与本机生成的地址不一致。请核对 Chatwoot 保存的完整 Webhook 地址。", flush=True)
                return self.finish_request(404)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024 * 1024:
                    return self.finish_request(400)
                self.connection.settimeout(5)
                body = self.rfile.read(length)
            except (ValueError, OSError):
                return self.finish_request(400)

            if not valid_signature(self.headers, body, secret):
                print("回调签名校验失败，请核对机器人页面里的 Webhook Secret。", flush=True)
                return self.finish_request(401)
            try:
                event = json.loads(body)
                target = reply_target(event, inbox_id)
                if event.get("event") == "message_created" and event.get("message_type") == "incoming":
                    incoming_inbox = event["inbox"]["id"]
                    conversation_status = event["conversation"]["status"]
                    print(f"收到访客消息回调，收件箱 #{incoming_inbox}，会话状态 {conversation_status}。", flush=True)
                    if target is None:
                        print(f"已跳过。测试回复适用于收件箱 #{inbox_id} 的待处理会话，发送「您好」或「你好」触发。", flush=True)
            except (ValueError, KeyError, TypeError, AttributeError):
                return self.finish_request(400)
            if target is None:
                return self.finish_request(204)

            message_id, conversation_id = target
            try:
                if not claim_message(message_id):
                    return self.finish_request(204)
                status = send_reply(conversation_id, token)
            except (OSError, http.client.HTTPException, sqlite3.Error, UnicodeError):
                print("回复请求失败，请检查本机能否访问 Chatwoot，然后重发「您好」。", flush=True)
                return self.finish_request(500)
            if not 200 <= status < 300:
                print(f"Chatwoot 未接受回复（HTTP {status}），请核对机器人的 Access Token 和收件箱绑定。", flush=True)
                return self.finish_request(500)

            print(f"已向会话 #{conversation_id} 发送测试回复。", flush=True)
            return self.finish_request(204)

    return ThreadingHTTPServer((listen_ip, port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--callback-host", help="Chatwoot 能解析的本机域名，默认使用本机内网 IP")
    parser.add_argument("--chatwoot-host", help="Chatwoot 地址：host、host:port 或 https://域名（默认见代码常量 CHATWOOT_HOST；迁云端后传 https://<域名>）")
    parser.add_argument("--listen", help="HTTP 监听地址，默认使用本机内网 IP；反向隧道方案固定用 127.0.0.1")
    parser.add_argument("--inbox", type=int, help="收件箱 ID，不填则交互输入（一键恢复脚本用）")
    parser.add_argument("--token", help="机器人 Access Token，不填则交互输入（一键恢复脚本用）")
    parser.add_argument("--secret", help="Webhook Secret，不填则交互输入（一键恢复脚本用）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--setup", action="store_true", help="通过 Chatwoot 接口创建测试机器人并启动")
    mode.add_argument("--polling", action="store_true", help="主动通过 Chatwoot API 读取并回复新访客消息")
    mode.add_argument("--save-credentials", action="store_true", help="录入并用 Windows DPAPI 加密保存两个令牌，供无人值守轮询使用")
    mode.add_argument("--save-llm-key", action="store_true", help="录入并用 Windows DPAPI 加密保存智谱 GLM API Key")
    parser.add_argument("--llm-model", help="GLM 生成模型，默认 glm-4-flash")
    args = parser.parse_args()

    # 云端预留：先应用环境变量（CHATWOOT_SCHEME/HOST/PORT），再用 --chatwoot-host 覆盖。
    # 两级配置都只在显式给出时生效，默认见代码常量；带 https:// 前缀自动切换 TLS 连接。
    apply_chatwoot_env()
    if args.chatwoot_host:
        try:
            apply_chatwoot_endpoint(args.chatwoot_host)
        except ValueError:
            raise SystemExit(f"无法识别的 Chatwoot 地址：{args.chatwoot_host}（应为 host、host:port 或 https://域名）。") from None

    if args.save_credentials:
        save_credentials()
        return

    if args.save_llm_key:
        save_llm_key()
        return

    if args.llm_model:
        global LLM_MODEL
        LLM_MODEL = args.llm_model

    if args.polling:
        poll_main(args.inbox)
        return

    try:
        listen_ip = args.listen or local_ip()
        path = initialize_state()
    except (OSError, sqlite3.Error) as error:
        raise SystemExit(f"无法准备机器人，请检查内网连接和文件夹写入权限。{error}") from None

    webhook_url = f"http://{args.callback_host or listen_ip}:{BOT_PORT}{path}"
    if args.setup:
        print("正在设置 Chatwoot 测试机器人。请在这个窗口依次填写收件箱 ID 和管理员令牌。")
    else:
        print(f"Webhook URL: {webhook_url}")
        print("请先在 Chatwoot 的「设置 → 机器人」创建机器人，并把上面的 URL 填进去。")
    try:
        inbox_id = int(str(args.inbox if args.inbox is not None else input("Odoo 网站收件箱 ID（打开收件箱设置后，地址末尾的数字）: ")).strip())
    except ValueError:
        raise SystemExit("收件箱 ID 需要填数字。") from None
    if inbox_id < 1:
        raise SystemExit("收件箱 ID 需要填大于零的数字。")
    if args.setup:
        token, secret = setup_credentials(webhook_url)
    else:
        token = (args.token or getpass.getpass("机器人 Access Token（粘贴时不显示）: ")).strip()
        if not token:
            raise SystemExit("请填写机器人的 Access Token。")
        secret = (args.secret or getpass.getpass("Webhook Secret（旧版本页面没有此项时按回车）: ")).strip()

    try:
        server = make_server(listen_ip, BOT_PORT, path, inbox_id, token, secret)
    except OSError as error:
        raise SystemExit(f"无法监听 {listen_ip}:{BOT_PORT}。{error}") from None

    print(f"机器人已启动，监听 {listen_ip}:{BOT_PORT}。请绑定收件箱 #{inbox_id}，然后在 Odoo 新对话发送「您好」。")
    with server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n机器人已停止。")


if __name__ == "__main__":
    main()
