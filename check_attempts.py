# 查询机器人 sqlite 的 attempts 记录数与明细（终验用）
import sqlite3
import sys
from pathlib import Path

db_path = Path(__file__).parent / "chatwoot_test_bot.sqlite3"
with sqlite3.connect(db_path) as db:
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "attempts" not in tables:
        print("attempts 表不存在（机器人尚未初始化数据库）")
        raise SystemExit
    count = db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
    print(f"attempts 记录数: {count}")
    if len(sys.argv) > 1 and sys.argv[1] == "-v":
        for (mid,) in db.execute("SELECT message_id FROM attempts ORDER BY message_id"):
            print(f"  message_id={mid}")
