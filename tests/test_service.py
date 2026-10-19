import contextlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import http.client
import io
import json
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import service


NOW = datetime(2026, 10, 20, 4, 0, tzinfo=timezone.utc)
SECRET = "sk-test-only-never-echo-this"


def key_row(**updates):
    row = dict(checked_at=NOW, id=7, group_id=3, key_status="active", last_used_at=None,
               expires_at=None, quota=Decimal("100"), quota_used=Decimal("12.5"),
               user_status="active", balance=Decimal("80.125"), frozen_balance=Decimal("2"),
               user_concurrency_limit=5, found_group_id=3, group_name="测试组",
               group_status="active", subscription_type="standard")
    row.update(updates)
    return row


def usage_row():
    return dict(requests=2, billed_usd=Decimal("14.75"), input_tokens=10,
                output_tokens=20, cache_creation_tokens=30, cache_read_tokens=40)


def snapshot():
    return service.make_snapshot(key_row(), usage_row(), 3, True)


class MetricsTests(unittest.TestCase):
    def test_tokens_billed_usage_and_quota_are_distinct(self):
        result = snapshot()
        self.assertEqual(result["usage"]["total_tokens"], 100)
        self.assertEqual(result["usage"]["billed_usd"], Decimal("14.75"))
        self.assertEqual(result["key_quota_used_usd"], Decimal("12.5"))
        self.assertEqual(result["key_quota_remaining_usd"], Decimal("87.5"))
        self.assertEqual(result["wallet_balance_usd"], Decimal("80.125"))

    def test_unlimited_key_can_have_usage_and_zero_quota_counter(self):
        result = service.make_snapshot(key_row(quota=0, quota_used=0), usage_row(), 0, True)
        self.assertIsNone(result["key_quota_remaining_usd"])
        self.assertEqual(result["usage"]["billed_usd"], Decimal("14.75"))
        self.assertEqual(result["key_effective_status"], "active")

    def test_status_covers_disabled_expiry_exhaustion_and_stored_status(self):
        for updates, expected in [
            ({"key_status": "disabled"}, "disabled"),
            ({"expires_at": NOW}, "expired"),
            ({"quota_used": 101}, "quota_exhausted"),
            ({"key_status": "quota_exhausted", "quota_used": 0}, "quota_exhausted"),
        ]:
            with self.subTest(expected=expected, updates=updates):
                self.assertEqual(service.effective_key_status(key_row(**updates), NOW), expected)

    def test_deleted_group_and_unavailable_redis_are_not_false_active_or_zero(self):
        result = service.make_snapshot(key_row(found_group_id=None, group_status=None), usage_row(), None, False)
        self.assertEqual(result["group_status"], "missing")
        self.assertIsNone(result["current_concurrency"])
        self.assertFalse(result["concurrency_available"])

    def test_rate_window_resets_at_exact_boundary(self):
        result = service.key_rate_windows(key_row(rate_limit_5h=10, usage_5h=8,
            window_5h_start=NOW-timedelta(hours=5)), NOW)
        self.assertEqual(result[0]["used_usd"], 0)
        self.assertEqual(result[0]["remaining_usd"], 10)
        self.assertIsNone(result[0]["resets_at"])

    def subscription(self, **updates):
        row = key_row(subscription_type="subscription", subscription_id=9,
            subscription_status="active", subscription_starts_at=NOW-timedelta(days=10),
            subscription_expires_at=NOW+timedelta(days=30),
            daily_limit_usd=10, weekly_limit_usd=100, monthly_limit_usd=500,
            daily_usage_usd=8, weekly_usage_usd=80, monthly_usage_usd=200,
            daily_window_start=NOW-timedelta(days=1),
            weekly_window_start=NOW-timedelta(days=7),
            monthly_window_start=NOW-timedelta(days=10))
        row.update(updates)
        return service.subscription_snapshot(row, NOW, "Asia/Shanghai")

    def test_subscription_uses_current_windows_and_minimum_remaining(self):
        result = self.subscription()
        self.assertEqual([w["used_usd"] for w in result["windows"]], [0, 0, 200])
        self.assertEqual(result["remaining_usd"], 10)

    def test_one_day_subscription_does_not_reissue_daily_allowance(self):
        result = self.subscription(subscription_starts_at=NOW-timedelta(hours=16),
            subscription_expires_at=NOW+timedelta(hours=4),
            daily_window_start=NOW-timedelta(hours=16))
        self.assertEqual(result["windows"][0]["used_usd"], 8)
        self.assertEqual(result["windows"][0]["resets_at"], NOW+timedelta(hours=4))

    def test_subscription_expiry_prevents_extra_period(self):
        result = self.subscription(subscription_expires_at=NOW)
        self.assertEqual(result["status"], "expired")
        self.assertEqual(result["remaining_usd"], 0)
        self.assertEqual(result["windows"][1]["used_usd"], 80)


class FakeCursor:
    def __init__(self, row):
        self.calls, self.row = [], row
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass
    def execute(self, sql, params):
        self.calls.append((sql, params))
    def fetchone(self):
        return self.row if len(self.calls) == 1 else usage_row()


class FakeConnection:
    def __init__(self, row):
        self.fake_cursor, self.session, self.closed = FakeCursor(row), None, False
    def set_session(self, **kwargs):
        self.session = kwargs
    def cursor(self, **_kwargs):
        return self.fake_cursor
    def close(self):
        self.closed = True


class FakePG:
    def __init__(self, connection):
        self.connection = connection
    def connect(self, *_args, **kwargs):
        self.options = kwargs
        return self.connection


class FakeRedis:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail
    def time(self):
        if self.fail:
            raise RuntimeError("private redis credential must not escape")
        self.calls.append(("TIME",))
        return int(NOW.timestamp()), 0
    def pipeline(self, transaction):
        assert transaction is False
        return self
    def zcount(self, key, minimum, maximum):
        self.calls.append(("ZCOUNT", key, minimum, maximum))
    def execute(self):
        return [2, 1]


class StoreTests(unittest.TestCase):
    def store(self, row=None, redis_fail=False):
        obj = object.__new__(service.Store)
        obj.settings = service.Settings(pg_dsn="not-a-real-dsn")
        obj.pg = FakePG(FakeConnection(row or key_row()))
        obj.cursor_type = object
        obj.redis = FakeRedis(redis_fail)
        return obj

    def test_readonly_parameterized_sql_and_exact_per_key_redis_bounds(self):
        store = self.store()
        result = store.lookup(SECRET)
        connection = store.pg.connection
        self.assertTrue(connection.closed)
        self.assertTrue(connection.session["readonly"])
        self.assertIn("default_transaction_read_only=on", store.pg.options["options"])
        self.assertEqual(connection.fake_cursor.calls[0][1], (SECRET,))
        self.assertEqual(connection.fake_cursor.calls[1][1], (7,))
        self.assertNotIn(SECRET, connection.fake_cursor.calls[0][0])
        now = int(NOW.timestamp())
        self.assertEqual(store.redis.calls, [("TIME",),
            ("ZCOUNT", "concurrency:api_key:7", f"({now-1800}", "+inf"),
            ("ZCOUNT", "concurrency:live:api_key:7", f"({now-60}", "+inf")])
        self.assertEqual(result["current_concurrency"], 3)
        self.assertNotIn(SECRET, json.dumps(result, default=service.json_value))

    def test_redis_failure_keeps_database_results_and_marks_count_unavailable(self):
        result = self.store(redis_fail=True).lookup(SECRET)
        self.assertFalse(result["concurrency_available"])
        self.assertIsNone(result["current_concurrency"])
        self.assertEqual(result["usage"]["total_tokens"], 100)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class FakeStore:
            def lookup(self, key):
                if key == "explode":
                    raise RuntimeError("SECRET_ERROR_DETAIL")
                return snapshot() if key == SECRET else None
        cls.server = service.make_server(service.Settings(pg_dsn="fake", port=0), FakeStore())
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def request(self, method="POST", path="/api/check", body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        default_headers = {"Content-Type": "application/json"}
        default_headers.update(headers or {})
        conn.request(method, path, json.dumps({"key": SECRET}) if body is None else body, default_headers)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def test_success_has_security_headers_and_no_key(self):
        code, headers, body = self.request()
        self.assertEqual(code, 200)
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertNotIn(SECRET.encode(), body)
        self.assertEqual(json.loads(body)["usage"]["total_tokens"], 100)

    def test_rejects_cross_origin_and_unknown_host(self):
        self.assertEqual(self.request(headers={"Origin": "https://evil.invalid"})[0], 403)
        self.assertEqual(self.request(headers={"Host": "evil.invalid"})[0], 403)

    def test_invalid_unknown_and_oversize_keys(self):
        for body, expected in [("{}", 400), ("not-json", 400), ('{"key":42}', 400),
            (json.dumps({"key": "x"*129}), 400), ("x"*1025, 413),
            (json.dumps({"key": "not-found"}), 404)]:
            with self.subTest(body=body[:50]):
                self.assertEqual(self.request(body=body)[0], expected)

    def test_key_in_query_and_form_content_type_are_rejected(self):
        self.assertEqual(self.request(path="/api/check?key=invalid")[0], 404)
        self.assertEqual(self.request(body="key=invalid", headers={"Content-Type": "application/x-www-form-urlencoded"})[0], 415)

    def test_exception_message_is_neither_logged_nor_returned(self):
        logs = io.StringIO()
        with contextlib.redirect_stderr(logs):
            code, _headers, body = self.request(body='{"key":"explode"}')
        self.assertEqual(code, 503)
        self.assertNotIn("SECRET_ERROR_DETAIL", logs.getvalue())
        self.assertNotIn(b"SECRET_ERROR_DETAIL", body)

    def test_static_page_and_liveness(self):
        code, _headers, body = self.request(method="GET", path="/", body="")
        self.assertEqual(code, 200)
        self.assertIn(b'method="post"', body)
        self.assertNotIn(b"https://", body)
        self.assertEqual(self.request(method="GET", path="/healthz", body="")[0], 200)


if __name__ == "__main__":
    unittest.main()
