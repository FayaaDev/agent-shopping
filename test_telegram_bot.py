import asyncio
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import telegram_bot as bot


def update(user=42, chat="private"):
    return SimpleNamespace(effective_user=SimpleNamespace(id=user),
                           effective_chat=SimpleNamespace(type=chat),
                           effective_message=SimpleNamespace(reply_text=AsyncMock()))


def product():
    return {"name": "Nadec Full Fat Milk", "sku": "123", "product_type": "full fat milk",
            "brand": "Nadec", "package_size": "1 L", "package_quantity": 1,
            "package_unit": "L", "unit_price": 6.5, "mainstream_brand": True,
            "matches_request": True, "match_reason": "Milk in the requested 1 L package.",
            "match_evidence": ["Milk", "1 L"]}


def shopping_result(mode="automatic_substitution"):
    outcome = {"item_name": "Milk 1 L", "selection_mode": mode,
               "product": product() if mode is not None else None}
    if mode == "automatic_substitution":
        outcome.update(requested_type="full fat milk", required_package_quantity=1,
                       package_unit="L", candidates=[product()], comparison=product(),
                       preferred_brand_available=False, exact_unavailable=True, approved_unavailable=True)
    return {"status": "attempt_saved", "success": True, "attempt": 3,
            "summary": {"cart": [{"name": "Nadec Full Fat Milk", "quantity": 2, "unit_price": 6.5}],
                        "slot": "Tomorrow", "stage": "slot_selected", "unresolved": [],
                        "missing_or_over_cap": [], "item_outcomes": [outcome]}}


class ConfigurationTests(unittest.TestCase):
    def test_voice_text_callbacks_registered_without_replaying_old_updates(self):
        from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(bot, "configuration", return_value=("123:abc", 42, Path(directory) / "shopping.db")), \
                patch.object(Application, "run_polling", autospec=True) as polling, \
                patch("sys.argv", ["telegram_bot.py"]):
            bot.main()
        application = polling.call_args.args[0]
        handlers = application.handlers[0]
        commands = set().union(*(handler.commands for handler in handlers if isinstance(handler, CommandHandler)))
        self.assertIn("status", commands)
        self.assertIn("cancel", commands)
        self.assertEqual(sum(isinstance(handler, MessageHandler) for handler in handlers), 2)
        self.assertEqual(sum(isinstance(handler, CallbackQueryHandler) for handler in handlers), 1)
        self.assertTrue(polling.call_args.kwargs["drop_pending_updates"])
        self.assertEqual(polling.call_args.kwargs["bootstrap_retries"], 0)
        self.assertIn("callback_query", polling.call_args.kwargs["allowed_updates"])

    def test_required_environment_and_absolute_database(self):
        with patch("telegram_bot.load_dotenv"), patch.dict(os.environ, {}, clear=True):
            for token, user in (("", "42"), ("bad", "42"), ("123:abc", ""),
                                ("123:abc", "0"), ("123:abc", "-1"), ("123:abc", "4.2")):
                os.environ.update(TELEGRAM_BOT_TOKEN=token, TELEGRAM_ALLOWED_USER_ID=user)
                with self.assertRaises(ValueError):
                    bot.configuration()
            os.environ.update(TELEGRAM_BOT_TOKEN="123:abc", TELEGRAM_ALLOWED_USER_ID="42", SHOPPING_DB="custom.db")
            self.assertEqual(bot.configuration(), ("123:abc", 42, bot.PROJECT / "custom.db"))

    def test_structured_outcomes(self):
        self.assertIn("readiness failed", bot.shop_text({"status": "readiness_failed", "success": False, "reasons": ["secret"]}))
        self.assertNotIn("secret", bot.shop_text({"status": "readiness_failed", "reasons": ["secret"]}))
        self.assertIn("incomplete", bot.shop_text({"status": "no_summary"}))
        data = {"status": "attempt_saved", "success": True, "attempt": 3,
                "summary": {"cart": [{"name": "Milk", "quantity": 2, "unit_price": 5}],
                            "slot": "Tomorrow", "stage": "slot_selected", "unresolved": [], "missing_or_over_cap": []}}
        self.assertIn("Cart prepared", bot.shop_text(data))
        self.assertIn("Manual approval required", bot.shop_text(data))
        data["summary"]["missing_or_over_cap"] = ["Eggs"]
        self.assertIn("incomplete", bot.shop_text(data))
        data["success"] = False
        self.assertIn("Eggs", bot.shop_text(data))


class ShoppingResultTests(unittest.TestCase):
    def test_generic_yogurt_optional_criteria_and_pack_quantity(self):
        from test_shopping import generic_yogurt

        item, outcome = generic_yogurt()
        data = shopping_result()
        data["summary"]["item_outcomes"] = [outcome]
        data["summary"]["cart"] = [{"name": outcome["product"]["name"], "quantity": item["quantity"], "unit_price": 9.95}]
        text = bot.shop_text(data)
        self.assertIn("Cart prepared.", text)
        self.assertIn("× 10 purchasable units", text)
        self.assertIn("observed package 3 x 160 g", text)
        self.assertIn("Greek yogurt → Nada Greek Yogurt Assorted Pack3X160G", text)

    def test_automatic_substitution_shows_requested_chosen_observed_package_and_price(self):
        data = shopping_result()
        data["summary"]["item_outcomes"][0]["model_text"] = "private model explanation"
        text = bot.shop_text(data)
        self.assertIn("Cart prepared", text)
        self.assertIn("Automatic substitution: Milk 1 L → Nadec Full Fat Milk", text)
        self.assertIn("observed package 1 L (1 L), price per purchasable unit 6.5", text)
        self.assertIn("Manual approval required for checkout.", text)
        self.assertNotIn("private", text)

    def test_exact_and_approved_results_keep_checkout_approval(self):
        for mode in ("exact", "approved_alternative"):
            with self.subTest(mode=mode):
                data = shopping_result(mode)
                del data["summary"]["item_outcomes"][0]["product"]["sku"]
                text = bot.shop_text(data)
                self.assertIn("Cart prepared", text)
                self.assertNotIn("Automatic substitution", text)
                self.assertIn("Manual approval required for checkout.", text)

    def test_unresolved_outcome_overrides_success_and_uses_only_canned_reasons(self):
        expected = {"ambiguous_type": "Requested product type is unclear",
                    "insufficient_evidence": "Not enough verified product information",
                    "insufficient_package": "No sufficiently sized package verified",
                    "price_cap": "No suitable product within the price limit",
                    "no_safe_candidate": "No safe substitute verified",
                    "recovery_exhausted": "Item recovery limit reached",
                    "item_unavailable": "Requested item unavailable",
                    "quantity_unverified": "Cart quantity could not be verified",
                    "private model reason token=secret": "Manual review required",
                    None: "Manual review required"}
        for reason, canned in expected.items():
            with self.subTest(reason=reason):
                data = shopping_result(None)
                data["summary"]["item_outcomes"][0]["unresolved_reason"] = reason
                text = bot.shop_text(data)
                self.assertIn("Shopping incomplete", text)
                self.assertNotIn("Cart prepared", text)
                self.assertIn(f"Unresolved: Milk 1 L — {canned}", text)
                self.assertIn("Manual approval required for checkout.", text)
                self.assertNotIn("private model", text)
                self.assertNotIn("token", text)
                if reason is not None:
                    self.assertNotIn(reason, text)

    def test_summary_unresolved_names_merge_with_outcomes_without_duplicates(self):
        data = shopping_result(None)
        data["summary"]["unresolved"] = ["Milk 1 L", "Eggs"]
        data["summary"]["item_outcomes"][0]["unresolved_reason"] = "item_unavailable"
        text = bot.shop_text(data)
        self.assertEqual(text.count("Unresolved: Milk 1 L"), 1)
        self.assertIn("Unresolved: Eggs — Manual review required", text)
        self.assertNotIn("Cart prepared", text)

    def test_selected_outcome_with_unresolved_reason_is_incomplete(self):
        data = shopping_result("exact")
        data["summary"]["item_outcomes"][0]["unresolved_reason"] = "quantity_unverified"
        text = bot.shop_text(data)
        self.assertNotIn("Cart prepared", text)
        self.assertIn("Cart quantity could not be verified", text)

    def test_assessed_missing_evidence_and_minimal_exact_product(self):
        data = shopping_result("exact")
        data["summary"]["item_outcomes"][0]["product"] = {"name": "Nadec Full Fat Milk", "unit_price": 6.5}
        self.assertIn("Cart prepared", bot.shop_text(data))
        data = shopping_result()
        outcome = data["summary"]["item_outcomes"][0]
        outcome["unresolved_reason"] = "insufficient_evidence"
        outcome["product"] = None
        del outcome["comparison"]
        text = bot.shop_text(data)
        self.assertIn("Unresolved: Milk 1 L — Not enough verified product information", text)
        self.assertNotIn("Automatic substitution:", text)
        self.assertNotIn("Cart prepared", text)

    def test_non_attempt_results_also_require_checkout_approval(self):
        for status in ("readiness_failed", "no_summary"):
            text = bot.shop_text({"status": status, "reasons": ["private model reason"]})
            self.assertIn("Manual approval required for checkout.", text)
            self.assertNotIn("private", text)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.adapter = bot.TelegramShopping(42, Path(directory.name) / "shopping.db")

    async def run_shop_result(self, code, data=None):
        async def run(*args):
            if data is not None:
                Path(args[args.index("--result-file") + 1]).write_text(json.dumps(data))
            return code, b"discarded private subprocess output"
        self.adapter.run = AsyncMock(side_effect=run)
        event = update()
        await self.adapter.shop(event, SimpleNamespace(args=[]))
        await asyncio.gather(*tuple(self.adapter.tasks))
        self.adapter.run.assert_awaited_once()
        self.assertFalse(self.adapter.lock.locked())
        return event.effective_message.reply_text.call_args.args[0]

    async def test_structured_error_exit_two_private_diagnostic_and_reference(self):
        data = {"status": "error", "success": False, "phase": "cart_agent", "error_type": "RuntimeError",
                "attempt": 7, "summary": {"cart": ["private groceries"]},
                "diagnostic": {"message": "token=private-credential " + "x" * 5000,
                               "frames": [{"file": "/private/path/agent.py", "line": 42, "function": "run"}] * 30}}
        text = await self.run_shop_result(2, data)
        self.assertIn("Stage: cart_agent; type: RuntimeError; exit code: 2", text)
        self.assertIn("cart may have changed", text)
        self.assertIn("no automatic retry", text)
        self.assertNotIn("token", text)
        path = self.adapter.db.parent / "shopping-diagnostics.jsonl"
        payload = json.loads(path.read_text())
        self.assertIn(payload["run_id"], text)
        self.assertEqual(payload["exit_code"], 2)
        self.assertEqual(payload["status"], "error")
        self.assertTrue(payload["result_exists"])
        self.assertEqual(payload["attempt"], 7)
        self.assertNotIn("summary", payload)
        self.assertNotIn("private-credential", path.read_text())
        self.assertNotIn("/private/path", path.read_text())
        self.assertLessEqual(len(payload["diagnostic"]["message"]), 2000)
        self.assertEqual(len(payload["diagnostic"]["frames"]), 20)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        for _ in range(130):
            bot.record_diagnostic(self.adapter.db, "rotation", 2, True, data)
        logs = list(path.parent.glob("shopping-diagnostics.jsonl*"))
        self.assertEqual(len(logs), 3)
        for log in logs:
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)
            self.assertLessEqual(log.stat().st_size, 128 * 1024)
            for line in log.read_text().splitlines():
                self.assertNotIn("private-credential", line)
                json.loads(line)

    async def test_no_summary_agent_failure_logs_one_private_record(self):
        details = json.dumps({"error": "token=private-credential", "detail": "private tool details"})
        data = {"status": "no_summary", "success": False, "phase": "cart_agent",
                "error_type": "RuntimeError", "error_code": "agent_failure",
                "diagnostic": {"message": details, "frames": []}}
        with patch("telegram_bot.record_diagnostic", wraps=bot.record_diagnostic) as record:
            text = await self.run_shop_result(1, data)
        record.assert_called_once()
        path = self.adapter.db.parent / "shopping-diagnostics.jsonl"
        self.assertEqual(len(path.read_text().splitlines()), 1)
        payload = json.loads(path.read_text())
        self.assertEqual(payload["status"], "no_summary")
        self.assertEqual(payload["phase"], "cart_agent")
        self.assertEqual(payload["error_type"], "RuntimeError")
        self.assertEqual(payload["error_code"], "agent_failure")
        self.assertEqual(payload["diagnostic"]["frames"], [])
        self.assertIn("private tool details", payload["diagnostic"]["message"])
        self.assertNotIn("private-credential", path.read_text())
        self.assertIn("Shopping incomplete", text)
        self.assertIn("Agent/tool failure", text)
        self.assertIn(payload["run_id"], text)
        self.assertIn("Review it manually", text)
        self.assertIn("no automatic retry", text)
        self.assertNotIn("Shopping failed", text)
        self.assertNotIn("private", text)
        self.assertNotIn("token", text)

    async def test_saved_partial_attempt_logs_redacted_diagnostic_and_reference_without_retry(self):
        data = shopping_result(None)
        data["success"] = False
        data["summary"]["item_outcomes"][0]["unresolved_reason"] = "insufficient_evidence"
        data.update(phase="cart_agent", error_type="RuntimeError", error_code="agent_failure",
                    diagnostic={"message": "token=private-credential https://private.example/cart "
                                           "person@example.com private tool details " + "x" * 5000,
                                "frames": [{"file": "/private/path/agent.py", "line": 42,
                                            "function": "run"}] * 25})
        with patch("telegram_bot.record_diagnostic", wraps=bot.record_diagnostic) as record:
            text = await self.run_shop_result(1, data)
        record.assert_called_once()
        path = self.adapter.db.parent / "shopping-diagnostics.jsonl"
        self.assertEqual(len(path.read_text().splitlines()), 1)
        payload = json.loads(path.read_text())
        self.assertEqual(payload["status"], "attempt_saved")
        self.assertEqual(payload["phase"], "cart_agent")
        self.assertEqual(payload["error_type"], "RuntimeError")
        self.assertEqual(payload["error_code"], "agent_failure")
        self.assertEqual(payload["attempt"], 3)
        self.assertEqual(payload["exit_code"], 1)
        self.assertTrue(payload["result_exists"])
        self.assertNotIn("summary", payload)
        self.assertLessEqual(len(payload["diagnostic"]["message"]), 2000)
        self.assertEqual(len(payload["diagnostic"]["frames"]), 20)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertIn("[URL REDACTED]", payload["diagnostic"]["message"])
        self.assertIn("[EMAIL REDACTED]", payload["diagnostic"]["message"])
        self.assertIn("private tool details", payload["diagnostic"]["message"])
        for private in ("private-credential", "private.example", "person@example.com", "/private/path"):
            self.assertNotIn(private, path.read_text())
            self.assertNotIn(private, text)
        self.assertIn(f"Diagnostic reference: {payload['run_id']}.", text)
        self.assertIn("Shopping incomplete", text)
        self.assertIn("Attempt: 3", text)
        self.assertIn("Not enough verified product information", text)
        self.assertIn("Manual approval required for checkout.", text)
        self.assertNotIn("Cart prepared", text)
        self.assertNotIn("Shopping failed", text)
        self.assertNotIn("private tool details", text)
        self.assertNotIn("RuntimeError", text)

    async def test_saved_attempt_diagnostic_write_failure_keeps_summary_and_reference(self):
        data = shopping_result(None)
        data["success"] = False
        data["diagnostic"] = {"message": "private model text", "frames": []}
        with patch("telegram_bot.record_diagnostic", side_effect=OSError("secret credential")) as record:
            text = await self.run_shop_result(1, data)
        record.assert_called_once()
        self.assertIn("Shopping incomplete", text)
        self.assertIn("Attempt: 3", text)
        self.assertIn("Diagnostic reference:", text)
        self.assertIn("Manual approval required for checkout.", text)
        self.assertNotIn("Shopping failed", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("private", text)

    async def test_no_summary_unsupported_error_code_is_bounded_and_private(self):
        for error_code in ("private-code-" + "x" * 10000, ["private-code"], {"private-code": True}):
            with self.subTest(error_code=type(error_code).__name__):
                text = await self.run_shop_result(1, {"status": "no_summary", "error_code": error_code})
                path = self.adapter.db.parent / "shopping-diagnostics.jsonl"
                payload = json.loads(path.read_text().splitlines()[-1])
                self.assertEqual(payload["error_code"], "unknown")
                self.assertIn("Unknown reason", text)
                self.assertIn(payload["run_id"], text)
                self.assertLess(len(text), 500)
                self.assertNotIn("private-code", text)
                self.assertNotIn("private-code", path.read_text())

    async def test_no_summary_older_result_logs_unknown_reason(self):
        text = await self.run_shop_result(0, {"status": "no_summary", "success": False})
        payload = json.loads((self.adapter.db.parent / "shopping-diagnostics.jsonl").read_text())
        self.assertEqual(payload["error_code"], "unknown")
        self.assertEqual(payload["phase"], "unknown")
        self.assertEqual(payload["diagnostic"], {"message": "", "frames": []})
        self.assertIn("Shopping incomplete", text)
        self.assertIn("Unknown reason", text)
        self.assertNotIn("No final output", text)
        self.assertIn(payload["run_id"], text)
        self.assertIn("no automatic retry", text)

    async def test_no_summary_known_reasons(self):
        for error_code, reason in bot.INCOMPLETE_REASONS.items():
            with self.subTest(error_code=error_code):
                text = await self.run_shop_result(1, {"status": "no_summary", "error_code": error_code})
                self.assertIn(reason, text)
                payload = json.loads((self.adapter.db.parent / "shopping-diagnostics.jsonl").read_text().splitlines()[-1])
                self.assertEqual(payload["error_code"], error_code)

    async def test_missing_result_preserves_exit_code_and_reference(self):
        text = await self.run_shop_result(2)
        payload = json.loads((self.adapter.db.parent / "shopping-diagnostics.jsonl").read_text())
        self.assertEqual(payload["phase"], "result_file")
        self.assertEqual(payload["error_type"], "FileNotFoundError")
        self.assertEqual(payload["exit_code"], 2)
        self.assertFalse(payload["result_exists"])
        self.assertIn("exit code: 2", text)
        self.assertIn(payload["run_id"], text)

    async def test_formatting_failure_keeps_attempt_without_summary(self):
        text = await self.run_shop_result(0, {"status": "attempt_saved", "success": True, "attempt": 3,
                                              "summary": {"cart": None, "private": "personal data"}})
        payload = json.loads((self.adapter.db.parent / "shopping-diagnostics.jsonl").read_text())
        self.assertEqual(payload["phase"], "result_format")
        self.assertEqual(payload["status"], "attempt_saved")
        self.assertEqual(payload["attempt"], 3)
        self.assertNotIn("personal data", json.dumps(payload))
        self.assertNotIn("Cart prepared", text)

    async def test_malformed_outcomes_use_sanitized_result_format_path_without_retry(self):
        changes = [
            ("summary", "item_outcomes", {"private": "model text"}),
            ("summary", "unresolved", [{"reason": "private model text"}]),
            ("outcome", "item_name", {"private": "model text"}),
            ("outcome", "selection_mode", "private model text"),
            ("outcome", "unresolved_reason", {"private": "model text"}),
            ("outcome", "product", None),
            ("outcome", "requested_type", None),
            ("outcome", "required_package_quantity", True),
            ("outcome", "package_unit", ["private model text"]),
            ("outcome", "preferred_brand_available", "false"),
            ("outcome", "exact_unavailable", 1),
            ("outcome", "approved_unavailable", "false"),
            ("outcome", "candidates", "private model text"),
            ("outcome", "candidates", [{"name": "private model text"}]),
            ("outcome", "comparison", {"name": "private model text"}),
            ("product", "package_quantity", 0),
            ("product", "package_quantity", -1),
            ("product", "package_size", {"private": "model text"}),
            ("product", "unit_price", float("nan")),
            ("product", "unit_price", float("inf")),
            ("product", "unit_price", True),
            ("product", "mainstream_brand", "true"),
        ]
        for target, key, value in changes:
            with self.subTest(target=target, key=key, value=value):
                data = shopping_result()
                outcome = data["summary"]["item_outcomes"][0]
                {"summary": data["summary"], "outcome": outcome, "product": outcome["product"]}[target][key] = value
                with patch("telegram_bot.record_diagnostic", wraps=bot.record_diagnostic) as record:
                    text = await self.run_shop_result(0, data)
                record.assert_called_once()
                payload = json.loads((self.adapter.db.parent / "shopping-diagnostics.jsonl").read_text().splitlines()[-1])
                self.assertEqual(payload["phase"], "result_format")
                self.assertEqual(payload["attempt"], 3)
                self.assertIn(payload["run_id"], text)
                self.assertIn("Shopping failed", text)
                self.assertIn("Manual approval required for checkout.", text)
                self.assertIn("no automatic retry", text)
                self.assertNotIn("Cart prepared", text)
                self.assertNotIn("private", text)
                self.assertNotIn("model text", json.dumps(payload))

    async def test_diagnostic_write_failure_still_replies_and_releases_lock(self):
        with patch("telegram_bot.record_diagnostic", side_effect=OSError("secret credential")):
            text = await self.run_shop_result(2, {"status": "error", "phase": "config", "error_type": "ValueError",
                                                  "diagnostic": {"message": "secret credential"}})
        self.assertIn("Diagnostic reference:", text)
        self.assertIn("Manual approval required", text)
        self.assertNotIn("secret", text)

    async def test_every_handler_rejects_other_users_and_groups(self):
        self.adapter.run = AsyncMock()
        for handler in (self.adapter.help, self.adapter.add, self.adapter.clear, self.adapter.list, self.adapter.shop):
            for event in (update(user=43), update(chat="group"), update(chat="channel")):
                await handler(event, SimpleNamespace(args=["Milk", "2"]))
                event.effective_message.reply_text.assert_not_awaited()
        self.adapter.run.assert_not_awaited()
        self.assertFalse(self.adapter.tasks)

    async def test_add_parsing_and_canned_reply(self):
        event = update()
        self.adapter.run = AsyncMock(return_value=(0, b"secret logs"))
        for args in ([], ["Milk"], ["Milk", "0"], ["Milk", "-2"], ["Milk", "2.5"]):
            await self.adapter.add(event, SimpleNamespace(args=args))
        self.adapter.run.assert_not_awaited()
        await self.adapter.add(event, SimpleNamespace(args=["-Organic", "whole", "milk", "3"]))
        self.adapter.run.assert_awaited_once_with("add", "--", "-Organic whole milk", "3")
        self.assertEqual(event.effective_message.reply_text.call_args.args[0], "Shopping list updated.")

    async def test_clear_validation_locking_and_sanitized_reply(self):
        event = update()
        context = SimpleNamespace(args=[])

        async def run(*args):
            self.assertTrue(self.adapter.lock.locked())
            return 0, b"secret logs"

        self.adapter.run = AsyncMock(side_effect=run)
        await self.adapter.clear(event, SimpleNamespace(args=["Milk"]))
        event.effective_message.reply_text.assert_awaited_with("Usage: /clear", parse_mode=None)
        self.adapter.run.assert_not_awaited()
        await self.adapter.clear(event, context)
        self.adapter.run.assert_awaited_once_with("clear")
        event.effective_message.reply_text.assert_awaited_with(
            "Saved shopping list, product preferences, and approved alternatives cleared. Live cart unchanged.",
            parse_mode=None)
        self.assertFalse(self.adapter.lock.locked())

        for result in ((1, b"secret logs"), OSError("secret token"), ValueError("secret traceback")):
            self.adapter.run.reset_mock()
            self.adapter.run.side_effect = result if isinstance(result, Exception) else None
            self.adapter.run.return_value = result
            await self.adapter.clear(event, context)
            self.adapter.run.assert_awaited_once_with("clear")
            event.effective_message.reply_text.assert_awaited_with(
                "Could not clear the shopping list. Check locally.", parse_mode=None)
            self.assertFalse(self.adapter.lock.locked())

        self.adapter.run.reset_mock()
        async with self.adapter.lock:
            await self.adapter.clear(event, context)
            self.assertTrue(self.adapter.lock.locked())
            self.adapter.run.assert_not_awaited()
            event.effective_message.reply_text.assert_awaited_with(
                "Shopping/list update in progress. Try /clear after it finishes.", parse_mode=None)
        self.adapter.stopping = True
        await self.adapter.clear(event, context)
        self.adapter.run.assert_not_awaited()
        event.effective_message.reply_text.assert_awaited_with(
            "Shopping/list update in progress. Try /clear after it finishes.", parse_mode=None)
        self.assertTrue(all("secret" not in call.args[0]
                            for call in event.effective_message.reply_text.call_args_list))

    async def test_list_json_plain_bounded_replies_and_failure(self):
        event = update()
        self.adapter.run = AsyncMock(return_value=(0, json.dumps([{"name": "*Milk*", "quantity": 2}]).encode()))
        await self.adapter.list(event, SimpleNamespace(args=[]))
        event.effective_message.reply_text.assert_awaited_with("*Milk* × 2", parse_mode=None)
        self.adapter.run.return_value = (1, b"secret traceback")
        await self.adapter.list(event, SimpleNamespace(args=[]))
        self.assertNotIn("secret", event.effective_message.reply_text.call_args.args[0])
        event.effective_message.reply_text.reset_mock()
        await bot.reply(event.effective_message, "😀" * 15000)
        self.assertLessEqual(event.effective_message.reply_text.await_count, 7)
        for call in event.effective_message.reply_text.call_args_list:
            self.assertLessEqual(len(call.args[0].encode("utf-16-le")) // 2, 4096)
            self.assertIsNone(call.kwargs["parse_mode"])

    async def test_background_shop_lock_result_and_location(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        calls = []

        async def run(*args, **kwargs):
            calls.append(args)
            if args[0] == "shop":
                entered.set()
                await finish.wait()
                Path(args[args.index("--result-file") + 1]).write_text('{"status":"no_summary","success":false}')
            return 0, b"[]"

        self.adapter.run = run
        event = update()
        await self.adapter.shop(event, SimpleNamespace(args=["My", "Home"]))
        await entered.wait()
        await self.adapter.shop(event, SimpleNamespace(args=[]))
        await self.adapter.add(event, SimpleNamespace(args=["Milk", "2"]))
        await self.adapter.list(event, SimpleNamespace(args=[]))
        await self.adapter.help(event, SimpleNamespace(args=[]))
        self.assertEqual([args[0] for args in calls], ["shop", "list"])
        self.assertEqual(calls[0][-2:], ("--location", "My Home"))
        tasks = tuple(self.adapter.tasks)
        finish.set()
        await asyncio.gather(*tasks)
        self.assertFalse(self.adapter.lock.locked())
        self.assertFalse(Path(calls[0][2]).exists())
        self.assertIn("incomplete", event.effective_message.reply_text.call_args.args[0])

    async def test_shutdown_cancels_background_shop(self):
        entered = asyncio.Event()

        async def run(*args):
            entered.set()
            await asyncio.Event().wait()

        self.adapter.run = run
        await self.adapter.shop(update(), SimpleNamespace(args=[]))
        await entered.wait()
        await self.adapter.shutdown(None)
        self.assertFalse(self.adapter.tasks)
        self.assertFalse(self.adapter.lock.locked())

    async def test_shutdown_before_background_task_starts(self):
        self.adapter.run = AsyncMock()
        await self.adapter.shop(update(), SimpleNamespace(args=[]))
        await self.adapter.shutdown(None)
        self.adapter.run.assert_not_awaited()
        self.assertFalse(self.adapter.lock.locked())

    async def test_missing_result_is_sanitized_without_retry(self):
        event = update()
        self.adapter.run = AsyncMock(side_effect=OSError("secret token"))
        await self.adapter.shop(event, SimpleNamespace(args=[]))
        await asyncio.gather(*tuple(self.adapter.tasks))
        self.adapter.run.assert_awaited_once()
        text = event.effective_message.reply_text.call_args.args[0]
        self.assertNotIn("secret", text)
        self.assertIn("Manual approval required", text)
        self.assertFalse(self.adapter.lock.locked())

    async def test_saved_result_cannot_hide_failed_or_interrupted_exit(self):
        data = {"status": "attempt_saved", "success": True, "attempt": 1,
                "summary": {"cart": [], "slot": "Tomorrow", "stage": "slot_selected",
                            "unresolved": [], "missing_or_over_cap": []}}
        for code in (1, 130, 143, -15):
            async def run(*args):
                Path(args[args.index("--result-file") + 1]).write_text(json.dumps(data))
                return code, b""

            self.adapter.run = AsyncMock(side_effect=run)
            event = update()
            await self.adapter.shop(event, SimpleNamespace(args=[]))
            await asyncio.gather(*tuple(self.adapter.tasks))
            self.adapter.run.assert_awaited_once()
            text = event.effective_message.reply_text.call_args.args[0]
            self.assertNotIn("Cart prepared", text)
            self.assertIn("Manual approval required", text)
            if code != 1:
                self.assertIn("Shopping interrupted", text)
                self.assertIn(f"exit code: {code}", text)

    async def test_subprocess_arguments_output_and_cancellation_escalation(self):
        process = SimpleNamespace(returncode=None, stdout=SimpleNamespace(read=AsyncMock(side_effect=[b"[]", b""])),
                                  wait=AsyncMock(return_value=0), terminate=unittest.mock.Mock(), kill=unittest.mock.Mock())
        process.returncode = 0
        with patch("telegram_bot.asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as spawn:
            self.assertEqual(await self.adapter.run("list", capture=True), (0, b"[]"))
            self.assertEqual(spawn.call_args.args, (bot.sys.executable, str(bot.PROJECT / "shopping.py"),
                                                  "--db", str(self.adapter.db), "list"))
            self.assertEqual(spawn.call_args.kwargs["cwd"], str(bot.PROJECT))
            self.assertEqual(spawn.call_args.kwargs["stdin"], asyncio.subprocess.DEVNULL)
            self.assertEqual(spawn.call_args.kwargs["stderr"], asyncio.subprocess.DEVNULL)
            process.returncode = None
            process.wait = AsyncMock(side_effect=[asyncio.CancelledError(), asyncio.TimeoutError(), 0])
            with self.assertRaises(asyncio.CancelledError):
                await self.adapter.run("shop")
            self.assertEqual(spawn.call_args.kwargs["stdout"], asyncio.subprocess.DEVNULL)
            process.terminate.assert_called_once()
            process.kill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
