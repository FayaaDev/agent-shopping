"""Synthetic shared-profile check: run seed, then restore in separate containers."""

import asyncio
import json
import logging
import os
import socket
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.metadata import version
from pathlib import Path
from threading import Thread

os.environ["ANONYMIZED_TELEMETRY"] = "false"
os.environ["BROWSER_USE_SETUP_LOGGING"] = "false"
os.environ["BROWSER_USE_HEADLESS"] = "true"
os.environ.pop("DISPLAY", None)
logging.basicConfig(level=logging.ERROR)

import psutil
import shopping

ROOT = Path("/tmp/browser-use-user-data-dir-regression")
shopping.PROFILE = ROOT / "profile"
URL = "http://127.0.0.1:8765/"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<title>Synthetic profile regression</title>")

    def log_message(self, *args):
        pass


def alive(process):
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


async def main(phase):
    assert version("browser-use") == "0.13.10", "Expected BrowserUse 0.13.10"
    assert socket.gethostname() == "shopping-browser", "Unexpected hostname"
    assert ROOT.is_mount(), "Synthetic root must be a mounted volume"
    snapshot_path = shopping.PROFILE / "storage-state.json"
    lock = shopping.PROFILE / "SingletonLock"
    if phase == "seed":
        assert not any(ROOT.iterdir()), "Seed volume must start empty"
    else:
        assert snapshot_path.is_file(), "Restore requires the seeded volume"
        assert lock.is_symlink(), "Restore must exercise a stale singleton lock"
        assert os.readlink(lock).rsplit("-", 1)[0] == "shopping-browser", "Stale lock hostname mismatch"

    browser = shopping.create_browser()
    assert browser.browser_profile.keep_alive is True
    assert browser.browser_profile.headless is True
    browser.browser_profile.enable_default_extensions = False
    browser.browser_profile.captcha_solver = False
    browser.browser_profile.allowed_domains = ["127.0.0.1"]
    processes = []
    try:
        await shopping.start_browser(browser, URL)
        process = browser._local_browser_watchdog._subprocess
        assert process is not None, "No tracked local Chromium process"
        assert Path(process.exe()).resolve() == Path("/usr/lib/chromium/chromium").resolve(), "Not native Chromium"
        processes = [process, *process.children(recursive=True)]
        assert os.readlink(lock) == f"shopping-browser-{process.pid}", "Lock owner mismatch"
        page = await browser.get_current_page()
        assert page is not None, "No localhost page"
        if phase == "seed":
            await page.evaluate("""() => {
                document.cookie = 'regression_cookie=synthetic; path=/';
                localStorage.setItem('regression_storage', 'synthetic');
            }""")
        state = json.loads(await page.evaluate("""() => ({
            cookie: document.cookie.split('; ').includes(
                'regression_cookie=synthetic'),
            storage: localStorage.getItem('regression_storage') === 'synthetic'
        })"""))
        assert state == {"cookie": True, "storage": True}, state

        # Match BrowserUse 0.13.10's keep-alive Agent.close(), without an LLM.
        await browser.event_bus.stop(clear=False, timeout=1)
        browser.event_bus.event_queue = None
        browser.event_bus._on_idle = None
        assert alive(process), "Keep-alive teardown closed Chromium"
    finally:
        await browser.kill()

    # Assert before container exit so Docker teardown cannot mask a broken kill().
    async with asyncio.timeout(10):
        while any(alive(p) for p in processes):
            await asyncio.sleep(0.1)
    # SIGTERM can leave singleton artifacts; the next same-host Chromium recovers them.
    if os.path.lexists(lock):
        assert lock.is_symlink(), "Leftover lock is not a symlink"
        assert os.readlink(lock) == f"shopping-browser-{process.pid}", "Leftover lock owner mismatch"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert any(c["name"] == "regression_cookie" and c["value"] == "synthetic"
               and c["domain"] == "127.0.0.1" and c.get("expires") in (-1, 0)
               for c in snapshot["cookies"]), "Session cookie not saved"
    assert any(o["origin"] == URL.rstrip("/") and
               {"name": "regression_storage", "value": "synthetic"} in o["localStorage"]
               for o in snapshot["origins"]), "Local storage not saved"
    print(f"PASS {phase}: hostname=shopping-browser; synthetic storage verified; "
          "keep-alive kill exited; same-host stale lock "
          + ("recovered" if phase == "restore" else "permitted"), flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"seed", "restore"}:
        raise SystemExit("Usage: python docker_profile_regression.py seed|restore")
    server = HTTPServer(("127.0.0.1", 8765), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        asyncio.run(main(sys.argv[1]))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
