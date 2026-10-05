"""Single-process private web owner for shopping.py."""

import asyncio
from collections import deque
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import tempfile
import time
from urllib.parse import urlsplit

from aiohttp import web
from dotenv import load_dotenv

from shopping import error_result
from telegram_bot import OUTPUT_LIMIT, PROJECT, TelegramShopping, record_diagnostic, shop_text

COOKIE = "shopping_session"
SESSION_SECONDS = 12 * 60 * 60
SERVICE = web.AppKey("service", object)
MESSAGES = {
    "running": "Shopping started.",
    "complete": "Cart prepared. Manual checkout approval required; no order placed.",
    "incomplete": "Shopping incomplete. Review the cart manually before retrying.",
    "error": "Shopping failed. Review the cart manually before retrying.",
}
PRODUCT_FIELDS = {"name": str, "sku": str, "brand": str, "package_size": str,
                  "package_unit": str, "product_type": str, "package_quantity": float,
                  "unit_price": float}


def text(value, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid text")
    return value.strip()


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("Invalid number")
    return value


def fields(source, schema):
    if not isinstance(source, dict):
        raise ValueError("Invalid object")
    result = {}
    for key, kind in schema.items():
        if key not in source:
            continue
        value = source[key]
        if value is not None:
            if kind is str:
                value = text(value, 1000)
            elif kind is float:
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise ValueError("Invalid number")
            elif type(value) is not kind:
                raise ValueError("Invalid value")
        result[key] = value
    return result


def clean_run(source):
    """Also applied to disk state: never return arbitrary persisted fields."""
    run_id = text(source["id"], 64)
    status = source["status"]
    if status not in MESSAGES:
        raise ValueError("Invalid status")
    result = {"id": run_id, "status": status, "message": MESSAGES[status], "cart": [], "outcomes": []}
    if "attempt" in source:
        if type(source["attempt"]) is not int or source["attempt"] < 1:
            raise ValueError("Invalid attempt")
        result["attempt"] = source["attempt"]
        result["confirmed"] = source.get("confirmed") is True
    if source.get("slot") is not None:
        result["slot"] = text(source["slot"], 1000)
    for key in ("cart", "outcomes", "unresolved", "missing_or_over_cap"):
        if not isinstance(source.get(key, []), list) or len(source.get(key, [])) > 1000:
            raise ValueError("Invalid list")
    for key in ("unresolved", "missing_or_over_cap"):
        result[key] = [text(name, 1000) for name in source.get(key, [])]
    unresolved = set(result["unresolved"]) | set(result["missing_or_over_cap"])
    result["cart"] = [fields(row, {**PRODUCT_FIELDS, "quantity": int}) for row in source.get("cart", [])]
    for row in source.get("outcomes", []):
        outcome = fields(row, {"item_name": str})
        mode = row.get("selection_mode")
        outcome["selection_mode"] = mode if mode in ("exact", "approved_alternative", "automatic_substitution") else None
        # Free-form explanations and diagnostic-looking unresolved reasons stay private.
        outcome["unresolved"] = (row.get("unresolved") is True or row.get("unresolved_reason") is not None
                                 or mode is None or outcome.get("item_name") in unresolved)
        if row.get("product") is not None:
            outcome["product"] = fields(row["product"], PRODUCT_FIELDS)
        result["outcomes"].append(outcome)
    return result


class WebConfigurationError(ValueError):
    """Explicit validation failures with canned, public-safe messages."""


class ShoppingWeb:
    def __init__(self, db, password, origin, secure):
        if not isinstance(password, str) or not 16 <= len(password) <= 1024:
            raise WebConfigurationError("WEB_PASSWORD must contain 16–1024 characters.")
        try:
            if not isinstance(origin, str):
                raise ValueError
            parsed = urlsplit(origin)
            if (any(c.isspace() or c in "\\" for c in origin)
                    or parsed.scheme not in ("http", "https") or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path or parsed.query or parsed.fragment
                    or origin != f"{parsed.scheme}://{parsed.netloc}"):
                raise ValueError
            parsed.port  # Validate port syntax/range.
        except ValueError:
            raise WebConfigurationError(
                "WEB_ORIGIN must be an http/https origin with a host and optional valid port; no credentials, path, query, or fragment."
            ) from None
        self.runner = TelegramShopping(None, db)
        self.lock = self.runner.lock
        self.password = password.encode()
        self.origin, self.secure = origin, secure
        self.sessions = {}
        self.logins = deque(maxlen=10)
        self.tasks = set()
        self.stopping = False
        self.path = self.runner.db.with_name(self.runner.db.name + ".web-run.json")
        self.latest = None
        if self.path.exists():
            try:
                self.latest = clean_run(self.read_json(self.path))
                if self.latest["status"] == "running":
                    self.latest["status"] = "incomplete"
                    self.diagnostic(error_result(RuntimeError("Interrupted"), "interrupted"))
                self.save()
            except Exception as exc:
                self.latest = None
                self.diagnostic(error_result(exc, "persistence"))

    @staticmethod
    def read_json(path):
        with path.open("rb") as file:
            raw = file.read(OUTPUT_LIMIT + 1)
        if len(raw) > OUTPUT_LIMIT:
            raise ValueError("Output too large")
        return json.loads(raw)

    def diagnostic(self, data, code=None, exists=False):
        try:
            record_diagnostic(self.runner.db, self.latest["id"] if self.latest else "web",
                              code, exists, data)
        except Exception:
            pass

    def save(self):
        self.latest = clean_run(self.latest)
        raw = json.dumps(self.latest, allow_nan=False).encode()
        if len(raw) > OUTPUT_LIMIT:
            raise ValueError("State too large")
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".web-run-")
        try:
            with os.fdopen(fd, "wb") as file:
                os.fchmod(file.fileno(), 0o600)
                file.write(raw)
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def authenticated(self, request):
        now = time.monotonic()
        self.sessions = {token: expiry for token, expiry in self.sessions.items() if expiry > now}
        return request.cookies.get(COOKIE) in self.sessions

    async def state(self, request):
        code, output = await self.runner.run("list", capture=True)
        if code or len(output) > OUTPUT_LIMIT:
            raise RuntimeError("List failed")
        rows = json.loads(output)
        if not isinstance(rows, list) or len(rows) > 1000:
            raise RuntimeError("Invalid items")
        items = []
        for row in rows:
            item = fields(row, {"name": str, "quantity": int, "preferred_name": str,
                                "sku": str, "brand": str, "max_price": float})
            if not item.get("name") or type(item.get("quantity")) is not int or item["quantity"] < 1:
                raise RuntimeError("Invalid item")
            alternatives = row.get("alternatives", [])
            if not isinstance(alternatives, list):
                raise RuntimeError("Invalid alternatives")
            item["alternatives"] = [fields(alt, {"name": str, "sku": str}) for alt in alternatives]
            items.append(item)
        return web.json_response({"items": items, "run": self.latest})

    async def post(self, request):
        action = request.match_info["action"]
        try:
            if request.content_type != "application/json":
                raise ValueError("JSON required")
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Object required")
        except (ValueError, UnicodeError, web.HTTPRequestEntityTooLarge):
            return web.json_response({"error": "Invalid request."}, status=400)
        if action == "login":
            now = time.monotonic()
            while self.logins and self.logins[0] <= now - 60:
                self.logins.popleft()
            if len(self.logins) == self.logins.maxlen:
                return web.json_response({"error": "Try again later."}, status=429)
            self.logins.append(now)
            password = body.get("password")
            if not isinstance(password, str) or len(password) > 1024:
                return web.json_response({"error": "Invalid request."}, status=400)
            if not hmac.compare_digest(password.encode(), self.password):
                return web.json_response({"error": "Unauthorized."}, status=401)
            self.authenticated(request)
            if len(self.sessions) >= 128:
                self.sessions.pop(next(iter(self.sessions)))
            self.sessions.pop(request.cookies.get(COOKIE), None)
            token = secrets.token_urlsafe(32)
            self.sessions[token] = now + SESSION_SECONDS
            response = web.json_response({"authenticated": True})
            response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                                secure=self.secure, samesite="Strict", path="/")
            return response
        if action == "logout":
            self.sessions.pop(request.cookies.get(COOKIE), None)
            response = web.json_response({"authenticated": False})
            response.del_cookie(COOKIE, path="/")
            return response
        try:
            if action == "items":
                if type(body.get("quantity")) is not int or not 1 <= body["quantity"] <= 999:
                    raise ValueError("Invalid quantity")
                args = ["add", "--", text(body.get("name")), str(body["quantity"])]
            elif action == "remove":
                args = ["remove", "--", text(body.get("name"))]
            elif action == "clear":
                if body:
                    raise ValueError("Empty body required")
                args = ["clear"]
            elif action == "preference":
                if set(body) != {"name", "product", "sku", "brand", "max_price"}:
                    raise ValueError("Full replacement required")
                args = ["prefer"]
                for key in ("sku", "brand", "max_price"):
                    if body[key] is not None:
                        value = number(body[key]) if key == "max_price" else text(body[key])
                        args.append(f"--{key.replace('_', '-')}={value}")
                args += ["--", text(body["name"]), text(body["product"])]
            elif action == "shop":
                location = body.get("location")
                if location is not None:
                    location = text(location, 500)
                args = []
            elif action == "confirm":
                if type(body.get("attempt")) is not int or body["attempt"] < 1 or body.get("confirmed") is not True:
                    raise ValueError("Explicit confirmation required")
                args = ["confirm-purchase", str(body["attempt"])]
            else:
                return web.json_response({"error": "Not found."}, status=404)
        except (ValueError, TypeError, OverflowError):
            return web.json_response({"error": "Invalid request."}, status=400)
        if self.stopping or self.lock.locked():
            return web.json_response({"error": "Shopping/list update in progress."}, status=409)
        await self.lock.acquire()
        background = False
        try:
            if action == "shop":
                self.latest = {"id": secrets.token_hex(12), "status": "running"}
                try:
                    self.save()
                except Exception:
                    self.latest = clean_run({"id": self.latest["id"], "status": "error"})
                    raise
                task = asyncio.create_task(self.shop_job(location))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
                background = True
                return web.json_response(self.latest, status=202)
            if action == "confirm" and (not self.latest or self.latest.get("attempt") != body["attempt"]
                                        or self.latest["status"] not in ("complete", "incomplete")
                                        or self.latest.get("confirmed")):
                return web.json_response({"error": "Attempt is not available for confirmation."}, status=409)
            code, _ = await self.runner.run(*args)
            if code:
                self.diagnostic(error_result(RuntimeError("Command failed"), "subprocess"), code)
                return web.json_response({"error": "Could not update shopping data."}, status=400)
            if action == "confirm":
                self.latest["confirmed"] = True
                self.save()
            return web.json_response({"ok": True})
        finally:
            if not background:
                self.lock.release()

    async def shop_job(self, location):
        code, exists, phase = None, False, "subprocess"
        try:
            with tempfile.TemporaryDirectory(prefix="web-shopping-") as directory:
                path = Path(directory) / "result.json"
                args = ["shop", "--result-file", str(path)]
                if location:
                    args += ["--location", location]
                code, _ = await self.runner.run(*args)
                phase, exists = "result_file", path.exists()
                data = self.read_json(path)
                phase = "result_format"
                if not isinstance(data, dict):
                    raise ValueError("Invalid result")
                if code < 0 or code in (130, 143):
                    self.latest["status"] = "incomplete"
                    self.diagnostic(error_result(RuntimeError("Interrupted"), "interrupted"), code, exists)
                elif data.get("status") == "error":
                    self.latest["status"] = "error"
                    self.diagnostic(data, code, exists)
                elif code not in (0, 1):
                    raise ValueError("Unexpected exit code")
                else:
                    if data.get("status") == "attempt_saved" and code != 0:
                        data["success"] = False
                    message = shop_text(data)
                    self.latest["status"] = "complete" if message.startswith("Cart prepared") else "incomplete"
                    if data.get("status") == "attempt_saved":
                        summary = data["summary"]
                        self.latest.update(attempt=data["attempt"], cart=summary["cart"],
                                           outcomes=summary.get("item_outcomes", []), slot=summary.get("slot"),
                                           unresolved=summary.get("unresolved", []),
                                           missing_or_over_cap=summary.get("missing_or_over_cap", []))
                    if data.get("status") in ("no_summary", "readiness_failed") or "diagnostic" in data:
                        self.diagnostic(data, code, exists)
                self.save()
        except asyncio.CancelledError as exc:
            self.latest = {"id": self.latest["id"], "status": "incomplete"}
            self.diagnostic(error_result(exc, "interrupted"), code, exists)
            raise
        except Exception as exc:
            self.latest = {"id": self.latest["id"], "status": "error"}
            self.diagnostic(error_result(exc, phase), code, exists)
        finally:
            try:
                self.save()
            except Exception as exc:
                self.diagnostic(error_result(exc, "persistence"), code, exists)
            self.lock.release()

    async def shutdown(self, app):
        self.stopping = True
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.latest and self.latest["status"] == "running":
            self.latest["status"] = "incomplete"
            self.diagnostic(error_result(RuntimeError("Interrupted"), "interrupted"))
            try:
                self.save()
            except Exception as exc:
                self.diagnostic(error_result(exc, "persistence"))
        if self.lock.locked():
            self.lock.release()


def create_app(db=None, password=None, origin=None, secure=None):
    service = ShoppingWeb(db or os.getenv("SHOPPING_DB", str(PROJECT / "shopping.db")),
                          password if password is not None else os.getenv("WEB_PASSWORD", ""),
                          origin if origin is not None else os.getenv("WEB_ORIGIN", "http://127.0.0.1:8080"),
                          secure if secure is not None else os.getenv("WEB_COOKIE_SECURE", "true").lower() != "false")

    @web.middleware
    async def boundary(request, handler):
        task = asyncio.current_task()
        service.tasks.add(task)
        try:
            if service.stopping:
                response = web.json_response({"error": "Service stopping."}, status=409)
            elif request.method == "POST" and request.headers.get("Origin") != service.origin:
                response = web.json_response({"error": "Invalid origin."}, status=403)
            elif request.path.startswith("/api/") and request.path != "/api/login" and not service.authenticated(request):
                response = web.json_response({"error": "Unauthorized."}, status=401)
            else:
                response = await handler(request)
        except web.HTTPException as exc:
            response = web.json_response({"error": "Invalid request."}, status=400 if exc.status == 413 else exc.status)
        except Exception as exc:
            service.diagnostic(error_result(exc, "subprocess"))
            response = web.json_response({"error": "Request failed."}, status=500)
        finally:
            service.tasks.discard(task)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                                 "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"})
        return response

    async def session(request):
        return web.json_response({"authenticated": True})

    async def static(request):
        files = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                 "/style.css": ("style.css", "text/css"),
                 "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
                 "/icon.svg": ("icon.svg", "image/svg+xml")}
        filename, content_type = files[request.path]
        try:
            content = (PROJECT / "web" / filename).read_bytes()
        except FileNotFoundError:
            raise web.HTTPNotFound() from None
        return web.Response(body=content, content_type=content_type)

    app = web.Application(middlewares=[boundary], client_max_size=16 * 1024)
    app[SERVICE] = service
    app.router.add_get("/api/state", service.state)
    app.router.add_get("/api/session", session)
    app.router.add_post("/api/{action}", service.post)
    for path in ("/", "/app.js", "/style.css", "/manifest.webmanifest", "/icon.svg"):
        app.router.add_get(path, static)
    app.on_shutdown.append(service.shutdown)
    return app


def main():
    try:
        load_dotenv(PROJECT / ".env.local")
        try:
            port = int(os.getenv("WEB_PORT", "8080"))
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            raise WebConfigurationError("WEB_PORT must be an integer from 1 to 65535.") from None
        app = create_app()
    except WebConfigurationError as exc:
        raise SystemExit(str(exc)) from None
    except OSError:
        raise SystemExit("Could not access web configuration or data files. Check filesystem permissions and paths.") from None
    except ValueError:
        raise SystemExit("Invalid web configuration.") from None
    web.run_app(app, host=os.getenv("WEB_HOST", "127.0.0.1"), port=port, access_log=None)


if __name__ == "__main__":
    main()
