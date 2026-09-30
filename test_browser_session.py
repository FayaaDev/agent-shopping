"""Opt-in browser restart check using only a local server and temporary profile."""

import asyncio
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import shopping
from shopping import create_browser, start_browser


@unittest.skipUnless(os.getenv("TEST_BROWSER_SESSION") == "1", "Opt-in local browser check")
class BrowserSessionTest(unittest.TestCase):
    def test_auth_storage_survives_restart(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body>Local session check</body></html>")

            def log_message(self, *args):
                pass

        async def restart_check(url):
            browser = create_browser()
            browser.browser_profile.headless = True
            try:
                await start_browser(browser, url)
                page = await browser.get_current_page()
                await page.evaluate("""() => {
                    document.cookie = 'test_session=present; path=/';
                    document.cookie = 'test_persistent=present; path=/; max-age=3600';
                    localStorage.setItem('test_local', 'present');
                    sessionStorage.setItem('test_auth', 'present');
                }""")
            finally:
                await browser.kill()
            snapshot_path = shopping.PROFILE / "storage-state.json"
            original_snapshot = snapshot_path.read_text()
            browser = create_browser()
            browser.browser_profile.headless = True
            try:
                await start_browser(browser, url)
                page = await browser.get_current_page()
                raw = await page.evaluate("""() => JSON.stringify({
                    cookie: document.cookie.includes('test_session=present'),
                    local: localStorage.getItem('test_local') === 'present',
                    session: sessionStorage.getItem('test_auth') === 'present'
                })""")
                self.assertEqual(json.loads(raw), {"cookie": True, "local": True, "session": True})
                await page.evaluate("""() => {
                    document.cookie = 'test_persistent=newer; path=/; max-age=3600';
                    localStorage.setItem('test_local', 'newer');
                    sessionStorage.setItem('test_auth', 'newer');
                }""")
            finally:
                await browser.kill()
            latest_snapshot = json.loads(snapshot_path.read_text())
            self.assertTrue(any(cookie["name"] == "test_persistent" and cookie["value"] == "newer"
                                for cookie in latest_snapshot["cookies"]))
            # A stale snapshot wins over newer matching values in the persistent profile.
            snapshot_path.write_text(original_snapshot)
            browser = create_browser()
            browser.browser_profile.headless = True
            try:
                await start_browser(browser, url)
                page = await browser.get_current_page()
                raw = await page.evaluate("""() => JSON.stringify({
                    cookie: document.cookie.includes('test_persistent=present'),
                    local: localStorage.getItem('test_local') === 'present',
                    session: sessionStorage.getItem('test_auth') === 'present'
                })""")
                self.assertEqual(json.loads(raw), {"cookie": True, "local": True, "session": True})
            finally:
                await browser.kill()

        with tempfile.TemporaryDirectory() as directory, \
                patch("shopping.PROFILE", (Path(directory) / "profile").resolve()), \
                ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                asyncio.run(restart_check(f"http://127.0.0.1:{server.server_port}/"))
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    unittest.main()
