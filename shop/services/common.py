import os
import threading
import time

import psycopg

DB_URL = os.getenv("DATABASE_URL", "postgresql://shop:shop_demo_only@demo-db:5432/shop")


def log(level, msg):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {level} {msg}", flush=True)


def connect():
    return psycopg.connect(DB_URL, connect_timeout=2)


def db_ok():
    try:
        with connect() as conn:
            conn.execute("SELECT 1")
        return True, "ok"
    except Exception as e:
        reason = str(e).strip().splitlines()[0][:200]
        log("ERROR", f"database connection failed host=demo-db:5432 reason={reason}")
        return False, reason


def init_schema_in_background(sql_statements):
    """Create tables once the DB is reachable, without crashing the service if it isn't yet."""

    def worker():
        while True:
            try:
                with connect() as conn:
                    for stmt in sql_statements:
                        conn.execute(stmt)
                log("INFO", "schema ready")
                return
            except Exception as e:
                log("WARN", f"waiting for database: {str(e).strip().splitlines()[0][:120]}")
                time.sleep(2)

    threading.Thread(target=worker, daemon=True).start()
