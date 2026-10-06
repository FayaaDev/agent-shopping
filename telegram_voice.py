"""In-process Telegram voice/text previews; handlers are wired by telegram_bot.main."""

import asyncio
import contextlib
import io
import json
import os
import re
import time
import uuid

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile

from telegram_bot import TelegramShopping, reply, record_diagnostic, shop_text
from voice_bridge import Bridge, TERMINAL
import voice_shortcut as voice

AUDIO_LIMIT = 8 * 1024 * 1024
GEORGE = "JBFqnCBsd6RMkjVDRZzb"


class BoundedAudio(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > AUDIO_LIMIT:
            raise ValueError("Audio too large")
        return super().write(data)


async def response_bytes(response, limit):
    if response.status != 200:
        raise ValueError("Provider unavailable")
    data = bytearray()
    async for chunk in response.content.iter_chunked(8192):
        if len(data) + len(chunk) > limit:
            raise ValueError("Provider response too large")
        data.extend(chunk)
    return bytes(data)


class TelegramVoiceShopping(TelegramShopping):
    def __init__(self, user_id, db):
        super().__init__(user_id, db)
        self.bridge = Bridge(self.db)
        self.pending = None
        self.draft = ""
        self.generation = 0
        self.current = None
        self.uncertain = False

    def invalidate(self):
        self.pending = None
        self.generation += 1

    def diagnostic(self, reason):
        reference = uuid.uuid4().hex[:12]
        # Never retain provider exceptions, HTTP bodies, transcripts, URLs or keys.
        with contextlib.suppress(Exception):
            record_diagnostic(self.db, reference, None, False,
                              {"phase": "subprocess", "error_type": "Error",
                               "diagnostic": {"message": reason}})
        return reference

    async def bridge_call(self, mode, value=None):
        async with self.bridge.serial:
            task = asyncio.create_task(asyncio.to_thread(self.bridge.handle, mode, value))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # A cancelled HTTP/Telegram wait cannot abandon a reservation thread.
                with contextlib.suppress(Exception):
                    await task
                raise

    def active_status(self):
        path = self.bridge.root / "active.json"
        if not path.exists():
            return None
        token = voice.read_json(path).get("token")
        if not isinstance(token, str) or not re.fullmatch(voice.TOKEN, token):
            raise voice.Rejected("Previous launch uncertain")
        return self.bridge.status(token)

    def blocked(self):
        active = self.active_status()
        return (self.uncertain or (active is not None and active["status"] not in
                {"completed", "incomplete", "failed"}) or self.bridge.busy())

    async def available(self, message):
        if self.lock.locked() or self.stopping:
            await reply(message, "Shopping/list update in progress. Use /status.")
            return False
        try:
            async with self.bridge.serial:
                blocked = await asyncio.to_thread(self.blocked)
        except Exception:
            blocked = True
        if blocked:
            await reply(message, "Previous shopping run active or uncertain, or profile busy. Use /status and inspect manually; no automatic retry.")
        return not blocked

    async def run(self, *args, capture=False):
        if args and args[0] in {"add", "clear", "shop"}:
            async with self.bridge.serial:
                if await asyncio.to_thread(self.blocked):
                    raise ValueError("Previous launch active or uncertain")
        return await super().run(*args, capture=capture)

    async def add(self, update, context):
        if self.authorized(update):
            self.invalidate()
            self.draft = ""
            if await self.available(update.effective_message):
                await super().add(update, context)

    async def clear(self, update, context):
        if self.authorized(update):
            self.invalidate()
            self.draft = ""
            if await self.available(update.effective_message):
                await super().clear(update, context)

    async def shop(self, update, context):
        if self.authorized(update):
            self.invalidate()
            self.draft = ""
            if await self.available(update.effective_message):
                await super().shop(update, context)

    async def transcribe(self, audio, key):
        form = aiohttp.FormData()
        form.add_field("model_id", "scribe_v2")
        form.add_field("tag_audio_events", "false")
        form.add_field("diarize", "false")
        form.add_field("file", audio, filename="voice.ogg", content_type="audio/ogg")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90, connect=10, sock_read=60)) as session:
            async with session.post("https://api.elevenlabs.io/v1/speech-to-text",
                                    headers={"xi-api-key": key}, data=form, allow_redirects=False) as response:
                result = json.loads(await response_bytes(response, 65536))
        text = result.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Missing transcript")
        return text.strip()

    async def speak(self, message, text):
        key = os.getenv("ELEVENLABS_API_KEY", "").strip()
        if not key:
            return
        voice_id = os.getenv("ELEVENLABS_VOICE_ID", GEORGE).strip()
        try:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", voice_id):
                raise ValueError("Invalid voice")
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25, connect=10, sock_read=20)) as session:
                async with session.post("https://api.elevenlabs.io/v1/text-to-speech/" + voice_id,
                                        headers={"xi-api-key": key},
                                        json={"text": text[:500], "model_id": "eleven_multilingual_v2"},
                                        params={"output_format": "mp3_44100_128"}, allow_redirects=False) as response:
                    audio = await response_bytes(response, 1024 * 1024)
            await message.reply_audio(audio=InputFile(audio, filename="reply.mp3"))
        except Exception:
            self.diagnostic("Spoken reply unavailable")

    async def voice(self, update, context):
        if not self.authorized(update):
            return
        self.invalidate()
        generation = self.generation
        message = update.effective_message
        key = os.getenv("ELEVENLABS_API_KEY", "").strip()
        if not key:
            await reply(message, "Set ELEVENLABS_API_KEY to enable voice transcription. You can send text instead.")
            return
        audio = message.voice
        if (audio is None or not isinstance(audio.duration, (int, float))
                or not 0 < audio.duration <= 180
                or (audio.file_size is not None and not 0 < audio.file_size <= AUDIO_LIMIT)):
            await reply(message, "Voice messages must be at most 180 seconds and 8 MB.")
            return
        if not await self.available(message):
            return
        async with self.lock:
            try:
                with BoundedAudio() as buffer:
                    file = await asyncio.wait_for(context.bot.get_file(audio.file_id), 20)
                    await asyncio.wait_for(file.download_to_memory(outfile=buffer), 60)
                    if not 0 < buffer.tell() <= AUDIO_LIMIT:
                        raise ValueError("Invalid audio size")
                    transcript = await self.transcribe(buffer.getvalue(), key)
                await self.preview(message, transcript, generation)
            except Exception:
                reference = self.diagnostic("Voice transcription or preview unavailable")
                await reply(message, f"Could not process voice. Send text or try manually. Diagnostic reference: {reference}.")

    async def text(self, update, context):
        if not self.authorized(update):
            return
        text = update.effective_message.text
        if not isinstance(text, str) or text.startswith("/"):
            return
        self.invalidate()
        generation = self.generation
        if not await self.available(update.effective_message):
            return
        async with self.lock:
            try:
                await self.preview(update.effective_message, text, generation)
            except Exception:
                reference = self.diagnostic("Text preview unavailable")
                await reply(update.effective_message, f"Could not prepare preview. Try manually. Diagnostic reference: {reference}.")

    async def preview(self, message, transcript, generation):
        if generation != self.generation:
            return
        draft = "\n".join(filter(None, (self.draft, transcript.strip())))
        if (not draft.strip() or len(draft.encode()) > 4096
                or any(ord(c) < 32 and c not in "\n\t" for c in draft)):
            await reply(message, "Draft too long or invalid. Cancel with /cancel and send a shorter list (4 KB maximum).")
            return
        self.draft = draft
        result = await self.bridge_call("preview", draft)
        if generation != self.generation:
            return
        if result["status"] == "clarification":
            text = "Transcript/draft:\n" + draft + "\n\n" + result["message"]
            if len(text) > 12000:
                await reply(message, "Preview too long. Send a shorter list; approval unavailable.")
                return
            await reply(message, text)
            await self.speak(message, result["message"])
            return
        token = result["token"]
        if not re.fullmatch(voice.TOKEN, token):
            raise ValueError("Invalid preview token")
        # Include every saved field, even preferences/caps on untouched items.
        text = ("Transcript/draft:\n" + draft + "\n\n" + result["message"]
                + "\n\nFull merged list (quantities are purchasable units; prices in SAR):\n"
                + self.list_preview(result["items"]))
        if len(text) > 12000:
            await reply(message, "Full preview exceeds reply limit. Cancel with /cancel and send a shorter list; approval unavailable.")
            return
        await reply(message, text)
        if generation != self.generation:
            return
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Run shop", callback_data="voice:run:" + token),
                                          InlineKeyboardButton("Cancel", callback_data="voice:cancel:" + token)]])
        await message.reply_text("Approve this exact preview within 10 minutes:", reply_markup=keyboard)
        if generation == self.generation:
            self.pending = (token, message.chat_id)
        await self.speak(message, "Preview ready. Review the full list and changes, then press Run shop or Cancel.")

    @staticmethod
    def list_preview(items):
        lines = []
        for item in items:
            lines.append(f"• {item['name']} × {item['quantity']}")
            if item.get("max_price") is not None:
                lines.append(f"  Unit-price cap: {item['max_price']:g} SAR")
            for field, label in (("preferred_name", "Preferred"), ("brand", "Brand"), ("sku", "SKU")):
                if item.get(field):
                    lines.append(f"  {label}: {item[field]}")
            for alternative in item.get("alternatives", []):
                details = "; ".join(f"{key.replace('_', ' ')}: {value}" for key, value in alternative.items() if value is not None)
                lines.append("  Approved alternative: " + details)
        return "\n".join(lines) or "Empty list."

    async def cancel(self, update, context):
        if self.authorized(update):
            self.invalidate()
            self.draft = ""
            await reply(update.effective_message, "Draft cancelled. An already confirmed run is not cancelled; use /status.")

    async def callback(self, update, context):
        query = update.callback_query
        if query is None or not self.authorized(update):
            return
        match = re.fullmatch(r"voice:(run|cancel):(" + voice.TOKEN + r")", query.data or "")
        if not match or self.pending != (match[2], update.effective_chat.id):
            await query.answer("Stale preview. Send a new message or use /status.")
            return
        if self.lock.locked() or self.stopping:
            await query.answer("Shopping/list update in progress. Use /status.")
            return
        kind, token = match.groups()
        self.invalidate()  # Consume before any I/O; uncertain responses cannot replay.
        self.draft = ""
        if kind == "run":
            await self.lock.acquire()
            self.current = token
            self.uncertain = True
            task = asyncio.create_task(self.confirm_job(query.message, token))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        with contextlib.suppress(Exception):
            await query.answer("Checking confirmation." if kind == "run" else "Cancelled.")
        with contextlib.suppress(Exception):
            await query.edit_message_reply_markup(reply_markup=None)

    async def confirm_job(self, message, token):
        try:
            result = await self.bridge_call("confirm", token)
            self.uncertain = False
            deadline = time.monotonic() + 35 * 60
            if result["status"] == "running":
                with contextlib.suppress(Exception):
                    await reply(message, f"Shopping running. Run reference: {token}. Use /status. No automatic retry.")
            while result["status"] == "running":
                if time.monotonic() >= deadline:
                    self.uncertain = True
                    raise TimeoutError()
                await asyncio.sleep(2)
                result = await self.bridge_call("status", token)
            text = self.outcome(result)
            await reply(message, text)
            await self.speak(message, text)
        except asyncio.CancelledError:
            raise
        except voice.Rejected:
            self.uncertain = False
            with contextlib.suppress(Exception):
                await reply(message, f"Confirmation rejected: preview/list changed or workflow busy. Check /status, then send a new message. Run reference: {token}.")
        except Exception:
            self.uncertain = True
            reference = self.diagnostic("Confirmation or status unavailable; no automatic retry")
            with contextlib.suppress(Exception):
                await reply(message, f"Shopping confirmation/status unavailable. Review manually; no automatic retry. Run reference: {token}. Diagnostic reference: {reference}.")
        finally:
            try:
                child = self.bridge.children.get(token)
                if child is not None and child.poll() is None:
                    # Cancellation/timeout must await worker and browser cleanup.
                    await self.bridge.cleanup(None)
            finally:
                self.lock.release()

    def outcome(self, result):
        status = result.get("status")
        descriptions = {"completed": "Shopping run finished. Manual checkout approval required; no order placed.",
                        "incomplete": "Shopping incomplete. Review cart and outstanding items manually.",
                        "failed": voice.FAILURE, "interrupted": voice.FAILURE,
                        "running": "Shopping still running. No automatic retry.",
                        "preview": "Awaiting approval of the newest preview.",
                        "expired": "Preview expired. Send a new message."}
        text = descriptions.get(status, voice.FAILURE)
        if status in {"completed", "incomplete"}:
            try:
                data = voice.read_json(self.bridge.root / result["token"] / "result.json")
                if data.get("status") == "attempt_saved" and (status != "completed" or not data.get("summary", {}).get("cart")):
                    data["success"] = False
                text += "\n" + shop_text(data)
            except Exception:
                text += "\nShopping incomplete: verified details unavailable. Review Tamimi manually."
        return text + "\nRun reference: " + result["token"]

    async def status(self, update, context):
        if not self.authorized(update):
            return
        try:
            async with self.bridge.serial:
                active = await asyncio.to_thread(self.active_status)
            token = self.current or (active["token"] if active else None) or (self.pending[0] if self.pending else None)
            result = await self.bridge_call("status", token) if token else None
            text = self.outcome(result) if result else "No confirmed voice run recorded."
        except Exception:
            text = "Previous launch uncertain. Inspect saved state and cart manually; no automatic retry."
        await reply(update.effective_message, text)

    async def shutdown(self, application):
        self.stopping = True
        self.invalidate()
        await super().shutdown(application)
        await self.bridge.cleanup(None)
