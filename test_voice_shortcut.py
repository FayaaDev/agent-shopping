import asyncio
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import voice_shortcut as voice
from shopping import open_db, read_items


def action(kind, name=None, quantity=None, location=None, max_price=None):
    return dict(kind=kind, name=name, quantity=quantity, location=location, max_price=max_price)


def plan(*actions, clarification=None):
    return dict(clarification=clarification, actions=list(actions))


def container(token, running=True, exit_code=0, mounts=(), labels=None):
    return {"Name": "/shopping-voice-" + token,
            "State": {"Running": running, "ExitCode": exit_code},
            "Config": {"Labels": labels or {}},
            "Mounts": [{"Source": str(p)} for p in mounts]}


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        data = self.project / "server-data/data"
        data.mkdir(parents=True)
        self.db = data / "shopping.db"
        with contextlib.closing(open_db(self.db)) as connection:
            connection.execute("INSERT INTO items(name,quantity,brand) VALUES ('Milk 1L',2,'Saved brand')")
            connection.execute("INSERT INTO alternatives VALUES ('Milk 1L','Alternative',NULL)")
            connection.commit()
        self.gateway = voice.Gateway(self.project)
        self.gateway.command = Mock(return_value="")
        self.gateway.containers = Mock(return_value=[])

    def items(self):
        with contextlib.closing(open_db(self.db)) as connection:
            return read_items(connection)

    def preview(self, *actions):
        with patch.object(voice, "interpret", AsyncMock(return_value=plan(*actions))):
            response = voice.preview(self.db, "set milk target")
        return response["token"]

    def test_protocol_rejects_injection_and_noncanonical_input(self):
        text = "أضف حليب قليل الدسم ٢"
        encoded = base64.b64encode(text.encode()).decode()
        self.assertEqual(voice.parse_command("preview " + encoded), ("preview", text))
        for command in ["list; id", "list\n", "confirm " + "a" * 32 + ";id", "status ../file",
                        "preview $$$", "preview YQ==\n", "preview YR==", "preview AA==",
                        "preview " + base64.b64encode(b"a" * 4097).decode(), "preview /w=="]:
            with self.subTest(command=command[:60]), self.assertRaises(voice.Rejected):
                voice.parse_command(command)

    def test_actual_model_api_structured_text_and_timeout(self):
        schema = voice.plan_schema()
        model = Mock()
        model.ainvoke = AsyncMock(return_value=Mock(completion=schema.model_validate(plan(action("list")))))
        with patch("shopping.create_llm", return_value=model):
            result = asyncio.run(voice.interpret("show list", self.items()))
        self.assertEqual(result["actions"][0]["kind"], "list")
        args, kwargs = model.ainvoke.call_args
        self.assertIn("saved_items", args[0][1].content)
        self.assertIn("Milk 1L", args[0][1].content)
        self.assertEqual(kwargs["output_format"].__name__, "Plan")
        self.assertIn("add more", args[0][0].content)
        with patch("shopping.create_llm", return_value=model), patch.object(voice.asyncio, "wait_for", side_effect=TimeoutError):
            # Close the mocked coroutine ourselves when wait_for is replaced.
            model.ainvoke = Mock(return_value=None)
            with self.assertRaises(TimeoutError):
                asyncio.run(voice.interpret("show list", []))

    def test_strict_plan_and_ambiguous_add_more_no_token(self):
        schema = voice.plan_schema()
        for invalid in [plan(action("set", "milk", True)), plan(action("set", "milk", 0)),
                        plan(action("shop"), action("list")), plan(action("list", "milk")),
                        plan(action("shop", location=" ")), plan(action("shop", location="Riyadh\n")),
                        plan(action("shop", location="Riyadh\x7f")), plan(action("shop", location="Riyadh\x85")),
                        plan(action("set", "milk", 2, max_price=0)), plan(action("set", "milk", 2, max_price=True)),
                        plan(action("set", "milk", 2, max_price=float("inf"))),
                        plan(action("set", "milk", 2, max_price=float("nan"))),
                        plan(action("shop", max_price=5)),
                        plan(action("clear"), clarification="unclear"),
                        {**plan(action("list")), "shell": "id"}]:
            with self.assertRaises(ValueError):
                schema.model_validate(invalid)
        with patch.object(voice, "interpret", AsyncMock(return_value=plan(clarification="What absolute target quantity?"))):
            result = voice.preview(self.db, "add more milk")
        self.assertEqual(result["status"], "clarification")
        self.assertNotIn("token", result)
        self.assertEqual(len(self.items()), 1)

    def test_confirm_exact_plan_attributes_target_and_replay(self):
        token = self.preview(action("set", "Milk 1L", 5),
                             action("set", "Organic vanilla yogurt 3×170g max 10 SAR", 2, max_price=10))
        with patch.object(voice, "interpret", side_effect=AssertionError("Never reparse")), patch.object(voice, "run_shop") as shop:
            self.assertEqual(voice.worker(self.db, token)["status"], "completed")
            self.assertEqual(voice.worker(self.db, token)["status"], "failed")
            shop.assert_not_called()
        items = self.items()
        self.assertEqual(items[0]["quantity"], 5)
        self.assertEqual(items[0]["brand"], "Saved brand")
        self.assertEqual(items[0]["alternatives"][0]["name"], "Alternative")
        self.assertEqual(items[1]["quantity"], 2)
        self.assertEqual(items[1]["max_price"], 10)

    def test_price_cap_preview_persistence_preservation_and_enforcement(self):
        with patch.object(voice, "interpret", AsyncMock(return_value=plan(action("set", "Milk 1L", 3, max_price=8.5)))):
            preview = voice.preview(self.db, "set milk to 3, no more than 8.5 SAR per unit")
        self.assertIn("unit-price cap 8.5 SAR", preview["message"])
        self.assertEqual(voice.worker(self.db, preview["token"])["status"], "completed")
        item = self.items()[0]
        self.assertEqual(item["max_price"], 8.5)
        # Exercise the real shopping guard: the numeric cap, not a name label, blocks excess price.
        from shopping import selection_reason
        self.assertEqual(selection_reason(item, {"item_name": "Milk 1L", "selection_mode": "exact",
                                                "product": {"name": "Milk 1L", "unit_price": 8.6}}), "price_cap")
        token = self.preview(action("set", "Milk 1L", 4))
        self.assertEqual(voice.worker(self.db, token)["status"], "completed")
        self.assertEqual(self.items()[0]["max_price"], 8.5)
        token = self.preview(action("set", "Milk 1L", 4, max_price=7))
        self.assertEqual(voice.worker(self.db, token)["status"], "completed")
        self.assertEqual(self.items()[0]["max_price"], 7)

    def test_expiry_stale_list_and_atomic_batch_rollback(self):
        token = self.preview(action("set", "Milk 1L", 9))
        path = self.gateway.root / token / "plan.json"
        saved = voice.read_json(path)
        voice.write_json(path, {**saved, "expires": time.time() - 1})
        self.assertEqual(voice.worker(self.db, token)["status"], "failed")
        self.assertEqual(self.gateway.handle("confirm " + token)["status"], "expired")
        token = self.preview(action("set", "Milk 1L", 9))
        with contextlib.closing(open_db(self.db)) as connection:
            connection.execute("UPDATE items SET quantity=3,max_price=6")
            connection.commit()
        self.assertIn("changed", voice.worker(self.db, token)["message"])
        token = self.preview(action("set", "Milk 1L", 9, max_price=4), action("set", "fail", 2))
        with contextlib.closing(open_db(self.db)) as connection:
            connection.execute("CREATE TRIGGER fail_item BEFORE INSERT ON items WHEN NEW.name='fail' "
                               "BEGIN SELECT RAISE(ABORT,'private error'); END")
        with patch.object(voice, "diagnostic"):
            self.assertEqual(voice.worker(self.db, token)["status"], "failed")
        self.assertEqual(self.items()[0]["quantity"], 3)
        self.assertEqual(self.items()[0]["max_price"], 6)

    def test_lock_held_through_child_and_cleanup(self):
        token = self.preview(action("set", "Milk 1L", 7), action("shop"))

        def shopping(*args):
            with self.assertRaises(voice.Rejected):
                with voice.locked(self.gateway.root / "workflow.lock"):
                    pass
            return {"status": "failed", "message": voice.FAILURE}

        with patch.object(voice, "run_shop", side_effect=shopping):
            self.assertEqual(voice.worker(self.db, token)["status"], "failed")
        with voice.locked(self.gateway.root / "workflow.lock"):
            pass
        other = self.preview(action("clear"))
        with voice.locked(self.gateway.root / "workflow.lock"), patch.object(voice, "diagnostic"):
            self.assertEqual(voice.worker(self.db, other)["status"], "failed")
        self.assertEqual(self.items()[0]["quantity"], 7)

    def test_gateway_dedupe_lost_launch_reply_and_crash_detection(self):
        token = self.preview(action("shop"))
        self.gateway.compose = Mock(side_effect=TimeoutError("private"))
        first = self.gateway.handle("confirm " + token)
        self.assertEqual(first["status"], "interrupted")
        self.assertEqual(self.gateway.handle("confirm " + token)["status"], "interrupted")
        self.assertEqual(self.gateway.compose.call_count, 1)
        other = self.preview(action("shop"))
        with self.assertRaises(voice.Rejected):
            self.gateway.handle("confirm " + other)
        self.gateway.containers.return_value = [container(token)]
        self.assertEqual(self.gateway.handle("status " + token)["status"], "running")
        voice.write_json(self.gateway.root / token / "status.json", {"status": "completed", "message": "done"})
        self.assertEqual(self.gateway.handle("status " + token)["status"], "running")
        self.gateway.containers.return_value = [container(token, running=False, exit_code=137)]
        self.assertEqual(self.gateway.handle("status " + token)["status"], "interrupted")
        self.gateway.containers.return_value = [container(token, running=False)]
        self.assertEqual(self.gateway.handle("status " + token)["status"], "completed")

    def test_profile_owners_readers_and_host_processes(self):
        profile = self.project / "server-data/browser-profile"

        def external(**kwargs):
            value = container("unused", **kwargs)
            value["Name"] = "/external-browser"
            return value

        self.assertTrue(self.gateway.busy([external(mounts=[profile])]))
        self.assertFalse(self.gateway.busy([external(mounts=[profile], labels={"shopping.voice.reader": "true"})]))
        self.assertFalse(self.gateway.busy([external(running=False, mounts=[profile])]))
        self.assertFalse(self.gateway.busy([external(mounts=[self.project / "unrelated"])]))
        renamed = external(labels={"com.docker.compose.project.working_dir": str(self.project),
                                   "com.docker.compose.service": "bot"})
        self.assertTrue(self.gateway.busy([renamed]))
        self.assertTrue(self.gateway.busy([external(mounts=[profile.parent])]))
        self.gateway.command.return_value = "python shopping.py --db /data/shopping.db shop"
        self.assertTrue(self.gateway.busy([]))
        token = self.preview(action("clear"))
        with self.assertRaises(voice.Rejected):
            self.gateway.handle("confirm " + token)
        self.assertFalse((self.gateway.root / token / "started.json").exists())

    def test_compose_no_shell_no_restart_fixed_script_mount(self):
        self.gateway.compose("worker", "a" * 32, detached=True)
        args = self.gateway.command.call_args.args[0]
        self.assertIn("--no-deps", args)
        self.assertIn("-T", args)
        self.assertIn("-d", args)
        self.assertEqual(args[-4:], ["python", "voice_shortcut.py", "worker", "a" * 32])
        self.assertEqual(json.loads(self.gateway.command.call_args.kwargs["input"])["services"]["bot"]["restart"], "no")

    def test_container_inspection_compact_ndjson_without_environment(self):
        ids = [f"id{i}" for i in range(45)]

        def command(args):
            if args[:3] == ["docker", "ps", "-aq"]:
                return "\n".join(ids)
            self.assertEqual(args[:3], ["docker", "inspect", "--format"])
            template = args[3]
            self.assertNotIn(".Config.Env", template)
            self.assertNotIn("{{json .Config}}", template)
            self.assertNotIn("{{json .State}}", template)
            self.assertNotIn("{{json .Mounts}}", template)
            self.assertIn(".Config.Labels", template)
            self.assertIn("$m.Source", template)
            self.assertLessEqual(len(args[4:]), 20)
            return "\n".join(json.dumps(container(i)) for i in args[4:]) + "\n"

        self.gateway.command = Mock(side_effect=command)
        result = voice.Gateway.containers(self.gateway)
        self.assertEqual([c["Name"] for c in result], ["/shopping-voice-" + i for i in ids])
        self.assertEqual(self.gateway.command.call_count, 4)

    def test_single_launch_deduplicates_and_readers_remain_available(self):
        token = self.preview(action("shop"))
        other = self.preview(action("set", "Milk 1L", 8))
        self.gateway.compose = Mock(return_value="container-id")
        self.assertEqual(self.gateway.handle("confirm " + token)["status"], "running")
        self.gateway.containers.return_value = [container(token)]
        self.assertEqual(self.gateway.handle("confirm " + token)["status"], "running")
        self.gateway.compose.assert_called_once_with("worker", token, detached=True)
        with self.assertRaises(voice.Rejected):
            self.gateway.handle("confirm " + other)
        self.gateway.compose.return_value = json.dumps({"status": "list", "message": "Milk 1L × 2"})
        self.assertEqual(self.gateway.handle("list")["status"], "list")
        self.gateway.containers.return_value = [container(token, running=False)]
        with voice.locked(self.gateway.root / "workflow.lock"):
            with self.assertRaises(voice.Rejected):
                self.gateway.handle("confirm " + other)
        self.assertFalse((self.gateway.root / other / "started.json").exists())

    def test_child_failure_missing_result_timeout_cleanup_and_safe_summary(self):
        directory = self.gateway.root / ("a" * 32)
        directory.mkdir()
        result = directory / "result.json"
        child = Mock()
        child.wait.return_value = 0
        child.poll.return_value = 0
        child.returncode = 0
        summary = {"status": "attempt_saved", "success": True, "attempt": 1,
                   "summary": {"cart": [{"name": "Milk 1L", "quantity": 2, "unit_price": 8}],
                               "stage": "slot_selected", "slot": "Tomorrow"}}
        voice.write_json(result, summary)
        with patch.object(voice.subprocess, "Popen", return_value=child) as popen:
            outcome = voice.run_shop(self.db, directory, action("shop", location="Riyadh"))
        self.assertEqual(outcome["status"], "completed")
        self.assertIn("Manual approval", outcome["message"])
        self.assertNotIn("shell", popen.call_args.kwargs)
        self.assertEqual(popen.call_args.args[0][-2:], ["--location", "Riyadh"])
        result.unlink()
        with patch.object(voice.subprocess, "Popen", return_value=child), patch.object(voice, "diagnostic"):
            self.assertEqual(voice.run_shop(self.db, directory, action("shop"))["status"], "failed")
        voice.write_json(result, {"status": "no_summary", "diagnostic": {"message": "SECRET"}})
        with patch.object(voice.subprocess, "Popen", return_value=child), patch.object(voice, "diagnostic") as log:
            outcome = voice.run_shop(self.db, directory, action("shop"))
            log.assert_called_once()
        self.assertEqual(outcome["status"], "incomplete")
        self.assertNotIn("SECRET", json.dumps(outcome))
        child.wait.side_effect = [subprocess.TimeoutExpired("shop", 1800), subprocess.TimeoutExpired("shop", 35), -9]
        child.returncode = -9
        child.pid = 12345
        with patch.object(voice.subprocess, "Popen", return_value=child), patch.object(voice.os, "killpg") as kill, patch.object(voice, "diagnostic"):
            outcome = voice.run_shop(self.db, directory, action("shop"))
        self.assertEqual(outcome["status"], "interrupted")
        kill.assert_called_once_with(12345, signal.SIGKILL)
        child.terminate.assert_called()

    def test_readiness_and_unverified_shopping_never_complete(self):
        directory = self.gateway.root / ("c" * 32)
        directory.mkdir()
        child = Mock(returncode=0)
        child.wait.return_value = 0
        child.poll.return_value = 0
        cart = [{"name": "Milk 1L", "quantity": 2, "unit_price": 8}]
        base = {"cart": cart, "stage": "slot_selected", "slot": "Tomorrow"}
        cases = [({"status": "readiness_failed"}, 0),
                 ({"status": "no_summary"}, 0)]
        for changes, success, code in [({"stage": "cart_review"}, True, 0),
                                       ({"slot": None}, True, 0),
                                       ({"unresolved": ["Milk 1L"]}, True, 0),
                                       ({"missing_or_over_cap": ["Milk 1L"]}, True, 0),
                                       ({"item_outcomes": [{"item_name": "Milk 1L", "selection_mode": None,
                                                            "unresolved_reason": "price_cap"}]}, True, 0),
                                       ({"cart": []}, True, 0), ({}, False, 0), ({}, True, 1)]:
            cases.append(({"status": "attempt_saved", "success": success, "attempt": 1,
                           "summary": {**base, **changes}}, code))
        for data, code in cases:
            with self.subTest(data=data, code=code):
                child.wait.return_value = code
                child.returncode = code
                voice.write_json(directory / "result.json", data)
                with patch.object(voice.subprocess, "Popen", return_value=child), patch.object(voice, "diagnostic"):
                    outcome = voice.run_shop(self.db, directory, action("shop"))
                self.assertEqual(outcome["status"], "incomplete")
                self.assertNotIn("Cart prepared.", outcome["message"])
                voice.write_json(directory / "plan.json", {"expires": time.time() + 100})
                voice.write_json(directory / "status.json", outcome)
                # A result file is not terminal until its container exits after cleanup.
                self.assertEqual(self.gateway.status(directory.name, [container(directory.name)])["status"], "running")
                self.assertEqual(self.gateway.status(directory.name, [container(directory.name, running=False)])["status"], "incomplete")

    def test_sigterm_waits_for_child_cleanup_before_terminal_result(self):
        directory = self.gateway.root / ("b" * 32)
        directory.mkdir()
        voice.write_json(directory / "result.json", {"status": "no_summary"})
        child = Mock(returncode=-15)
        child.poll.return_value = -15
        previous = signal.getsignal(signal.SIGTERM)
        calls = 0

        def wait(timeout):
            nonlocal calls
            calls += 1
            if calls == 1:
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return -15

        child.wait.side_effect = wait
        with patch.object(voice.subprocess, "Popen", return_value=child), patch.object(voice, "diagnostic"):
            result = voice.run_shop(self.db, directory, action("shop"))
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(calls, 2)
        child.send_signal.assert_called_once_with(signal.SIGTERM)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_invalid_child_result_is_private_and_releases_lock(self):
        token = self.preview(action("shop"))
        directory = self.gateway.root / token
        voice.write_json(directory / "result.json", {"status": "attempt_saved", "diagnostic": {"message": "SECRET"}})
        child = Mock(returncode=0)
        child.wait.return_value = 0
        child.poll.return_value = 0
        with patch.object(voice.subprocess, "Popen", return_value=child), patch.object(voice, "diagnostic") as log:
            outcome = voice.worker(self.db, token)
        self.assertEqual(outcome["status"], "failed")
        self.assertNotIn("SECRET", json.dumps(outcome))
        log.assert_called_once()
        with voice.locked(self.gateway.root / "workflow.lock"):
            pass

    def test_sigterm_racing_child_exit_still_reports_interrupted(self):
        directory = self.gateway.root / ("d" * 32)
        directory.mkdir()
        voice.write_json(directory / "result.json", {"status": "readiness_failed"})
        child = Mock(returncode=0)
        child.poll.return_value = 0
        child.send_signal.side_effect = ProcessLookupError("child already gone")
        previous = signal.getsignal(signal.SIGTERM)
        waits = 0

        def wait(timeout):
            nonlocal waits
            waits += 1
            if waits == 1:
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return 0

        child.wait.side_effect = wait
        with patch.object(voice.subprocess, "Popen", return_value=child), patch.object(voice, "diagnostic"):
            self.assertEqual(voice.run_shop(self.db, directory, action("shop"))["status"], "interrupted")
        self.assertEqual(waits, 2)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_clear_preserves_purchase_history_and_list_browser_free(self):
        with contextlib.closing(open_db(self.db)) as connection:
            connection.execute("INSERT INTO attempts(summary) VALUES ('{}')")
            connection.commit()
        token = self.preview(action("clear"), action("list"))
        with patch.object(voice, "run_shop", side_effect=AssertionError("No browser")):
            self.assertEqual(voice.worker(self.db, token)["message"], "Saved list empty.")
        with contextlib.closing(open_db(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM attempts").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM alternatives").fetchone()[0], 0)

    def test_host_cli_stdlib_only_clean_safe_json(self):
        command = [sys.executable, "-S", str(Path(voice.__file__)), "gateway", "--project", str(self.project)]
        result = subprocess.run(command, capture_output=True, text=True,
                                env={**os.environ, "SSH_ORIGINAL_COMMAND": "list; echo SECRET"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["status"], "rejected")
        self.assertNotIn("SECRET", result.stdout + result.stderr)

    def test_permission_failure_is_friendly_without_rigid_uid_check(self):
        previous_mask = os.umask(0o077)
        self.addCleanup(os.umask, previous_mask)
        output = io.StringIO()
        with patch.object(sys, "argv", ["voice_shortcut.py", "gateway", "--project", str(self.project)]), \
                patch.object(voice, "Gateway", side_effect=PermissionError("SECRET /private/path")), \
                contextlib.redirect_stdout(output):
            voice.main()
        response = json.loads(output.getvalue())
        self.assertEqual(response["status"], "failed")
        self.assertIn("UID 1000 with Docker access", response["message"])
        self.assertNotIn("SECRET", output.getvalue())


if __name__ == "__main__":
    unittest.main()
