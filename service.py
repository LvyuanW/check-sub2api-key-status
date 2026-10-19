#!/usr/bin/env python3
"""Read-only API key status page for sub2api v0.2.17.

Only PostgreSQL SELECT and Redis TIME/ZCOUNT are used at runtime.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import inspect
import os
from pathlib import Path
import socket
import sys
import threading
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
ZERO = Decimal("0")
KEY_SQL = """
SELECT CURRENT_TIMESTAMP AS checked_at,
       k.id, k.group_id, k.status AS key_status, k.last_used_at,
       k.expires_at, k.quota, k.quota_used,
       k.rate_limit_5h, k.rate_limit_1d, k.rate_limit_7d,
       k.usage_5h, k.usage_1d, k.usage_7d,
       k.window_5h_start, k.window_1d_start, k.window_7d_start,
       u.status AS user_status, u.balance, u.frozen_balance,
       u.concurrency AS user_concurrency_limit,
       g.id AS found_group_id, g.name AS group_name,
       g.status AS group_status, g.subscription_type,
       g.daily_limit_usd, g.weekly_limit_usd, g.monthly_limit_usd,
       s.id AS subscription_id, s.status AS subscription_status,
       s.starts_at AS subscription_starts_at,
       s.expires_at AS subscription_expires_at,
       s.daily_window_start, s.weekly_window_start, s.monthly_window_start,
       s.daily_usage_usd, s.weekly_usage_usd, s.monthly_usage_usd
FROM api_keys k
JOIN users u ON u.id = k.user_id AND u.deleted_at IS NULL
LEFT JOIN groups g ON g.id = k.group_id AND g.deleted_at IS NULL
LEFT JOIN user_subscriptions s ON s.user_id = k.user_id
     AND s.group_id = k.group_id AND s.deleted_at IS NULL
WHERE k.key = %s AND k.deleted_at IS NULL
"""
USAGE_SQL = """
SELECT COUNT(*) AS requests,
       COALESCE(SUM(actual_cost), 0) AS billed_usd,
       COALESCE(SUM(input_tokens), 0) AS input_tokens,
       COALESCE(SUM(output_tokens), 0) AS output_tokens,
       COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
       COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens
FROM usage_logs WHERE api_key_id = %s
"""


@dataclass(frozen=True)
class Settings:
    pg_dsn: str
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_username: str = "key_status_ro"
    redis_password: str = ""
    redis_db: int = 0
    slot_ttl_seconds: int = 1800
    bind: str = "127.0.0.1"
    port: int = 8090
    allowed_hosts: tuple = ("key.tmallmodels.com", "key.manyai.icu", "localhost", "127.0.0.1")
    sub2api_timezone: str = "Asia/Shanghai"

    @classmethod
    def from_env(cls):
        s = cls(
            pg_dsn=os.environ["KEY_STATUS_PG_DSN"],
            redis_host=os.getenv("KEY_STATUS_REDIS_HOST", "127.0.0.1"),
            redis_port=int(os.getenv("KEY_STATUS_REDIS_PORT", "6379")),
            redis_username=os.getenv("KEY_STATUS_REDIS_USERNAME", "key_status_ro"),
            redis_password=os.environ["KEY_STATUS_REDIS_PASSWORD"],
            redis_db=int(os.getenv("KEY_STATUS_REDIS_DB", "0")),
            slot_ttl_seconds=int(os.getenv("KEY_STATUS_SLOT_TTL_SECONDS", "1800")),
            bind=os.getenv("KEY_STATUS_BIND", "127.0.0.1"),
            port=int(os.getenv("KEY_STATUS_PORT", "8090")),
            allowed_hosts=tuple(x.strip().lower() for x in os.getenv(
                "KEY_STATUS_ALLOWED_HOSTS", ",".join(cls.allowed_hosts)).split(",") if x.strip()),
            sub2api_timezone=os.getenv("SUB2API_TIMEZONE", "Asia/Shanghai"),
        )
        if s.bind not in ("127.0.0.1", "::1"):
            raise ValueError("KEY_STATUS_BIND must be a loopback address")
        if not 1 <= s.slot_ttl_seconds <= 86400 or not 1 <= s.port <= 65535:
            raise ValueError("Invalid port or slot TTL")
        if not s.allowed_hosts or not s.pg_dsn or not s.redis_password:
            raise ValueError("Missing service configuration")
        ZoneInfo(s.sub2api_timezone)
        return s


def decimal(value):
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


def effective_key_status(row, now):
    status = row["key_status"]
    if status != "active":
        return status
    if row["expires_at"] is not None and row["expires_at"] <= now:
        return "expired"
    if decimal(row["quota"]) > 0 and decimal(row["quota_used"]) >= decimal(row["quota"]):
        return "quota_exhausted"
    return "active"


def key_rate_windows(row, now):
    windows = []
    for name, seconds in (("5h", 18000), ("1d", 86400), ("7d", 604800)):
        limit = decimal(row.get("rate_limit_" + name))
        if limit <= 0:
            continue
        start = row.get("window_" + name + "_start")
        reset = start + timedelta(seconds=seconds) if start else None
        used = decimal(row.get("usage_" + name)) if reset and now < reset else ZERO
        windows.append({"window": name, "limit_usd": limit, "used_usd": used,
                        "remaining_usd": max(ZERO, limit - used),
                        "resets_at": reset if reset and now < reset else None})
    return windows


def subscription_snapshot(row, now, tz_name):
    if row.get("subscription_type") != "subscription":
        return None
    if row.get("subscription_id") is None:
        return {"status": "missing", "remaining_usd": ZERO, "expires_at": None, "windows": []}
    starts, expires = row["subscription_starts_at"], row["subscription_expires_at"]
    status = row["subscription_status"]
    if expires <= now:
        status = "expired"
    local_zone = ZoneInfo(tz_name)
    start_midnight = starts.astimezone(local_zone).replace(hour=0, minute=0, second=0, microsecond=0)
    one_time_daily = expires <= starts + timedelta(days=1)
    windows = []
    for name, days in (("daily", 1), ("weekly", 7), ("monthly", 30)):
        limit = row.get(name + "_limit_usd")
        if limit is None or decimal(limit) <= 0:
            continue
        limit = decimal(limit)
        used = decimal(row.get(name + "_usage_usd"))
        previous = row.get(name + "_window_start")
        reset = None
        if previous is not None:
            if name == "daily":
                if one_time_daily:
                    reset = expires
                else:
                    previous_midnight = previous.astimezone(local_zone).replace(
                        hour=0, minute=0, second=0, microsecond=0)
                    today = now.astimezone(local_zone).replace(hour=0, minute=0, second=0, microsecond=0)
                    if today > previous_midnight:
                        used = ZERO
                        previous_midnight = today
                    reset = previous_midnight + timedelta(days=1)
            else:
                # Match v0.2.17's legacy first-window anchor and expiry boundary.
                anchor = starts if start_midnight < starts and previous == start_midnight else previous
                period = timedelta(days=days)
                following = anchor + period
                if now >= following and following < expires:
                    used = ZERO
                    count = min((now - anchor) // period,
                                (expires - anchor - timedelta(microseconds=1)) // period)
                    anchor += count * period
                reset = anchor + period
        windows.append({"window": name, "limit_usd": limit, "used_usd": used,
                        "remaining_usd": max(ZERO, limit - used), "resets_at": reset})
    remaining = min((x["remaining_usd"] for x in windows), default=None)
    if status != "active":
        remaining = ZERO
    return {"status": status, "expires_at": expires, "remaining_usd": remaining, "windows": windows}


def make_snapshot(row, usage, concurrency, concurrency_available, tz_name="Asia/Shanghai"):
    now = row["checked_at"]
    quota, used = decimal(row["quota"]), decimal(row["quota_used"])
    group_status = row.get("group_status")
    if row.get("group_id") is not None and row.get("found_group_id") is None:
        group_status = "missing"
    result = {
        "checked_at": now,
        "key_status": row["key_status"],
        "key_effective_status": effective_key_status(row, now),
        "user_status": row["user_status"],
        "group_name": row.get("group_name"),
        "group_status": group_status,
        "billing_mode": row.get("subscription_type") or "standard",
        "expires_at": row["expires_at"],
        "last_used_at": row["last_used_at"],
        "wallet_balance_usd": decimal(row["balance"]),
        "wallet_frozen_usd": decimal(row.get("frozen_balance")),
        "key_quota_limit_usd": quota if quota > 0 else None,
        "key_quota_used_usd": used,
        "key_quota_remaining_usd": max(ZERO, quota - used) if quota > 0 else None,
        "usage": {k: int(usage[k]) for k in ("requests", "input_tokens", "output_tokens",
                                             "cache_creation_tokens", "cache_read_tokens")},
        "current_concurrency": concurrency,
        "concurrency_available": concurrency_available,
        "user_concurrency_limit": row["user_concurrency_limit"],
        "rate_windows": key_rate_windows(row, now),
        "subscription": subscription_snapshot(row, now, tz_name),
    }
    result["usage"]["billed_usd"] = decimal(usage["billed_usd"])
    result["usage"]["total_tokens"] = sum(result["usage"][x] for x in
        ("input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens"))
    return result


class Store:
    def __init__(self, settings):
        import psycopg2
        from psycopg2.extras import RealDictCursor
        import redis
        self.settings, self.pg, self.cursor_type = settings, psycopg2, RealDictCursor
        redis_options = dict(
            host=settings.redis_host, port=settings.redis_port,
            username=settings.redis_username, password=settings.redis_password,
            db=settings.redis_db, socket_connect_timeout=2, socket_timeout=2,
            max_connections=24,
            # No PING, CLIENT SETINFO, transaction or write commands are needed.
            health_check_interval=0,
        )
        # Ubuntu 22.04 ships redis-py 3.5.3; 5+ adds CLIENT SETINFO defaults.
        parameters = inspect.signature(redis.Redis).parameters
        if "lib_name" in parameters:
            redis_options.update(lib_name=None, lib_version=None)
        self.redis = redis.Redis(**redis_options)

    def lookup(self, key):
        conn = self.pg.connect(self.settings.pg_dsn, connect_timeout=3,
            application_name="key-status-readonly",
            options="-c statement_timeout=4000 -c default_transaction_read_only=on")
        try:
            conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
            with conn.cursor(cursor_factory=self.cursor_type) as cur:
                cur.execute(KEY_SQL, (key,))
                row = cur.fetchone()
                if row is None:
                    return None
                cur.execute(USAGE_SQL, (row["id"],))
                usage = cur.fetchone()
        finally:
            conn.close()
        concurrency, available = None, False
        try:
            now = int(self.redis.time()[0])
            # Exclusive bounds are equivalent to the app's expired-slot removal.
            pipe = self.redis.pipeline(transaction=False)
            pipe.zcount(f"concurrency:api_key:{row['id']}", f"({now - self.settings.slot_ttl_seconds}", "+inf")
            pipe.zcount(f"concurrency:live:api_key:{row['id']}", f"({now - 60}", "+inf")
            counts = pipe.execute()
            concurrency, available = sum(int(x) for x in counts), True
        except Exception:
            # Missing Redis data is not reported as a false zero.
            pass
        return make_snapshot(row, usage, concurrency, available, self.settings.sub2api_timezone)


def json_value(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    raise TypeError("Unsupported response value")


class Handler(BaseHTTPRequestHandler):
    server_version = "KeyStatus"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_args):
        pass  # Do not log request paths, headers, API keys or response data.

    def send_error(self, code, message=None, explain=None):
        self.reply_json(code, {"error": "请求无效"})

    def reply(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass

    def reply_json(self, status, value):
        self.reply(status, json.dumps(value, ensure_ascii=False, default=json_value).encode(),
                   "application/json; charset=utf-8")

    def allowed_request(self):
        try:
            host = self.headers.get("Host", "")
            parsed = urlsplit("//" + host)
            if parsed.hostname not in self.server.settings.allowed_hosts or parsed.username or parsed.password:
                return False
            origin = self.headers.get("Origin")
            if origin:
                parsed_origin = urlsplit(origin)
                if parsed_origin.scheme not in ("http", "https") or parsed_origin.netloc.lower() != host.lower():
                    return False
            return True
        except ValueError:
            return False

    def do_GET(self):
        if not self.allowed_request():
            self.reply_json(403, {"error": "请求来源无效"})
            return
        if self.path == "/healthz":
            self.reply_json(200, {"status": "ok"})
            return
        files = {"/": ("index.html", "text/html; charset=utf-8"),
                 "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                 "/style.css": ("style.css", "text/css; charset=utf-8")}
        item = files.get(self.path)
        if item is None:
            self.reply_json(404, {"error": "页面不存在"})
            return
        self.reply(200, (ROOT / "static" / item[0]).read_bytes(), item[1])

    def do_POST(self):
        if not self.allowed_request():
            self.reply_json(403, {"error": "请求来源无效"})
            return
        if self.path != "/api/check":
            self.reply_json(404, {"error": "接口不存在"})
            return
        if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            self.reply_json(415, {"error": "请使用 JSON 请求"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 1 <= length <= 1024:
            self.reply_json(413, {"error": "请求长度无效"})
            return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError()
            data = json.loads(raw)
            key = data.get("key") if isinstance(data, dict) else None
            if not isinstance(key, str):
                raise ValueError()
            key = key.strip()
            if not 1 <= len(key.encode("utf-8")) <= 128 or any(ord(c) < 33 or ord(c) == 127 for c in key):
                raise ValueError()
        except (ValueError, UnicodeError, RecursionError, socket.timeout):
            self.reply_json(400, {"error": "请输入有效的 API Key（最长 128 字节）"})
            return
        if not self.server.lookup_slots.acquire(blocking=False):
            self.reply_json(503, {"error": "查询繁忙，请稍后重试"})
            return
        try:
            result = self.server.store.lookup(key)
            if result is None:
                self.reply_json(404, {"error": "未找到此 API Key，或所属用户已删除"})
            else:
                self.reply_json(200, result)
        except Exception as exc:
            # Exception text can contain SQL, credentials or API keys: omit it.
            print("Status lookup unavailable (" + type(exc).__name__ + ")", file=sys.stderr)
            self.reply_json(503, {"error": "数据服务暂时不可用，请稍后重试"})
        finally:
            self.server.lookup_slots.release()


def make_server(settings, store):
    server_type = ThreadingHTTPServer
    if settings.bind == "::1":
        class IPv6Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6
        server_type = IPv6Server
    server = server_type((settings.bind, settings.port), Handler)
    server.daemon_threads = True
    server.settings, server.store = settings, store
    server.lookup_slots = threading.BoundedSemaphore(24)
    return server


def main():
    try:
        settings = Settings.from_env()
        server = make_server(settings, Store(settings))
    except Exception as exc:
        print("Service configuration failed (" + type(exc).__name__ + ")", file=sys.stderr)
        return 1
    print(f"Key status service listening on {settings.bind}:{settings.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
