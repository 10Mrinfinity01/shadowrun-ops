import json
import os
import pathlib
import threading
import time

import psycopg

RUNTIME = pathlib.Path("/runtime")  # mounted from data/runtime on the host; written only by shop/deploy.py
DB_USER = os.getenv("DB_USER", "shop")
DB_PASSWORD = os.getenv("DB_PASSWORD", "shop_demo_only")
DB_NAME = os.getenv("DB_NAME", "shop")

DEFAULT_RELEASE = {"version": "2.2"}
DEFAULT_CONFIG = {"db_host": "demo-db", "db_port": 5432, "db_timeout_s": 2, "gateway_timeout_ms": 2000}


def log(level, msg):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {level} {msg}", flush=True)


def load_runtime(service):
    """Release and config are read once at startup, like most real services. A restart applies changes."""
    def read(kind, default):
        path = RUNTIME / f"{service}.{kind}.json"
        try:
            return {**default, **json.loads(path.read_text())}
        except FileNotFoundError:
            return dict(default)
        except Exception as e:
            log("ERROR", f"could not parse {path.name}: {e}; using defaults")
            return dict(default)
    release, config = read("release", DEFAULT_RELEASE), read("config", DEFAULT_CONFIG)
    log("INFO", f"{service} starting release={release['version']} config={config}")
    return release, config


class Db:
    def __init__(self, config):
        self.url = (f"postgresql://{DB_USER}:{DB_PASSWORD}@{config['db_host']}:{config['db_port']}/{DB_NAME}")
        self.timeout = int(config.get("db_timeout_s", 2))
        self.target = f"{config['db_host']}:{config['db_port']}"

    def connect(self):
        return psycopg.connect(self.url, connect_timeout=self.timeout)

    def ok(self):
        try:
            with self.connect() as conn:
                conn.execute("SELECT 1")
            return True, "ok"
        except Exception as e:
            reason = str(e).strip().splitlines()[0][:200]
            log("ERROR", f"database connection failed host={self.target} reason={reason}")
            return False, reason

    def init_schema_in_background(self, statements):
        def worker():
            while True:
                try:
                    with self.connect() as conn:
                        for stmt in statements:
                            conn.execute(stmt)
                    log("INFO", "schema ready")
                    return
                except Exception as e:
                    log("WARN", f"waiting for database at {self.target}: {str(e).strip().splitlines()[0][:120]}")
                    time.sleep(2)
        threading.Thread(target=worker, daemon=True).start()
