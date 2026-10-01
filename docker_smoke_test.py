"""Localhost-only Docker packaging check: python /tmp/docker_smoke_test.py."""

import asyncio
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

# Set before importing browser-use; native headless must work without X/Xvfb.
os.environ.pop("DISPLAY", None)
os.environ["ANONYMIZED_TELEMETRY"] = "false"
os.environ["BROWSER_USE_SETUP_LOGGING"] = "false"
logging.basicConfig(level=logging.ERROR)

from browser_use import Browser
from browser_use.browser.watchdogs.local_browser_watchdog import LocalBrowserWatchdog
from shopping import PRODUCT_PLUS_SELECTOR


# Product-image control structure observed on the failed yogurt page; no retailer calls.
PRODUCT_CONTROLS = '''
<div class="ProductDetails__ImgAndCarouselDiv-sc-10zw1uf-2 bjZItJ">
  <div class="Counter__Container-sc-1nu7oer-2 gFPpDi">
    <svg id="minus"><g><circle/><rect/></g></svg>
    <span>1</span>
    <svg id="plus" class="Counter__StyledAddToCart-sc-1nu7oer-3 hIvriT" maxedout="0">
      <g><circle/><g><rect/><rect/></g></g>
    </svg>
  </div>
</div>
<svg id="unrelated" class="Counter__StyledAddToCart-sc-other hash"></svg>
'''


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        seed = self.path == "/seed"
        body = (
            "<!doctype html><title>Packaging smoke test</title>"
            + ('<script>localStorage.setItem("smoke_storage", "synthetic");</script>' if seed else "")
            + '<p id="ready">Ready</p>'
            + PRODUCT_CONTROLS
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if seed:
            # No Expires/Max-Age: intentionally a session cookie.
            self.send_header("Set-Cookie", "smoke_cookie=synthetic; Path=/; SameSite=Lax")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


async def check_browser(profile, snapshot, executable, url):
    profile.mkdir()
    assert not any(profile.iterdir()), "Profile must start empty"
    browser = Browser(
        headless=True,
        executable_path=executable,
        user_data_dir=profile,
        storage_state=snapshot,
        downloads_path=profile.parent / "downloads",
        enable_default_extensions=False,
        captcha_solver=False,
        allowed_domains=["127.0.0.1"],
    )
    try:
        await browser.start()
        # 0.13.10 launches Chromium directly; inspect its actual process flags.
        process = browser._local_browser_watchdog._subprocess
        assert process is not None, "No local browser process"
        command = process.cmdline()
        # Debian's /usr/bin/chromium wrapper execs this native ELF binary.
        expected = "/usr/lib/chromium/chromium" if sys.platform == "linux" and executable == "/usr/bin/chromium" else executable
        assert Path(command[0]).resolve() == Path(expected).resolve(), (
            f"Wrong browser binary: launcher={executable!r}, expected={expected!r}, observed command[0]={command[0]!r}"
        )
        assert any(arg == "--headless" or arg.startswith("--headless=") for arg in command), "Not native headless"
        assert "DISPLAY" not in process.environ(), "Browser inherited DISPLAY"
        assert Path(browser.browser_profile.user_data_dir).resolve() == profile.resolve(), "Browser fell back to another profile"
        await browser.navigate_to(url)
        page = await browser.get_current_page()
        assert page is not None, "No current page"
        # Actor Page.evaluate takes an arrow-function string, returns JSON text.
        async with asyncio.timeout(10):
            while True:
                state = json.loads(await page.evaluate(
                    "() => ({ready: !!document.getElementById('ready'), "
                    "cookie: document.cookie.split('; ').includes('smoke_cookie=synthetic'), "
                    "storage: localStorage.getItem('smoke_storage') === 'synthetic'})"
                ))
                if state["ready"]:
                    break
                await asyncio.sleep(0.05)
        assert state["cookie"], "Synthetic session cookie missing"
        assert state["storage"], "Synthetic localStorage missing"
        assert len(await page.get_elements_by_css_selector(PRODUCT_PLUS_SELECTOR)) == 1
        assert await page.evaluate("(selector) => document.querySelector(selector).id", PRODUCT_PLUS_SELECTOR) == "plus"
        # Hashed classes and SVG nesting must not make the plus disappear.
        await page.evaluate("""() => {
            document.getElementById('plus').className.baseVal = 'Counter__StyledAddToCart-sc-new newHash';
            document.querySelector('[class*="ProductDetails__ImgAndCarouselDiv"]').className = 'ProductDetails__ImgAndCarouselDiv-sc-new newHash';
        }""")
        assert len(await page.get_elements_by_css_selector(PRODUCT_PLUS_SELECTOR)) == 1
        await page.evaluate("() => document.getElementById('plus').after(document.getElementById('plus').cloneNode(true))")
        assert len(await page.get_elements_by_css_selector(PRODUCT_PLUS_SELECTOR)) == 2, "Duplicate plus must remain ambiguous"
        await page.evaluate("(selector) => document.querySelectorAll(selector).forEach(el => el.remove())", PRODUCT_PLUS_SELECTOR)
        assert len(await page.get_elements_by_css_selector(PRODUCT_PLUS_SELECTOR)) == 0, "Minus must never match"
    finally:
        # kill() saves storage_state while CDP is still connected.
        await browser.kill()


async def main():
    assert version("browser-use") == "0.13.10", "Expected browser-use 0.13.10"
    executable = LocalBrowserWatchdog._find_installed_browser_path()
    assert executable and Path(executable).is_file(), "Image has no installed Chromium"
    if sys.platform == "linux":
        assert executable == "/usr/bin/chromium", "Expected Docker's /usr/bin/chromium"
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin = f"http://127.0.0.1:{server.server_port}"
        # This prefix prevents 0.13.10 from copying Mac Chrome profiles elsewhere.
        with TemporaryDirectory(prefix="browser-use-user-data-dir-smoke-") as directory:
            root = Path(directory)
            snapshot = root / "storage-state.json"
            await check_browser(root / "seed-profile", snapshot, executable, origin + "/seed")
            saved = json.loads(snapshot.read_text(encoding="utf-8"))
            assert any(
                c["name"] == "smoke_cookie" and c["value"] == "synthetic"
                and c["domain"] == "127.0.0.1" and c.get("expires") in (-1, 0)
                for c in saved["cookies"]
            ), "kill() did not save the synthetic session cookie"
            assert any(
                o["origin"] == origin
                and {"name": "smoke_storage", "value": "synthetic"} in o["localStorage"]
                for o in saved["origins"]
            ), "kill() did not save localhost localStorage"
            # /check never sets either value; only snapshot import can restore them.
            await check_browser(root / "restore-profile", snapshot, executable, origin + "/check")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    print(f"PASSED: native headless ({executable}); kill snapshot; fresh-profile cookie/storage restore; product plus DOM regression")


if __name__ == "__main__":
    asyncio.run(main())
