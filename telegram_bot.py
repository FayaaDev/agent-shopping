"""Private, authorized Telegram interface to shopping.py."""

import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid

from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler
from shopping import error_result

PROJECT = Path(__file__).resolve().parent
HELP = "/add <multiword name> <positive quantity>\n/clear\n/list\n/shop [location]\n/help\nShopping stops for manual approval; no payment or order is placed."
OUTPUT_LIMIT = 65536
ERROR_PHASES = frozenset("config db_open list_read browser_startup readiness readiness_output cart_planning cart_tools cart_agent cart_output assessment persistence result_output cleanup subprocess result_file result_format interrupted".split())
ERROR_TYPES = frozenset("Error Exception RuntimeError ValueError TypeError KeyError AttributeError OSError FileNotFoundError PermissionError TimeoutError JSONDecodeError ValidationError APIError APIConnectionError APIStatusError RateLimitError AuthenticationError CancelledError KeyboardInterrupt".split())
INCOMPLETE_REASONS = {
    "guard_blocked_action": "Guard blocked an unsafe or unsupported action",
    "product_plus_unavailable": "Product + control unavailable",
    "step_limit": "Step budget exhausted",
    "agent_failure": "Agent/tool failure",
    "agent_stopped": "Agent stopped",
    "no_final_output": "No final output",
}
UNRESOLVED_REASONS = {
    "ambiguous_type": "Requested product type is unclear",
    "insufficient_evidence": "Not enough verified product information",
    "insufficient_package": "No sufficiently sized package verified",
    "price_cap": "No suitable product within the price limit",
    "no_safe_candidate": "No safe substitute verified",
    "recovery_exhausted": "Item recovery limit reached",
    "item_unavailable": "Requested item unavailable",
    "quantity_unverified": "Cart quantity could not be verified",
}


def error_metadata(data):
    phase, error_type = data.get("phase"), data.get("error_type")
    return (phase if isinstance(phase, str) and phase in ERROR_PHASES else "unknown",
            error_type if isinstance(error_type, str) and error_type in ERROR_TYPES else "Error")


class PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            return os.fdopen(fd, "a", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise

    def handleError(self, record):
        raise  # Let the caller suppress failures without printing private data.


def record_diagnostic(db, run_id, exit_code, result_exists, data):
    phase, error_type = error_metadata(data)
    diagnostic = data.get("diagnostic")
    diagnostic = diagnostic if isinstance(diagnostic, dict) else {}
    def scrub(value):
        value = re.sub(r"https?://[^\s\"'<>]+", "[URL REDACTED]", str(value))
        value = re.sub(r"\b[^\s@]+@[^\s@]+\b", "[EMAIL REDACTED]", value)
        return error_result(ValueError(value), "diagnostic")["diagnostic"]["message"]
    frames = diagnostic.get("frames", [])
    status = data.get("result_status", data.get("status"))
    error_code = data.get("error_code")
    payload = {"run_id": run_id, "exit_code": exit_code, "result_exists": result_exists,
               "status": status if isinstance(status, str) and status in {"error", "attempt_saved", "readiness_failed", "no_summary"} else "unknown",
               "phase": phase, "error_type": error_type,
               "error_code": error_code if isinstance(error_code, str) and error_code in INCOMPLETE_REASONS else "unknown",
               "diagnostic": {"message": scrub(diagnostic.get("message", "")), "frames": [
                   {"file": scrub(Path(str(frame.get("file", ""))).name)[:200],
                    "line": frame.get("line") if type(frame.get("line")) is int else None,
                    "function": scrub(frame.get("function", ""))[:200]}
                   for frame in (frames[-20:] if isinstance(frames, list) else []) if isinstance(frame, dict)]}}
    if type(data.get("attempt")) is int:
        payload["attempt"] = data["attempt"]
    handler = PrivateRotatingFileHandler(Path(db).parent / "shopping-diagnostics.jsonl",
                                         maxBytes=128 * 1024, backupCount=2, encoding="utf-8", delay=True)
    try:
        handler.handle(logging.LogRecord("shopping-diagnostics", logging.ERROR, "", 0,
                                        json.dumps(payload, ensure_ascii=True), (), None))
    finally:
        handler.close()


def error_text(data, run_id, code, interrupted=False):
    phase, error_type = error_metadata(data)
    outcome = "Shopping interrupted" if interrupted else "Shopping failed"
    return (f"{outcome}. Stage: {phase}; type: {error_type}; exit code: {code if code is not None else 'unavailable'}. "
            f"Diagnostic reference: {run_id}. The cart may have changed. Review it manually. "
            "Manual approval required for checkout. There is no automatic retry.")


def configuration():
    load_dotenv(PROJECT / ".env.local")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    user = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise ValueError("Set a valid TELEGRAM_BOT_TOKEN.")
    if not re.fullmatch(r"[0-9]+", user) or int(user) < 1:
        raise ValueError("Set TELEGRAM_ALLOWED_USER_ID to a positive numeric user ID.")
    db = Path(os.getenv("SHOPPING_DB", str(PROJECT / "shopping.db"))).expanduser()
    if not db.is_absolute():
        db = PROJECT / db
    return token, int(user), db.resolve()


async def reply(message, text):
    # Bound both message count and UTF-16 size, including non-BMP product names.
    text = str(text)
    if len(text) > 12000:
        text = text[:11900] + "\n[Reply truncated; review full details locally.]"
    for offset in range(0, len(text), 1800):
        await message.reply_text(text[offset:offset + 1800], parse_mode=None)


def result_string(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Invalid result string")


def result_number(value, positive=False):
    if (type(value) not in {int, float} or not math.isfinite(value)
            or (value <= 0 if positive else value < 0)):
        raise ValueError("Invalid result number")


def validate_product(product):
    if not isinstance(product, dict):
        raise ValueError("Invalid result product")
    for key in ("name", "product_type", "package_size", "package_unit", "match_reason"):
        result_string(product.get(key))
    if product.get("sku") is not None:
        result_string(product["sku"])
    result_number(product.get("package_quantity"), positive=True)
    result_number(product.get("unit_price"))
    if "mainstream_brand" in product and type(product["mainstream_brand"]) is not bool:
        raise ValueError("Invalid result brand evidence")
    if product.get("matches_request") is not True:
        raise ValueError("Invalid result match evidence")
    evidence = product.get("match_evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("Invalid result match evidence")
    visible = " ".join(str(product.get(key) or "") for key in ("name", "brand", "package_size"))
    for quote in evidence:
        result_string(quote)
        if quote.casefold() not in visible.casefold():
            raise ValueError("Invalid result match evidence")


def shop_text(data):
    status = data.get("status")
    if status == "readiness_failed":
        return "Shopping could not start: login/address readiness failed. Run shopping.py setup on your Mac, stop the server bot and import the refreshed snapshot, then verify readiness before retrying manually. Manual approval required for checkout."
    if status == "no_summary":
        return "Shopping incomplete: no verified summary. Review the cart manually before retrying. Manual approval required for checkout."
    if status != "attempt_saved" or type(data.get("success")) is not bool:
        raise ValueError("Invalid result")
    attempt = data["attempt"]
    summary = data["summary"]
    if type(attempt) is not int or attempt < 1 or not isinstance(summary, dict):
        raise ValueError("Invalid attempt")
    cart = summary.get("cart")
    outcomes = summary.get("item_outcomes", [])
    unresolved = summary.get("unresolved", [])
    missing = summary.get("missing_or_over_cap", [])
    if any(not isinstance(value, list) for value in (cart, outcomes, unresolved, missing)):
        raise ValueError("Invalid result lists")
    for name in unresolved + missing:
        result_string(name)
    if summary.get("slot") is not None:
        result_string(summary["slot"])
    if "stage" in summary:
        result_string(summary["stage"])
    for row in cart:
        if not isinstance(row, dict):
            raise ValueError("Invalid cart row")
        result_string(row.get("name"))
        if type(row.get("quantity")) is not int or row["quantity"] < 1:
            raise ValueError("Invalid cart quantity")
        result_number(row.get("unit_price"))
    reasons = {}
    substitutions = []
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            raise ValueError("Invalid item outcome")
        name, mode = outcome.get("item_name"), outcome.get("selection_mode")
        result_string(name)
        if "selection_mode" not in outcome or (mode is not None and mode not in (
                "exact", "approved_alternative", "automatic_substitution")):
            raise ValueError("Invalid selection mode")
        reason = outcome.get("unresolved_reason")
        if reason is not None:
            result_string(reason)
        product = outcome.get("product")
        if mode is None or reason is not None:
            reasons[name] = UNRESOLVED_REASONS.get(reason, "Manual review required")
            continue
        if not isinstance(product, dict):
            raise ValueError("Invalid result product")
        result_string(product.get("name"))
        result_number(product.get("unit_price"))
        if mode == "automatic_substitution":
            validate_product(product)
            result_string(outcome.get("requested_type"))
            if outcome.get("required_package_quantity") is not None:
                result_string(outcome.get("package_unit"))
                result_number(outcome["required_package_quantity"], positive=True)
            for key in ("preferred_brand_available", "exact_unavailable", "approved_unavailable"):
                if outcome.get(key) is not None and type(outcome[key]) is not bool:
                    raise ValueError("Invalid substitution evidence")
            candidates = outcome.get("candidates")
            if not isinstance(candidates, list):
                raise ValueError("Invalid substitution candidates")
            for candidate in candidates:
                validate_product(candidate)
            validate_product(outcome.get("comparison"))
            substitutions.append(
                f"Automatic substitution: {name} → {product['name']} — "
                f"observed package {product['package_size']} "
                f"({product['package_quantity']:g} {product['package_unit']}), "
                 f"price per purchasable unit {product['unit_price']:g}")
    complete = (data["success"] and summary.get("stage") == "slot_selected"
                and bool(summary.get("slot")) and not unresolved and not reasons and not missing)
    lines = ["Cart prepared." if complete else "Shopping incomplete: review outstanding items.",
             f"Attempt: {attempt}",
             "Manual approval required for checkout. Review Tamimi and finish checkout yourself. No order has been placed by this bot."]
    for row in cart:
        lines.append(f"{row['name']} × {row['quantity']} purchasable units — unit price {row['unit_price']}")
    lines.extend(substitutions)
    lines.extend([f"Slot: {summary.get('slot') or 'not selected'}",
                  f"Stage: {summary.get('stage', 'unknown')}"])
    for name in dict.fromkeys(unresolved + list(reasons)):
        lines.append(f"Unresolved: {name} — {reasons.get(name, 'Manual review required')}")
    if missing:
        lines.append("Missing/over cap: " + ", ".join(missing))
    return "\n".join(lines)


class TelegramShopping:
    def __init__(self, user_id, db):
        self.user_id = user_id
        self.db = Path(db).resolve()
        self.lock = asyncio.Lock()
        self.tasks = set()
        self.stopping = False

    def authorized(self, update):
        return (update.effective_chat is not None and update.effective_chat.type == "private"
                and update.effective_user is not None and update.effective_user.id == self.user_id
                and update.effective_message is not None)

    async def run(self, *args, capture=False):
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(PROJECT / "shopping.py"), "--db", str(self.db), *args,
            cwd=str(PROJECT), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output = b""
            if capture:
                while chunk := await process.stdout.read(8192):
                    output += chunk
                    if len(output) > OUTPUT_LIMIT:
                        raise ValueError("Output too large")
            code = await process.wait()
            return code, output
        finally:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), 30)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()

    async def help(self, update, context):
        if self.authorized(update):
            await reply(update.effective_message, HELP)

    async def add(self, update, context):
        if not self.authorized(update):
            return
        args = context.args
        if (len(args) < 2 or not re.fullmatch(r"[0-9]+", args[-1])
                or len(args[-1]) > 18 or int(args[-1]) < 1 or not " ".join(args[:-1]).strip()):
            await reply(update.effective_message, "Usage: /add <multiword name> <positive quantity>")
            return
        if self.lock.locked() or self.stopping:
            await reply(update.effective_message, "Shopping/list update in progress. Try /add after it finishes.")
            return
        async with self.lock:
            try:
                code, _ = await self.run("add", "--", " ".join(args[:-1]), args[-1])
                text = "Shopping list updated." if code == 0 else "Could not update the shopping list. Check locally."
            except (OSError, ValueError):
                text = "Could not update the shopping list. Check locally."
            await reply(update.effective_message, text)

    async def clear(self, update, context):
        if not self.authorized(update):
            return
        if context.args:
            await reply(update.effective_message, "Usage: /clear")
            return
        if self.lock.locked() or self.stopping:
            await reply(update.effective_message, "Shopping/list update in progress. Try /clear after it finishes.")
            return
        async with self.lock:
            try:
                code, _ = await self.run("clear")
                text = ("Saved shopping list, product preferences, and approved alternatives cleared. Live cart unchanged."
                        if code == 0 else "Could not clear the shopping list. Check locally.")
            except (OSError, ValueError):
                text = "Could not clear the shopping list. Check locally."
            await reply(update.effective_message, text)

    async def list(self, update, context):
        if not self.authorized(update):
            return
        try:
            code, output = await self.run("list", capture=True)
            if code:
                raise ValueError("List failed")
            items = json.loads(output)
            text = "\n".join(f"{item['name']} × {item['quantity']}" for item in items) or "Shopping list is empty."
        except (OSError, ValueError, KeyError, TypeError):
            text = "Could not read the shopping list. Check locally."
        await reply(update.effective_message, text)

    async def shop(self, update, context):
        if not self.authorized(update):
            return
        if self.lock.locked() or self.stopping:
            await reply(update.effective_message, "Shopping/list update already in progress.")
            return
        await self.lock.acquire()
        task = asyncio.create_task(self.shop_job(update.effective_message, " ".join(context.args)))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def shop_job(self, message, location):
        run_id = uuid.uuid4().hex[:12]
        code, result_exists, data = None, False, {}
        phase = "subprocess"
        def report(error, interrupted=False):
            try:
                record_diagnostic(self.db, run_id, code, result_exists,
                                  dict(error, result_status=data.get("status") if isinstance(data, dict) else None))
            except Exception:
                pass
            if error.get("status") == "no_summary" and not interrupted:
                error_code = error.get("error_code")
                reason = INCOMPLETE_REASONS.get(error_code, "Unknown reason") if isinstance(error_code, str) else "Unknown reason"
                return (f"Shopping incomplete: no verified summary. Reason: {reason}. "
                        f"Diagnostic reference: {run_id}. The cart may have changed. Review it manually. "
                         "Manual approval required for checkout. There is no automatic retry.")
            return error_text(error, run_id, code, interrupted)
        try:
            await reply(message, "Shopping started. /list and /help remain available.")
            with tempfile.TemporaryDirectory(prefix="telegram-shopping-") as directory:
                result = Path(directory) / "result.json"
                args = ["shop", "--result-file", str(result)]
                if location:
                    args.extend(["--location", location])
                code, _ = await self.run(*args)
                phase = "result_file"
                result_exists = result.exists()
                with result.open("rb") as file:
                    raw = file.read(OUTPUT_LIMIT + 1)
                if len(raw) > OUTPUT_LIMIT:
                    raise ValueError("Result too large")
                data = json.loads(raw)
                phase = "result_format"
                if not isinstance(data, dict):
                    raise ValueError("Invalid result object")
                if code < 0 or code in {130, 143}:
                    error = dict(data, phase="interrupted", error_type="CancelledError")
                    text = report(error, interrupted=True)
                elif data.get("status") == "error":
                    text = report(data)
                elif code not in {0, 1}:
                    raise ValueError("Unexpected child exit code")
                elif data.get("status") == "no_summary":
                    text = report(data)
                else:
                    if data.get("status") == "attempt_saved" and code != 0:
                        data["success"] = False
                    text = shop_text(data)
                    if data.get("status") == "attempt_saved" and isinstance(data.get("diagnostic"), dict):
                        try:
                            record_diagnostic(self.db, run_id, code, result_exists, data)
                        except Exception:
                            pass
                        text += f"\nDiagnostic reference: {run_id}."
            await reply(message, text)
        except asyncio.CancelledError as exc:
            report(error_result(exc, "interrupted"), interrupted=True)
            raise
        except Exception as exc:
            # Never relay exceptions: browser/HTTP errors can contain credentials.
            error = error_result(exc, phase)
            if isinstance(data, dict) and type(data.get("attempt")) is int:
                error["attempt"] = data["attempt"]
            text = report(error, interrupted=code is not None and (code < 0 or code in {130, 143}))
            try:
                await reply(message, text)
            except Exception:
                pass
        finally:
            self.lock.release()

    async def shutdown(self, application):
        self.stopping = True
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # A task cancelled before its first step never enters shop_job's finally.
        if self.lock.locked():
            self.lock.release()


async def error_handler(update, context):
    # PTB's default exception logging may include request URLs and tokens.
    pass


def main():
    try:
        token, user, db = configuration()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    adapter = TelegramShopping(user, db)
    application = (Application.builder().token(token).post_stop(adapter.shutdown)
                   .post_shutdown(adapter.shutdown).build())
    for command, handler in (("start", adapter.help), ("help", adapter.help),
                             ("add", adapter.add), ("clear", adapter.clear),
                             ("list", adapter.list), ("shop", adapter.shop)):
        application.add_handler(CommandHandler(command, handler))
    application.add_error_handler(error_handler)
    application.run_polling(bootstrap_retries=0, drop_pending_updates=True, allowed_updates=["message"])


if __name__ == "__main__":
    main()
