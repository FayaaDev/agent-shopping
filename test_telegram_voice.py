import asyncio
import contextlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from shopping import open_db
from telegram_bot import TelegramShopping
import telegram_voice as tv
import voice_shortcut as voice


def action(kind, name=None, quantity=None, max_price=None):
    return dict(kind=kind, name=name, quantity=quantity, max_price=max_price, location=None)


class FakeResponse:
    def __init__(self, data, status=200):
        self.status = status
        self.data = data
        self.content = self

    async def iter_chunked(self, size):
        for offset in range(0, len(self.data), size):
            yield self.data[offset:offset + size]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.post = Mock(return_value=response)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class TelegramVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / "shopping.db"
        with contextlib.closing(open_db(self.db)) as connection:
            connection.execute("INSERT INTO items VALUES ('Milk',2,'Preferred','sku','Brand',8.5)")
            connection.execute("INSERT INTO alternatives VALUES ('Milk','Alternative','alt-sku')")
            connection.execute("INSERT INTO items(name,quantity) VALUES ('Bread',1)")
            connection.commit()
        self.adapter = tv.TelegramVoiceShopping(42, self.db)
        self.message = SimpleNamespace(chat_id=42, text="set Milk to 5", voice=None,
                                       reply_text=AsyncMock(), reply_audio=AsyncMock())
        self.update = SimpleNamespace(effective_chat=SimpleNamespace(id=42, type="private"),
                                      effective_user=SimpleNamespace(id=42),
                                      effective_message=self.message, callback_query=None)
        self.file = SimpleNamespace(download_to_memory=AsyncMock(side_effect=self.download))
        self.context = SimpleNamespace(args=[], bot=SimpleNamespace(get_file=AsyncMock(return_value=self.file)))
        self.model = AsyncMock(return_value={"clarification": None, "actions": [action("set", "Milk", 5)]})
        self.child = Mock(returncode=None)
        self.child.poll.return_value = None
        self.child.wait.side_effect = self.finish_child
        for target, name, value in [(voice, "interpret", self.model),
                                    (self.adapter.bridge, "busy", Mock(return_value=False))]:
            patched = patch.object(target, name, value)
            patched.start()
            self.addCleanup(patched.stop)
        patched = patch("voice_bridge.subprocess.Popen", return_value=self.child)
        self.launch = patched.start()
        self.addCleanup(patched.stop)
        patched = patch.dict(os.environ, {"ELEVENLABS_API_KEY": ""})
        patched.start()
        self.addCleanup(patched.stop)
        # Every provider call must be explicitly mocked in an individual test.
        patched = patch.object(tv.aiohttp, "ClientSession", side_effect=AssertionError("Unexpected network"))
        patched.start()
        self.addCleanup(patched.stop)
        self.before = voice.saved_items(self.db)

    def finish_child(self):
        self.child.poll.return_value = 0
        self.child.returncode = 0
        return 0

    async def asyncTearDown(self):
        await self.adapter.shutdown(None)

    @staticmethod
    async def download(*, outfile):
        outfile.write(b"mock ogg")

    def voice_message(self, duration=10, size=8):
        self.message.voice = SimpleNamespace(duration=duration, file_size=size, file_id="file")

    def replies(self):
        return "\n".join(call.args[0] for call in self.message.reply_text.call_args_list)

    def callback_update(self, token, kind="run"):
        query = SimpleNamespace(data=f"voice:{kind}:{token}", message=self.message,
                                answer=AsyncMock(), edit_message_reply_markup=AsyncMock())
        return SimpleNamespace(**{**vars(self.update), "callback_query": query})

    async def preview(self):
        await self.adapter.text(self.update, self.context)
        self.assertIsNotNone(self.adapter.pending)
        return self.adapter.pending[0]

    async def wait_jobs(self):
        await asyncio.wait_for(asyncio.gather(*tuple(self.adapter.tasks)), 3)

    async def test_auth_private_only_all_handlers(self):
        self.voice_message()
        for chat_type, user in [("group", 42), ("supergroup", 42), ("private", 99)]:
            self.update.effective_chat.type = chat_type
            self.update.effective_user.id = user
            self.update.callback_query = self.callback_update("a" * 32).callback_query
            for handler in (self.adapter.voice, self.adapter.text, self.adapter.callback,
                            self.adapter.status, self.adapter.add, self.adapter.clear, self.adapter.shop):
                await handler(self.update, self.context)
            self.update.callback_query.answer.assert_not_awaited()
        self.message.reply_text.assert_not_awaited()
        self.context.bot.get_file.assert_not_awaited()
        self.model.assert_not_awaited()
        self.launch.assert_not_called()

    async def test_missing_key_invalidates_without_download(self):
        await self.preview()
        self.voice_message()
        await self.adapter.voice(self.update, self.context)
        self.assertIsNone(self.adapter.pending)
        self.assertIn("ELEVENLABS_API_KEY", self.replies())
        self.context.bot.get_file.assert_not_awaited()

    async def test_audio_duration_and_known_size(self):
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "key"}):
            for duration, size in [(181, 8), (0, 8), (10, tv.AUDIO_LIMIT + 1), (10, 0)]:
                self.voice_message(duration, size)
                await self.adapter.voice(self.update, self.context)
        self.context.bot.get_file.assert_not_awaited()
        self.model.assert_not_awaited()

    async def test_unknown_size_stream_bounded_before_provider(self):
        self.voice_message(size=None)

        async def oversize(*, outfile):
            outfile.write(b"a" * tv.AUDIO_LIMIT)
            outfile.write(b"b")

        self.file.download_to_memory.side_effect = oversize
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "key"}), \
                patch.object(self.adapter, "transcribe", AsyncMock()) as stt:
            await self.adapter.voice(self.update, self.context)
        stt.assert_not_awaited()
        self.model.assert_not_awaited()
        self.assertIn("Could not process voice", self.replies())
        with tv.BoundedAudio() as buffer:
            buffer.write(b"a" * tv.AUDIO_LIMIT)
            buffer.seek(tv.AUDIO_LIMIT - 1)
            with self.assertRaises(ValueError):
                buffer.write(b"bb")

    async def test_stt_arabic_multipart_auto_language_and_voice_preview(self):
        session = FakeSession(FakeResponse(json.dumps({"text": "حليب خمسة"}).encode()))
        with patch.object(tv.aiohttp, "ClientSession", return_value=session) as factory:
            text = await self.adapter.transcribe(b"ogg", "key")
        self.assertEqual(text, "حليب خمسة")
        args, kwargs = session.post.call_args
        self.assertEqual(args, ("https://api.elevenlabs.io/v1/speech-to-text",))
        self.assertNotIn("params", kwargs)  # No enterprise-only enable_logging option.
        fields = {entry[0]["name"]: entry[2] for entry in kwargs["data"]._fields}
        self.assertEqual(fields["model_id"], "scribe_v2")
        self.assertEqual(fields["tag_audio_events"], "false")
        self.assertEqual(fields["diarize"], "false")
        self.assertNotIn("language_code", fields)
        self.assertEqual(factory.call_args.kwargs["timeout"].total, 90)
        self.voice_message(size=None)
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "key"}), \
                patch.object(self.adapter, "transcribe", AsyncMock(return_value=text)), \
                patch.object(self.adapter, "speak", AsyncMock()) as speak:
            await self.adapter.voice(self.update, self.context)
        self.assertIn(text, self.replies())
        self.assertEqual(self.adapter.draft, text)
        speak.assert_awaited_once()
        self.assertEqual(voice.saved_items(self.db), self.before)
        self.launch.assert_not_called()

    async def test_provider_caps_errors_and_diagnostics_never_echo_secrets(self):
        for response in [FakeResponse(b"x" * 65537), FakeResponse(b"SECRET https://private/?key=key", 500),
                         FakeResponse(b"not json"), FakeResponse(b'{"text":""}')]:
            with patch.object(tv.aiohttp, "ClientSession", return_value=FakeSession(response)):
                with self.assertRaises(ValueError):
                    await self.adapter.transcribe(b"ogg", "key")
        self.voice_message()
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "key"}), \
                patch.object(self.adapter, "transcribe", AsyncMock(side_effect=RuntimeError("SECRET https://private/?key=key"))):
            await self.adapter.voice(self.update, self.context)
        self.assertNotIn("SECRET", self.replies())
        diagnostic = (self.db.parent / "shopping-diagnostics.jsonl").read_text()
        self.assertNotIn("SECRET", diagnostic)
        self.assertNotIn("https://", diagnostic)

    async def test_full_merged_preview_saved_actions_preferences_caps_no_mutation(self):
        token = await self.preview()
        for value in ["Milk", "Bread", "Preferred", "sku", "Brand", "8.5", "Alternative", "alt-sku", "Set Milk target to 5"]:
            self.assertIn(value, self.replies())
        saved = voice.read_json(self.adapter.bridge.root / token / "plan.json")
        self.assertEqual([a["kind"] for a in saved["plan"]["actions"]], ["set", "shop"])
        self.assertIn("Milk × 5", self.replies())
        self.assertIn("Bread ×", self.replies())
        self.assertIn("Unit-price cap: 8.5 SAR", self.replies())
        self.assertNotIn('"max_price":', self.replies())
        self.assertEqual(saved["items"], self.before)
        self.assertEqual(voice.saved_items(self.db), self.before)
        self.launch.assert_not_called()
        for call in self.message.reply_text.call_args_list:
            if "reply_markup" not in call.kwargs:
                self.assertLessEqual(len(call.args[0]), 1800)

    async def test_destructive_actions_explicit_and_full_preview_limit(self):
        self.model.return_value["actions"] = [action("remove", "Bread"), action("clear"), action("set", "Eggs", 3, 6)]
        await self.preview()
        self.assertIn("Remove Bread", self.replies())
        self.assertIn("Clear saved list and preferences", self.replies())
        self.assertIn("6 SAR", self.replies())
        self.assertEqual(voice.saved_items(self.db), self.before)
        directory = self.adapter.bridge.root / ("a" * 32)
        directory.mkdir()
        oversized = {"status": "preview", "token": "a" * 32, "message": "actions", "actions": [], "items": [{"name": "x" * 12000, "quantity": 1}]}
        with patch.object(self.adapter, "bridge_call", AsyncMock(return_value=oversized)):
            await self.adapter.text(self.update, self.context)
        self.assertIsNone(self.adapter.pending)
        self.assertIn("approval unavailable", self.replies())

    async def test_clarification_accumulates_correction_and_bounds_draft(self):
        old = await self.preview()
        self.model.return_value = {"clarification": "How many?", "actions": []}
        self.message.text = "bread"
        await self.adapter.text(self.update, self.context)
        self.assertIsNone(self.adapter.pending)
        self.message.text = "three, not five"
        await self.adapter.text(self.update, self.context)
        self.assertEqual(self.model.call_args.args[0], "set Milk to 5\nbread\nthree, not five")
        await self.adapter.callback(self.callback_update(old), self.context)
        self.launch.assert_not_called()
        self.message.text = "ع" * 2049
        before = self.model.await_count
        await self.adapter.text(self.update, self.context)
        self.assertEqual(self.model.await_count, before)
        self.assertIn("4 KB", self.replies())
        await self.adapter.cancel(self.update, self.context)
        self.assertEqual(self.adapter.draft, "")

    async def test_new_utterance_invalidates_before_expensive_work(self):
        old = await self.preview()
        entered, release = asyncio.Event(), asyncio.Event()

        async def available(message):
            entered.set()
            await release.wait()
            return True

        with patch.object(self.adapter, "available", side_effect=available):
            task = asyncio.create_task(self.adapter.text(self.update, self.context))
            await entered.wait()
            self.assertIsNone(self.adapter.pending)
            await self.adapter.callback(self.callback_update(old), self.context)
            self.launch.assert_not_called()
            release.set()
            await task

    async def test_cancel_stale_token_and_restart_reject(self):
        old = await self.preview()
        new = await self.preview()
        await self.adapter.callback(self.callback_update(old), self.context)
        self.assertEqual(self.adapter.pending[0], new)
        await self.adapter.callback(self.callback_update(new, "cancel"), self.context)
        self.assertIsNone(self.adapter.pending)
        self.assertEqual(self.adapter.draft, "")
        await self.adapter.callback(self.callback_update(new), self.context)
        restarted = tv.TelegramVoiceShopping(42, self.db)
        await restarted.callback(self.callback_update(new), self.context)
        self.launch.assert_not_called()

    async def test_duplicate_confirmation_lock_status_and_keyboard_failure(self):
        token = await self.preview()
        update = self.callback_update(token)
        update.callback_query.edit_message_reply_markup.side_effect = RuntimeError("edit failed")
        await self.adapter.callback(update, self.context)
        self.assertTrue(self.adapter.lock.locked())
        await self.adapter.callback(update, self.context)
        # Let confirmation reserve and launch while the worker remains running.
        for _ in range(100):
            if self.launch.called:
                break
            await asyncio.sleep(.01)
        self.launch.assert_called_once()
        self.assertEqual(voice.read_json(self.adapter.bridge.root / "active.json")["token"], token)
        self.assertTrue((self.adapter.bridge.root / token / "started.json").exists())
        await self.adapter.status(self.update, self.context)
        self.assertIn("still running", self.replies())
        with patch.object(TelegramShopping, "run", AsyncMock()) as run:
            await self.adapter.add(self.update, self.context)
            await self.adapter.clear(self.update, self.context)
            await self.adapter.shop(self.update, self.context)
        run.assert_not_awaited()
        self.assertEqual(voice.saved_items(self.db), self.before)
        self.finish_child()
        voice.write_json(self.adapter.bridge.root / token / "status.json", {"status": "completed", "message": "SECRET"})
        await self.wait_jobs()
        self.assertFalse(self.adapter.lock.locked())
        self.assertNotIn("SECRET", self.replies())
        self.assertNotIn("Cart prepared", self.replies())
        self.assertIn("verified details unavailable", self.replies())

    async def test_result_details_require_a_validated_summary_and_keep_partial_runs_incomplete(self):
        from test_telegram_bot import shopping_result
        token = await self.preview()
        data = shopping_result("exact")
        voice.write_json(self.adapter.bridge.root / token / "result.json", data)
        result = {"token": token, "status": "completed", "message": "SECRET"}
        text = self.adapter.outcome(result)
        self.assertIn("Cart prepared.", text)
        self.assertIn("Nadec Full Fat Milk", text)
        self.assertIn("Slot: Tomorrow", text)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("Cart prepared.", self.adapter.outcome({**result, "status": "incomplete"}))
        data["summary"]["cart"] = []
        voice.write_json(self.adapter.bridge.root / token / "result.json", data)
        self.assertNotIn("Cart prepared.", self.adapter.outcome(result))

    async def test_ttl_saved_database_revalidation_no_launch(self):
        for change in ("expiry", "database"):
            token = await self.preview()
            if change == "expiry":
                path = self.adapter.bridge.root / token / "plan.json"
                saved = voice.read_json(path)
                saved["expires"] = time.time() - 1
                voice.write_json(path, saved)
            else:
                with contextlib.closing(open_db(self.db)) as connection:
                    connection.execute("UPDATE items SET quantity=9")
                    connection.commit()
            await self.adapter.callback(self.callback_update(token), self.context)
            await self.wait_jobs()
            self.assertIsNone(self.adapter.pending)
        self.launch.assert_not_called()
        await self.preview()  # A definite stale-list rejection permits a fresh preview.
        self.launch.assert_not_called()

    async def test_uncertain_launch_restart_blocks_bypass_mutations_and_reports_status(self):
        token = await self.preview()
        self.launch.side_effect = OSError("SECRET")
        await self.adapter.callback(self.callback_update(token), self.context)
        await self.wait_jobs()
        self.launch.assert_called_once()
        self.adapter = tv.TelegramVoiceShopping(42, self.db)
        with patch.object(self.adapter.bridge, "busy", return_value=False), \
                patch.object(TelegramShopping, "run", AsyncMock()) as run:
            for handler in (self.adapter.add, self.adapter.clear, self.adapter.shop):
                await handler(self.update, self.context)
            await self.adapter.status(self.update, self.context)
        run.assert_not_awaited()
        self.assertIn(token, self.replies())
        self.assertIn("uncertain", self.replies())
        self.assertNotIn("SECRET", self.replies())
        self.assertEqual(voice.saved_items(self.db), self.before)

    async def test_bad_active_marker_fail_closed_and_other_owner(self):
        voice.write_json(self.adapter.bridge.root / "active.json", {"token": "../escape"})
        with patch.object(TelegramShopping, "run", AsyncMock()) as run:
            await self.adapter.clear(self.update, self.context)
            await self.adapter.shop(self.update, self.context)
            await self.adapter.status(self.update, self.context)
        run.assert_not_awaited()
        (self.adapter.bridge.root / "active.json").unlink()
        with patch.object(self.adapter.bridge, "busy", return_value=True):
            await self.adapter.text(self.update, self.context)
        self.model.assert_not_awaited()

    async def test_inherited_mutations_execute_under_shared_lock_and_recheck(self):
        self.context.args = ["Eggs", "2"]

        async def run(*args, **kwargs):
            self.assertTrue(self.adapter.lock.locked())
            return 0, b""

        with patch.object(TelegramShopping, "run", side_effect=run) as inherited:
            await self.adapter.add(self.update, self.context)
            self.context.args = []
            await self.adapter.clear(self.update, self.context)
        self.assertEqual(inherited.await_count, 2)
        with patch.object(self.adapter.bridge, "busy", return_value=True), \
                patch.object(TelegramShopping, "run", AsyncMock()) as inherited:
            with self.assertRaises(ValueError):
                await self.adapter.run("shop")
        inherited.assert_not_awaited()

    async def test_tts_optional_bounded_mp3_failure_does_not_block_preview(self):
        session = FakeSession(FakeResponse(b"mp3"))
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "key"}), \
                patch.object(tv.aiohttp, "ClientSession", return_value=session):
            await self.adapter.speak(self.message, "x" * 501)
        self.message.reply_audio.assert_awaited_once()
        args, kwargs = session.post.call_args
        self.assertTrue(args[0].endswith(tv.GEORGE))
        self.assertEqual(kwargs["json"]["model_id"], "eleven_multilingual_v2")
        self.assertEqual(len(kwargs["json"]["text"]), 500)
        self.assertNotIn("enable_logging", kwargs["params"])
        for response in [FakeResponse(b"x" * (1024 * 1024 + 1)), FakeResponse(b"SECRET", 500)]:
            with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "key"}), \
                    patch.object(tv.aiohttp, "ClientSession", return_value=FakeSession(response)):
                await self.preview()
                self.assertIsNotNone(self.adapter.pending)
        self.assertEqual(self.message.reply_audio.await_count, 1)
        self.assertNotIn("SECRET", self.replies())

    async def test_shutdown_awaits_bridge_child_cleanup(self):
        token = await self.preview()
        await self.adapter.callback(self.callback_update(token), self.context)
        for _ in range(100):
            if self.launch.called:
                break
            await asyncio.sleep(.01)
        self.launch.assert_called_once()
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def wait():
            loop.call_soon_threadsafe(entered.set)
            release.wait(timeout=3)
            return self.finish_child()

        self.child.wait.side_effect = wait
        shutdown = asyncio.create_task(self.adapter.shutdown(None))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.assertFalse(shutdown.done())
            self.assertTrue(self.adapter.lock.locked())
        finally:
            release.set()
            await shutdown
        self.child.send_signal.assert_called_once()
        self.child.wait.assert_called_once()
        self.assertFalse(self.adapter.lock.locked())
        self.assertEqual(self.adapter.tasks, set())
        await self.adapter.callback(self.callback_update(token), self.context)
        self.launch.assert_called_once()

    async def test_poll_timeout_cleans_worker_before_releasing_lock_no_retry(self):
        token = await self.preview()

        def wait():
            self.assertTrue(self.adapter.lock.locked())
            return self.finish_child()

        self.child.wait.side_effect = wait
        clock = Mock()
        clock.monotonic.side_effect = [0, 2101]
        with patch.object(tv, "time", clock):
            await self.adapter.callback(self.callback_update(token), self.context)
            await self.wait_jobs()
        self.child.send_signal.assert_called_once()
        self.child.wait.assert_called_once()
        self.assertFalse(self.adapter.lock.locked())
        self.assertTrue(self.adapter.uncertain)
        await self.adapter.callback(self.callback_update(token), self.context)
        self.launch.assert_called_once()
        self.assertIn("no automatic retry", self.replies())


if __name__ == "__main__":
    unittest.main()
