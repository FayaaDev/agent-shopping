import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from shopping import open_db, read_items
from web_app import COOKIE, SERVICE, ShoppingWeb, clean_run, create_app, main


PASSWORD = "private-test-password"
ORIGIN = "http://127.0.0.1:8080"


class StartupTests(unittest.TestCase):
    def startup_error(self, env, expected, app_error=None):
        with patch.dict(os.environ, env, clear=True), patch("web_app.load_dotenv"), \
                patch("web_app.TelegramShopping"), \
                patch("web_app.web.run_app") as run_app:
            if app_error is not None:
                with patch("web_app.create_app", side_effect=app_error):
                    with self.assertRaises(SystemExit) as caught:
                        main()
            else:
                with self.assertRaises(SystemExit) as caught:
                    main()
            self.assertEqual(str(caught.exception), expected)
            for value in env.values():
                if value:
                    self.assertNotIn(value, str(caught.exception))
            run_app.assert_not_called()

    def test_password_errors(self):
        for password in (None, "", "secret-short", "secret-long" * 103):
            with self.subTest(length=None if password is None else len(password)):
                env = {} if password is None else {"WEB_PASSWORD": password}
                self.startup_error(env, "WEB_PASSWORD must contain 16–1024 characters.")

    def test_origin_errors(self):
        for origin in ("https://secret.test/path", "https://secret:password@host",
                       "https://secret.test:secret-port", "https://secret.test:99999",
                       "https://[secret", "file://secret", "https://secret.test/",
                       "https://secret.test?secret", "https://secret.test#secret",
                       "https://secret .test", "https://secret\\test", ""):
            with self.subTest(case_length=len(origin)):
                self.startup_error({"WEB_PASSWORD": PASSWORD, "WEB_ORIGIN": origin},
                                   "WEB_ORIGIN must be an http/https origin with a host and optional valid port; no credentials, path, query, or fragment.")

    def test_port_errors(self):
        for port in ("secret-port", "", "0", "65536", "-1"):
            with self.subTest(case_length=len(port)):
                self.startup_error({"WEB_PASSWORD": PASSWORD, "WEB_PORT": port},
                                   "WEB_PORT must be an integer from 1 to 65535.")

    def test_unexpected_errors_stay_private(self):
        for error in (ValueError("private-password private-origin"),
                      OSError("private-password private-origin")):
            with self.subTest(error_type=type(error).__name__):
                expected = ("Could not access web configuration or data files. Check filesystem permissions and paths."
                            if isinstance(error, OSError) else "Invalid web configuration.")
                self.startup_error({"WEB_PASSWORD": PASSWORD}, expected, app_error=error)
                self.assertNotIn(str(error), expected)

    def test_dotenv_filesystem_error_stays_private(self):
        with patch("web_app.load_dotenv", side_effect=OSError("private-file-path")), \
                patch("web_app.web.run_app") as run_app:
            with self.assertRaises(SystemExit) as caught:
                main()
            self.assertEqual(str(caught.exception),
                             "Could not access web configuration or data files. Check filesystem permissions and paths.")
            run_app.assert_not_called()

    def test_valid_startup(self):
        for port in ("1", "65535", None):
            for password_length in (16, 1024):
                env = {"WEB_PASSWORD": "x" * password_length}
                if port is not None:
                    env["WEB_PORT"] = port
                with self.subTest(port=port, password_length=password_length), \
                        patch.dict(os.environ, env, clear=True), patch("web_app.load_dotenv"), \
                        patch("web_app.TelegramShopping") as runner, \
                        patch("web_app.web.run_app") as run_app:
                    runner.return_value.db = Path("isolated-test.db")
                    with patch("web_app.Path.exists", return_value=False):
                        main()
                    self.assertEqual(run_app.call_args.kwargs,
                                     {"host": "127.0.0.1", "port": int(port or "8080"), "access_log": None})


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "shopping.db"
        connection = open_db(self.db)
        connection.close()
        self.app = create_app(self.db, PASSWORD, ORIGIN, False)
        self.service = self.app[SERVICE]
        self.client = TestClient(TestServer(self.app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        self.service.runner.run = AsyncMock(return_value=(0, b"[]"))

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def post(self, action, body):
        return await self.client.post("/api/" + action, json=body, headers={"Origin": ORIGIN})

    async def login(self):
        response = await self.post("login", {"password": PASSWORD})
        self.assertEqual(response.status, 200)
        return response

    async def finish(self):
        tasks = tuple(self.service.tasks)
        if tasks:
            await asyncio.gather(*tasks)

    async def test_auth_csrf_sessions_headers_and_rate_limit(self):
        self.assertEqual((await self.client.get("/api/session")).status, 401)
        self.assertEqual((await self.client.post("/api/login", json={"password": PASSWORD})).status, 403)
        self.assertEqual((await self.post("login", {"password": "wrong"})).status, 401)
        response = await self.login()
        cookie = response.cookies[COOKIE]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Strict")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("unsafe-inline", response.headers["Content-Security-Policy"])
        self.assertEqual((await self.client.get("/api/session")).status, 200)
        self.assertEqual((await self.client.post("/api/clear", json={}, headers={"Origin": "https://evil.test"})).status, 403)
        await self.post("logout", {})
        self.assertEqual((await self.client.get("/api/state")).status, 401)
        await self.login()
        self.service.sessions = {key: 0 for key in self.service.sessions}
        self.assertEqual((await self.client.get("/api/session")).status, 401)
        for _ in range(12):
            response = await self.post("login", {"password": "wrong"})
        self.assertEqual(response.status, 429)
        self.assertLessEqual(len(self.service.logins), 10)

    async def test_input_bounds(self):
        await self.login()
        for body in ({"name": "x", "quantity": True}, {"name": "x", "quantity": 1000},
                     {"name": "", "quantity": 1}, {"name": "x" * 201, "quantity": 1},
                     {"name": "x", "quantity": 0}, []):
            self.assertEqual((await self.post("items", body)).status, 400)
        for cap in (-1, float("inf"), True):
            body = dict(name="x", product="y", sku=None, brand=None, max_price=cap)
            self.assertEqual((await self.post("preference", body)).status, 400)
        self.assertEqual((await self.post("preference", {"name": "x", "product": "y"})).status, 400)
        response = await self.client.post("/api/items", data="{broken", headers={"Origin": ORIGIN, "Content-Type": "application/json"})
        self.assertEqual(response.status, 400)
        self.assertEqual((await self.post("items", {"name": "x" * 17000})).status, 400)
        self.service.runner.run.assert_not_awaited()

    async def test_cli_mutations_nulls_and_state_allowlist(self):
        await self.login()
        self.assertEqual((await self.post("items", {"name": "--milk", "quantity": 3})).status, 200)
        self.service.runner.run.assert_awaited_with("add", "--", "--milk", "3")
        body = dict(name="milk", product="--fresh", sku="--123", brand="Brand", max_price=12.5)
        self.assertEqual((await self.post("preference", body)).status, 200)
        self.service.runner.run.assert_awaited_with("prefer", "--sku=--123", "--brand=Brand", "--max-price=12.5", "--", "milk", "--fresh")
        body.update(sku=None, brand=None, max_price=None)
        await self.post("preference", body)
        self.service.runner.run.assert_awaited_with("prefer", "--", "milk", "--fresh")
        await self.post("remove", {"name": "milk"})
        self.service.runner.run.assert_awaited_with("remove", "--", "milk")
        await self.post("clear", {})
        self.service.runner.run.assert_awaited_with("clear")
        self.service.runner.run.return_value = (0, json.dumps([dict(name="milk", quantity=3, diagnostic="SECRET", alternatives=[])]).encode())
        response = await self.client.get("/api/state")
        self.assertEqual(await response.json(), {"items": [{"name": "milk", "quantity": 3, "alternatives": []}], "run": None})

    async def test_real_list_cli_with_isolated_database(self):
        # Exercise actual argument parsing and full-replacement NULL behavior; never shop.
        del self.service.runner.run
        await self.login()
        self.assertEqual((await self.post("items", {"name": "milk", "quantity": 2})).status, 200)
        body = dict(name="milk", product="Fresh", sku="123", brand="Brand", max_price=5)
        self.assertEqual((await self.post("preference", body)).status, 200)
        body.update(sku=None, brand=None, max_price=None)
        self.assertEqual((await self.post("preference", body)).status, 200)
        with closing(open_db(self.db)) as db:
            item = read_items(db)[0]
            self.assertEqual(item["preferred_name"], "Fresh")
            self.assertIsNone(item["sku"])
            self.assertIsNone(item["brand"])
            self.assertIsNone(item["max_price"])
        self.assertEqual((await self.post("remove", {"name": "milk"})).status, 200)
        self.assertEqual((await self.post("clear", {})).status, 200)

    async def test_busy_state_and_shutdown(self):
        await self.login()
        started, stopped = asyncio.Event(), asyncio.Event()

        async def command(*args, **kwargs):
            if args[0] == "list":
                return 0, b"[]"
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        self.service.runner.run.side_effect = command
        self.assertEqual((await self.post("shop", {"location": "Home"})).status, 202)
        await started.wait()
        for action, body in (("clear", {}), ("shop", {}), ("items", {"name": "x", "quantity": 1})):
            self.assertEqual((await self.post(action, body)).status, 409)
        self.assertEqual((await self.client.get("/api/state")).status, 200)
        await self.service.shutdown(self.app)
        self.assertTrue(stopped.is_set())
        self.assertFalse(self.service.lock.locked())
        self.assertEqual(self.service.latest["status"], "incomplete")

    async def run_result(self, data, code=0, raw=None):
        async def command(*args, **kwargs):
            if args[0] == "shop":
                Path(args[args.index("--result-file") + 1]).write_bytes(raw if raw is not None else json.dumps(data).encode())
            return code, b"[]"
        self.service.runner.run.side_effect = command
        response = await self.post("shop", {})
        self.assertEqual(response.status, 202)
        await self.finish()
        return self.service.latest

    async def test_results_sanitization_confirmation_restart(self):
        await self.login()
        data = {"status": "attempt_saved", "success": True, "attempt": 7,
                "diagnostic": {"message": "SECRET"},
                "summary": {"stage": "slot_selected", "slot": "Tomorrow", "file": "/private/SECRET",
                            "cart": [{"name": "Milk", "quantity": 2, "unit_price": 4, "diagnostic": "SECRET"}],
                            "item_outcomes": [{"item_name": "milk", "selection_mode": "exact",
                                               "product": {"name": "Milk", "unit_price": 4, "file": "SECRET"}}]}}
        run = await self.run_result(data)
        self.assertEqual(run["status"], "complete")
        self.assertNotIn("SECRET", json.dumps(run))
        self.assertEqual(os.stat(self.service.path).st_mode & 0o777, 0o600)
        self.assertEqual((await self.post("confirm", {"attempt": 7})).status, 400)
        self.assertEqual((await self.post("confirm", {"attempt": 8, "confirmed": True})).status, 409)
        self.assertEqual((await self.post("confirm", {"attempt": 7, "confirmed": True})).status, 200)
        self.assertTrue(self.service.latest["confirmed"])
        restored = ShoppingWeb(self.db, PASSWORD, ORIGIN, False)
        self.assertEqual(restored.latest, self.service.latest)
        restored.latest["status"] = "running"
        restored.save()
        restored = ShoppingWeb(self.db, PASSWORD, ORIGIN, False)
        self.assertEqual(restored.latest["status"], "incomplete")
        self.assertFalse(restored.tasks)

    async def test_summary_unresolved_lists_survive_state_and_restart(self):
        await self.login()
        data = {"status": "attempt_saved", "success": True, "attempt": 7,
                "summary": {"stage": "slot_selected", "slot": "Tomorrow", "cart": [],
                            "unresolved": ["milk"], "missing_or_over_cap": ["eggs", "bread"],
                            "item_outcomes": [
                                {"item_name": name, "selection_mode": "exact",
                                 "product": {"name": name, "unit_price": 4}}
                                for name in ("milk", "eggs", "rice")]}}
        run = await self.run_result(data)
        self.assertEqual(run["status"], "incomplete")
        self.assertEqual(run["unresolved"], ["milk"])
        self.assertEqual(run["missing_or_over_cap"], ["eggs", "bread"])
        self.assertEqual([row["unresolved"] for row in run["outcomes"]], [True, True, False])
        response = await self.client.get("/api/state")
        self.assertEqual((await response.json())["run"], run)
        self.assertEqual(ShoppingWeb(self.db, PASSWORD, ORIGIN, False).latest, run)

    def test_summary_list_validation_bounds(self):
        for key in ("unresolved", "missing_or_over_cap"):
            for value in (None, "milk", [1], [""], ["x" * 1001], ["milk"] * 1001):
                with self.subTest(key=key, value_type=type(value).__name__):
                    with self.assertRaises(ValueError):
                        clean_run({"id": "test", "status": "incomplete", key: value})
            boundary = ["x" * 1000] * 1000
            self.assertEqual(clean_run({"id": "test", "status": "incomplete", key: boundary})[key], boundary)

    async def test_failed_and_unverified_results(self):
        await self.login()
        cases = [({"status": "no_summary", "diagnostic": {"message": "SECRET"}}, 0, "incomplete"),
                 ({"status": "error", "diagnostic": {"message": "SECRET"}}, 1, "error"),
                 ({"status": "readiness_failed"}, 1, "incomplete"),
                 ({"status": "no_summary"}, 2, "error"),
                 ({"status": "no_summary"}, 143, "incomplete"),
                 ({"status": "attempt_saved", "success": True}, 0, "error")]
        for data, code, expected in cases:
            with self.subTest(data=data, code=code):
                with patch("web_app.record_diagnostic") as diagnostic:
                    result = await self.run_result(data, code)
                    self.assertEqual(result["status"], expected)
                    self.assertNotIn("SECRET", json.dumps(result))
                    diagnostic.assert_called()
        self.assertEqual((await self.run_result({}, raw=b"x" * 65537))["status"], "error")
        self.assertEqual((await self.run_result({}, raw=b"not json"))["status"], "error")

    async def test_nonzero_cannot_complete_and_missing_file(self):
        await self.login()
        data = {"status": "attempt_saved", "success": True, "attempt": 1,
                "summary": {"stage": "slot_selected", "slot": "Tomorrow", "cart": []}}
        self.assertEqual((await self.run_result(data, 1))["status"], "incomplete")
        self.service.runner.run.side_effect = None
        self.service.runner.run.return_value = (0, b"")
        await self.post("shop", {})
        await self.finish()
        self.assertEqual(self.service.latest["status"], "error")

    async def test_shutdown_terminates_composed_subprocess(self):
        del self.service.runner.run
        entered = asyncio.Event()
        process = Mock(returncode=None)

        async def wait():
            entered.set()
            if process.returncode is None:
                await asyncio.Event().wait()
            return process.returncode

        def terminate():
            process.returncode = 143

        process.wait = AsyncMock(side_effect=wait)
        process.terminate.side_effect = terminate
        with patch("telegram_bot.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            await self.login()
            await self.post("shop", {})
            await entered.wait()
            await self.service.shutdown(self.app)
        process.terminate.assert_called_once()
        self.assertEqual(self.service.latest["status"], "incomplete")
        self.assertFalse(self.service.lock.locked())

    def test_configuration(self):
        for origin in ("https://host/path", "https://user:pass@host", "https://host:99999", "file://host", "https://host/#x"):
            with self.assertRaises(ValueError):
                create_app(self.db, PASSWORD, origin)
        with self.assertRaises(ValueError):
            create_app(self.db, "short", ORIGIN)
        self.assertTrue(create_app(self.db, PASSWORD, ORIGIN)[SERVICE].secure)


if __name__ == "__main__":
    unittest.main()
