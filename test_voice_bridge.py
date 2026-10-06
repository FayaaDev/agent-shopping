import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer

from shopping import open_db
import voice_bridge as bridge
import voice_shortcut as voice


def action(kind, name=None, quantity=None, max_price=None):
    return dict(kind=kind, name=name, quantity=quantity, max_price=max_price, location=None)


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = Path(self.temporary.name).resolve() / "shopping.db"
        with contextlib.closing(open_db(self.db)) as connection:
            connection.execute("INSERT INTO items VALUES ('Milk',2,'Preferred','sku','Brand',8.5)")
            connection.execute("INSERT INTO alternatives VALUES ('Milk','Alternative','alt-sku')")
            connection.execute("INSERT INTO items(name,quantity) VALUES ('Bread',1)")
            connection.commit()
        self.secret = "s" * 32
        self.app = bridge.create_app(self.db, self.secret)
        self.service = self.app[bridge.BRIDGE]
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.model = AsyncMock(return_value={"clarification": None, "actions": [action("set", "Milk", 5)]})
        model_patch = patch.object(voice, "interpret", self.model)
        model_patch.start()
        self.addCleanup(model_patch.stop)
        busy_patch = patch.object(self.service, "busy", return_value=False)
        busy_patch.start()
        self.addCleanup(busy_patch.stop)
        self.child = Mock(returncode=None)
        self.child.poll.return_value = None
        # All launches are mocked, including unexpected paths.
        launch_patch = patch.object(bridge.subprocess, "Popen", return_value=self.child)
        self.launch = launch_patch.start()
        self.addCleanup(launch_patch.stop)

    async def asyncTearDown(self):
        self.child.poll.return_value = 0
        self.child.returncode = 0
        await self.client.close()

    async def request(self, method, path, data=None, authenticated=True):
        headers = {"Authorization": "Bearer " + self.secret} if authenticated else {}
        response = await self.client.request(method, path, json=data, headers=headers)
        return response.status, await response.json()

    async def preview(self):
        code, data = await self.request("POST", "/preview", {"transcript": "set Milk to 5"})
        self.assertEqual(code, 200, data)
        return data

    def edit_saved(self, token, **changes):
        path = self.service.root / token / "plan.json"
        voice.write_json(path, {**voice.read_json(path), **changes})

    async def test_auth_all_routes_and_strong_configuration(self):
        for method, path, data in [("GET", "/list", None), ("POST", "/preview", {"transcript": "milk"}),
                                   ("POST", "/confirm", {"token": "a" * 32}),
                                   ("GET", "/status/" + "a" * 32, None)]:
            code, result = await self.request(method, path, data, authenticated=False)
            self.assertEqual(code, 401)
            self.assertEqual(result["message"], "Unauthorized.")
        self.model.assert_not_called()
        self.launch.assert_not_called()
        for token in ["", "x" * 31, "x" * 32 + "\n", "é" * 32]:
            with self.assertRaises(ValueError):
                bridge.create_app(self.db, token)
        with patch.dict(os.environ, {"VOICE_BRIDGE_TOKEN": self.secret, "SHOPPING_DB": str(self.db)}):
            self.assertEqual(bridge.create_app()[bridge.BRIDGE].db, self.db)

    async def test_preview_merged_preferences_caps_and_persisted_shop(self):
        data = await self.preview()
        self.assertEqual([a["kind"] for a in data["actions"]], ["set", "shop"])
        before = voice.saved_items(self.db)
        expected = [dict(item) for item in before]
        expected[1]["quantity"] = 5
        self.assertEqual(data["items"], expected)
        saved = voice.read_json(self.service.root / data["token"] / "plan.json")
        self.assertEqual(saved["plan"]["actions"], data["actions"])
        self.assertEqual(voice.saved_items(self.db), before)
        code, listed = await self.request("GET", "/list")
        self.assertEqual(code, 200)
        self.assertEqual(listed["items"], before)
        with patch.object(voice, "run_shop", return_value={"status": "completed", "message": "mock"}):
            outcome = voice.worker(self.db, data["token"])
        self.assertEqual(outcome["status"], "completed")
        self.assertEqual(voice.saved_items(self.db), data["items"])
        self.launch.assert_not_called()

    async def test_new_item_clear_remove_caps_and_old_default(self):
        self.model.return_value["actions"] = [action("remove", "Bread"), action("set", "Milk", 4, 7),
                                              action("set", "Eggs", 3)]
        data = await self.preview()
        self.assertEqual([item["name"] for item in data["items"]], ["Eggs", "Milk"])
        self.assertEqual(data["items"][1]["max_price"], 7)
        self.model.return_value["actions"] = [action("clear"), action("set", "Milk", 1)]
        data = await self.preview()
        self.assertIsNone(data["items"][0]["brand"])
        self.assertEqual(data["items"][0]["alternatives"], [])
        old = await asyncio.to_thread(voice.preview, self.db, "clear")
        saved = voice.read_json(self.service.root / old["token"] / "plan.json")
        self.assertNotIn("shop", [a["kind"] for a in saved["plan"]["actions"]])
        self.assertNotIn("items", old)
        self.model.return_value["actions"] = [action("shop")]
        data = await self.preview()
        self.assertEqual([a["kind"] for a in data["actions"]], ["shop"])

    async def test_clarification_invalid_input_and_redaction(self):
        for data in [{"transcript": " "}, {"transcript": "a" * 4097}, {"transcript": "\x00"},
                     {"transcript": 1}, {"transcript": "milk", "shop": True}, []]:
            code, _ = await self.request("POST", "/preview", data)
            self.assertEqual(code, 400)
        response = await self.client.post("/preview", data=b"x" * 8193,
                                          headers={"Authorization": "Bearer " + self.secret,
                                                   "Content-Type": "application/json"})
        self.assertEqual(response.status, 413)
        self.model.return_value = {"clarification": "How many?", "actions": []}
        code, data = await self.request("POST", "/preview", {"transcript": "milk"})
        self.assertEqual(data["status"], "clarification")
        self.assertNotIn("token", data)
        self.model.side_effect = RuntimeError("SECRET model credential")
        code, data = await self.request("POST", "/preview", {"transcript": "milk"})
        self.assertEqual(code, 500)
        self.assertNotIn("SECRET", json.dumps(data))
        for token in ["../plan.json", "A" * 32, "a" * 31]:
            code, _ = await self.request("POST", "/confirm", {"token": token})
            self.assertEqual(code, 400)

    async def test_expiry_stale_busy_and_workflow_lock_prevent_launch(self):
        data = await self.preview()
        token = data["token"]
        self.edit_saved(token, expires=time.time() - 1)
        _, result = await self.request("POST", "/confirm", {"token": token})
        self.assertEqual(result["status"], "expired")
        data = await self.preview()
        token = data["token"]
        with patch.object(self.service, "busy", return_value=True):
            code, _ = await self.request("POST", "/confirm", {"token": token})
            self.assertEqual(code, 409)
        with voice.locked(self.service.root / "workflow.lock"):
            code, _ = await self.request("POST", "/confirm", {"token": token})
            self.assertEqual(code, 409)
            code, _ = await self.request("POST", "/preview", {"transcript": "milk"})
            self.assertEqual(code, 409)
        with contextlib.closing(open_db(self.db)) as connection:
            connection.execute("UPDATE items SET quantity=6")
            connection.commit()
        code, result = await self.request("POST", "/confirm", {"token": token})
        self.assertEqual(code, 409)
        self.assertIn("changed", result["message"])
        self.assertFalse((self.service.root / token / "started.json").exists())
        self.launch.assert_not_called()
        with patch.object(voice, "run_shop") as shop, patch.object(voice, "diagnostic"):
            outcome = voice.worker(self.db, token)
        self.assertEqual(outcome["status"], "failed")
        shop.assert_not_called()

    async def test_worker_rechecks_expiry_after_confirm_and_terminal_durability(self):
        token = (await self.preview())["token"]
        await self.request("POST", "/confirm", {"token": token})
        self.edit_saved(token, expires=time.time() - 1)
        with patch.object(voice, "run_shop") as shop, patch.object(voice, "diagnostic"):
            outcome = voice.worker(self.db, token)
        self.assertEqual(outcome["status"], "failed")
        shop.assert_not_called()
        self.child.poll.return_value = 0
        self.child.returncode = 0
        restarted = bridge.Bridge(self.db)
        self.assertEqual(restarted.handle("status", token)["status"], "failed")
        self.assertEqual(restarted.handle("confirm", token)["status"], "failed")
        self.launch.assert_called_once()

    async def test_concurrent_confirm_reservation_launch_and_restart(self):
        token = (await self.preview())["token"]

        def launch(*args, **kwargs):
            self.assertTrue((self.service.root / token / "started.json").exists())
            self.assertEqual(voice.read_json(self.service.root / "active.json")["token"], token)
            with voice.locked(self.service.root / "workflow.lock"):
                pass
            return self.child

        self.launch.side_effect = launch
        results = await asyncio.gather(*(self.request("POST", "/confirm", {"token": token}) for _ in range(2)))
        self.assertTrue(all(result[1]["status"] == "running" for result in results))
        self.launch.assert_called_once()
        args, kwargs = self.launch.call_args
        self.assertEqual(args[0], [sys.executable, str(bridge.PROJECT / "voice_shortcut.py"), "worker", token])
        self.assertEqual(kwargs["env"]["SHOPPING_DB"], str(self.db))
        self.assertEqual(kwargs["cwd"], bridge.PROJECT)
        self.assertTrue(kwargs["start_new_session"])
        restarted = bridge.Bridge(self.db)
        self.assertEqual(restarted.handle("confirm", token)["status"], "interrupted")
        other = (await self.preview())["token"]
        with self.assertRaises(voice.Rejected):
            restarted.handle("confirm", other)
        voice.write_json(self.service.root / token / "status.json", {"status": "completed", "message": "Cart prepared."})
        self.assertEqual(self.service.status(token)["status"], "running")
        self.child.returncode = 0
        self.child.poll.return_value = 0
        _, result = await self.request("GET", "/status/" + token)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(restarted.handle("status", token), result)
        self.assertEqual(restarted.handle("confirm", token), result)
        self.launch.assert_called_once()

    async def test_uncertain_launch_never_retried_even_after_restart(self):
        token = (await self.preview())["token"]
        self.launch.side_effect = OSError("SECRET launch failure")
        _, result = await self.request("POST", "/confirm", {"token": token})
        self.assertEqual(result["status"], "interrupted")
        self.assertNotIn("SECRET", json.dumps(result))
        await self.request("POST", "/confirm", {"token": token})
        other = (await self.preview())["token"]
        restarted = bridge.Bridge(self.db)
        self.assertEqual(restarted.handle("confirm", token)["status"], "interrupted")
        with self.assertRaises(voice.Rejected):
            restarted.handle("confirm", other)
        self.launch.assert_called_once()

    async def test_shutdown_signals_child_and_awaits_cleanup(self):
        token = (await self.preview())["token"]
        await self.request("POST", "/confirm", {"token": token})
        waited = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def wait():
            loop.call_soon_threadsafe(waited.set)
            release.wait(timeout=5)

        self.child.wait.side_effect = wait
        cleanup = asyncio.create_task(self.service.cleanup(self.app))
        try:
            await asyncio.wait_for(waited.wait(), timeout=2)
            self.assertFalse(cleanup.done())
            self.child.send_signal.assert_called_once_with(signal.SIGTERM)
        finally:
            release.set()
            await cleanup
        code, _ = await self.request("POST", "/confirm", {"token": token})
        self.assertEqual(code, 503)

    async def test_browser_process_detection_excludes_bridge_self(self):
        def busy(output):
            with patch.object(bridge.subprocess, "run", return_value=Mock(stdout=output)):
                return bridge.Bridge.busy(self.service)

        self.assertFalse(busy(f"{os.getpid()} 12 python telegram_bot.py\n12 1 uv run telegram_bot.py\n1 0 docker-init -- telegram_bot.py\n999999 1 python voice_bridge.py\n"))
        for args in ["python shopping.py --db shopping.db shop", "python /project/shopping.py setup",
                     "python shopping.py readiness", "python telegram_bot.py",
                     "chromium --user-data-dir=/project/.browser-profile",
                     "chromium --user-data-dir /project/.browser-profile"]:
            self.assertTrue(busy(f"{os.getpid()} 12 python telegram_bot.py\n12 1 uv run telegram_bot.py\n1 0 docker-init -- telegram_bot.py\n999999 1 " + args))
        self.assertFalse(busy("999999 1 chromium --user-data-dir=/tmp/isolated"))

    async def test_main_binds_only_loopback(self):
        previous_mask = os.umask(0o077)
        self.addCleanup(os.umask, previous_mask)
        with patch.object(sys, "argv", ["voice_bridge.py", "--port", "8766"]), \
                patch.object(bridge, "create_app", return_value=self.app), \
                patch.object(bridge.web, "run_app") as serve:
            bridge.main()
        serve.assert_called_once_with(self.app, host="127.0.0.1", port=8766, access_log=None, print=None)


if __name__ == "__main__":
    unittest.main()
