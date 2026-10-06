"""Authenticated loopback HTTP bridge. Run: python voice_bridge.py --port 8765."""

import argparse
import asyncio
import contextlib
import hmac
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time

from aiohttp import web

import voice_shortcut as voice

PROJECT = Path(__file__).resolve().parent
TERMINAL = {"completed", "incomplete", "failed", "interrupted"}
BRIDGE = web.AppKey("bridge", object)


class Bridge:
    def __init__(self, db):
        self.db = Path(db).resolve()
        self.root = voice.state_root(self.db)
        self.children = {}
        self.serial = asyncio.Lock()
        self.stopping = False

    def busy(self):
        result = subprocess.run(["ps", "-axo", "pid=,ppid=,args="], capture_output=True,
                                text=True, timeout=5, check=True)
        processes = {}
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) != 3:
                raise voice.Rejected("Browser ownership unavailable. No launch.")
            processes[int(parts[0])] = (int(parts[1]), parts[2])
        ancestors = set()
        pid = os.getpid()
        while pid and pid not in ancestors:
            ancestors.add(pid)
            pid = processes.get(pid, (0, ""))[0]
        for pid, (_, command) in processes.items():
            if pid in ancestors:
                continue
            try:
                args = shlex.split(command)
            except ValueError:
                raise voice.Rejected("Browser ownership unavailable. No launch.") from None
            for index, arg in enumerate(args):
                if Path(arg).name == "shopping.py" and any(
                        command in args[index + 1:] for command in ("setup", "shop", "readiness")):
                    return True
                if Path(arg).name in {"telegram_bot.py", "web_app.py"}:
                    return True
            for index, arg in enumerate(args):
                if arg.startswith("--user-data-dir=") and ".browser-profile" in arg:
                    return True
                if arg == "--user-data-dir" and index + 1 < len(args) and ".browser-profile" in args[index + 1]:
                    return True
        return False

    def status(self, token):
        directory = self.root / token
        if not (directory / "plan.json").exists():
            raise voice.Rejected("Unknown token.")
        child = self.children.get(token)
        if child is not None and child.poll() is None:
            return {"token": token, "status": "running", "message": "Confirmed run still active."}
        if (directory / "started.json").exists():
            if (directory / "status.json").exists() and (child is None or child.returncode == 0):
                outcome = voice.read_json(directory / "status.json")
                if outcome.get("status") in TERMINAL:
                    return {"token": token, "status": outcome["status"],
                            "message": str(outcome.get("message", voice.FAILURE))[:12000]}
            return {"token": token, "status": "interrupted", "message": voice.FAILURE}
        expired = voice.read_json(directory / "plan.json")["expires"] < time.time()
        return {"token": token, "status": "expired" if expired else "preview",
                "message": "Preview expired." if expired else "Awaiting confirmation."}

    def confirm(self, token):
        current = self.status(token)
        if current["status"] != "preview":
            return current
        directory = self.root / token
        active = self.root / "active.json"
        if active.exists():
            previous = voice.read_json(active)["token"]
            if not re.fullmatch(voice.TOKEN, previous):
                raise voice.Rejected("Previous launch uncertain. Manual inspection required.")
            if self.status(previous)["status"] not in {"completed", "incomplete", "failed"}:
                raise voice.Rejected("Previous launch active or uncertain. Manual inspection required.")
        if self.busy():
            raise voice.Rejected("Browser/profile busy. Stop its owner manually before confirming.")
        with voice.locked(self.root / "workflow.lock"):
            saved = voice.read_json(directory / "plan.json")
            if saved["expires"] < time.time():
                return self.status(token)
            voice.plan_schema().model_validate(saved["plan"])
            if voice.saved_items(self.db) != saved["items"]:
                raise voice.Rejected("Saved list changed since preview. Dictate again.")
            voice.write_json(active, {"token": token})
            voice.write_json(directory / "started.json", {"started": time.time()})
        # The worker needs workflow.lock itself. The caller retains gateway.lock
        # across reservation and launch; a lost launch response is never retried.
        try:
            self.children[token] = subprocess.Popen(
                [sys.executable, str(PROJECT / "voice_shortcut.py"), "worker", token],
                cwd=PROJECT, env={**os.environ, "SHOPPING_DB": str(self.db)},
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
        except Exception:
            return self.status(token)
        return {"token": token, "status": "running", "message": "Confirmed run launched. Poll status."}

    def handle(self, mode, value=None):
        with voice.locked(self.root / "gateway.lock"):
            if mode == "preview":
                with voice.locked(self.root / "workflow.lock"):
                    return voice.preview(self.db, value, include_shop=True)
            if mode == "list":
                items = voice.saved_items(self.db)
                return {"status": "list", "items": items, "message": voice.list_message(items)}
            if mode == "confirm":
                return self.confirm(value)
            return self.status(value)

    async def cleanup(self, app):
        self.stopping = True
        async with self.serial:
            children = [child for child in self.children.values() if child.poll() is None]
            for child in children:
                with contextlib.suppress(ProcessLookupError):
                    child.send_signal(signal.SIGTERM)
            # Worker forwards SIGTERM and waits for shopping.py browser cleanup.
            await asyncio.gather(*(asyncio.to_thread(child.wait) for child in children))


def create_app(db=None, token=None):
    token = os.environ.get("VOICE_BRIDGE_TOKEN", "") if token is None else token
    if not isinstance(token, str) or len(token) < 32 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError("VOICE_BRIDGE_TOKEN must contain at least 32 printable non-space ASCII characters.")
    bridge = Bridge(db or os.environ.get("SHOPPING_DB", str(PROJECT / "shopping.db")))

    @web.middleware
    async def authenticated(request, handler):
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
            return web.json_response({"status": "rejected", "message": "Unauthorized."}, status=401)
        try:
            return await handler(request)
        except web.HTTPException as exc:
            return web.json_response({"status": "rejected", "message": "Invalid request."}, status=exc.status)
        except voice.Rejected as exc:
            return web.json_response({"status": "rejected", "message": str(exc)}, status=409)
        except Exception:
            return web.json_response({"status": "failed", "message": "Service failed. No automatic retry."}, status=500)

    async def endpoint(request):
        mode = request.match_info.get("mode", "status")
        value = None
        if request.method == "POST":
            try:
                if request.content_type != "application/json":
                    raise ValueError()
                data = await request.json()
                field = "transcript" if mode == "preview" else "token"
                if not isinstance(data, dict) or set(data) != {field} or not isinstance(data[field], str):
                    raise ValueError()
                value = data[field]
                if mode == "preview":
                    if (not value.strip() or len(value.encode()) > 4096
                            or any(ord(c) < 32 and c not in "\n\t" for c in value)):
                        raise ValueError()
            except (ValueError, UnicodeError):
                raise web.HTTPBadRequest() from None
        if mode in {"status", "confirm"}:
            value = request.match_info.get("token", value)
            if not re.fullmatch(voice.TOKEN, value or ""):
                raise web.HTTPBadRequest()
        async with bridge.serial:
            if bridge.stopping:
                raise web.HTTPServiceUnavailable()
            response = await asyncio.to_thread(bridge.handle, mode, value)
        return web.json_response(response)

    app = web.Application(middlewares=[authenticated], client_max_size=8192)
    app[BRIDGE] = bridge
    app.router.add_post("/{mode:preview|confirm}", endpoint)
    app.router.add_get("/{mode:list}", endpoint)
    app.router.add_get("/status/{token}", endpoint)
    app.on_shutdown.append(bridge.cleanup)
    return app


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535.")
    try:
        app = create_app()
    except ValueError as exc:
        parser.error(str(exc))
    web.run_app(app, host="127.0.0.1", port=args.port, access_log=None, print=None)


if __name__ == "__main__":
    main()
