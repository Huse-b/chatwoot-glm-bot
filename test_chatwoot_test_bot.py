import hmac
import http.client
import importlib.util
import json
import tempfile
import time
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread


ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("test_bot", ROOT / "chatwoot_test_bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)


@contextmanager
def serve(server):
    with server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            thread.join()


received = []
registered = []


class FakeChatwoot(BaseHTTPRequestHandler):
    agent_bots = "/api/v1/accounts/1/agent_bots"

    def json_response(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        assert self.path == self.agent_bots
        if self.headers["api-access-token"] != "fake-admin-token":
            return self.json_response(401, {})
        return self.json_response(200, registered)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        token = self.headers["api-access-token"]
        if self.path == self.agent_bots:
            assert token == "fake-admin-token"
            assert payload["name"] == bot.BOT_NAME
            record = {**payload, "id": 1, "access_token": "fake-bot-token", "secret": "secret"}
            registered.append(record)
            return self.json_response(201, record)
        received.append((self.path, token, payload))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_args):
        pass


def request(port, method, path, payload=None, secret="", timestamp=None):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    headers = {}
    if secret:
        timestamp = str(timestamp or int(time.time()))
        signature = hmac.new(secret.encode(), timestamp.encode() + b"." + body, "sha256").hexdigest()
        headers = {"X-Chatwoot-Timestamp": timestamp, "X-Chatwoot-Signature": "sha256=" + signature}
    with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=4)) as connection:
        connection.request(method, path, body, headers)
        return connection.getresponse().status


incoming = {
    "event": "message_created", "message_type": "incoming", "private": False, "content": " 您好 ", "id": 101,
    "account": {"id": 1}, "inbox": {"id": 42}, "conversation": {"id": 27, "status": "pending"},
}
bot.CHATWOOT_HOST = "127.0.0.1"
(ROOT / "work").mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(dir=ROOT / "work") as temporary:
    bot.STATE_FILE = Path(temporary) / "bot.sqlite3"
    path = bot.initialize_state()
    assert path == bot.initialize_state()
    with serve(ThreadingHTTPServer(("127.0.0.1", 0), FakeChatwoot)) as chatwoot_port:
        bot.CHATWOOT_PORT = chatwoot_port
        callback = f"http://203.0.113.10:18787{path}"
        try:
            bot.register_agent_bot(callback, "invalid-token")
        except RuntimeError as error:
            assert "401" in str(error)
        else:
            raise AssertionError("Expected the fake Chatwoot to reject invalid credentials")
        assert bot.register_agent_bot(callback, "fake-admin-token") == ("fake-bot-token", "secret")
        assert bot.register_agent_bot(callback, "fake-admin-token") == ("fake-bot-token", "secret")
        assert len(registered) == 1 and registered[0]["outgoing_url"] == callback
        try:
            bot.register_agent_bot(callback + "different", "fake-admin-token")
        except RuntimeError as error:
            assert bot.BOT_NAME in str(error)
        else:
            raise AssertionError("Expected the script to protect the existing callback")
        with serve(bot.make_server("127.0.0.1", 0, path, 42, "fake-bot-token", "secret")) as port:
            assert request(port, "GET", "/health") == 204
            assert request(port, "POST", "/chatwoot/incorrect", incoming, "secret") == 404
            assert request(port, "POST", path, incoming, "bad-secret") == 401
            assert request(port, "POST", path, incoming, "secret", int(time.time()) - 600) == 401
            assert request(port, "POST", path, incoming) == 401
            for modified in (
                {"message_type": "outgoing"}, {"event": "message_updated"}, {"private": True},
                {"inbox": {"id": 43}}, {"account": {"id": 2}}, {"content": "测试"},
                {"conversation": {"id": 27, "status": "open"}},
            ):
                assert request(port, "POST", path, incoming | modified, "secret") == 204
            assert received == []
            assert request(port, "POST", path, incoming, "secret") == 204
            assert request(port, "POST", path, incoming, "secret") == 204
        assert received == [(
            "/api/v1/accounts/1/conversations/27/messages", "fake-bot-token",
            {"content": bot.REPLY, "message_type": "outgoing", "private": False},
        )]
        with serve(bot.make_server("127.0.0.1", 0, path, 42, "fake-bot-token", "")) as port:
            assert request(port, "POST", path, incoming) == 204
            assert len(received) == 1
            assert request(port, "POST", path, incoming | {"id": 102, "content": "你好"}) == 204
            assert len(received) == 2

print("Passed: bot registration and reuse, signed and legacy callbacks, filtering, posting, and deduplication.")
