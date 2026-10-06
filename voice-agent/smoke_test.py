"""Local Worker HTTP smoke; no browser, providers, shopping DB, or live cart.

Run: python3 voice-agent/smoke_test.py
"""

import concurrent.futures
import copy
import http.client
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT = Path(__file__).resolve().parent
SAVED = [
    {"name": "Milk", "quantity": 2, "max_price": 7.5,
     "preferred_name": "Fresh whole milk 1L", "brand": "Saved brand", "sku": "mock-milk",
     "alternatives": [{"name": "Approved milk", "brand": "Alternative brand",
                       "sku": "mock-alternative", "package_size": "1L", "max_price": 7}]},
    {"name": "Bread", "quantity": 1, "max_price": 6,
     "preferred_name": "Whole wheat bread", "brand": "Saved bakery", "sku": "mock-bread",
     "alternatives": []},
]
MERGED = copy.deepcopy(SAVED) + [
    {"name": "Eggs", "quantity": 1, "max_price": 12, "preferred_name": None,
     "brand": None, "sku": None, "alternatives": []},
]
MERGED[0]["quantity"] = 3
ACTIONS = [
    {"kind": "set", "name": "Milk", "quantity": 3, "max_price": 7.5, "location": None},
    {"kind": "set", "name": "Eggs", "quantity": 1, "max_price": 12, "location": None},
    {"kind": "shop", "name": None, "quantity": None, "max_price": None, "location": None},
]


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class Bridge(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, token):
        super().__init__(("127.0.0.1", 0), Handler)
        self.token = token
        self.lock = threading.Lock()
        self.previews = {}
        self.transcripts = []
        self.confirm_count = 0
        self.status_tokens = []
        self.unexpected = 0
        self.saved = copy.deepcopy(SAVED)
        self.run_token = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.handle_bridge()

    def do_POST(self):
        self.handle_bridge()

    def handle_bridge(self):
        bridge = self.server
        code = 200
        data = {"message": "Unexpected mock request"}
        with bridge.lock:
            if self.headers.get("Authorization") != "Bearer " + bridge.token:
                bridge.unexpected += 1
                code = 401
            elif self.command == "GET" and self.path == "/list":
                data = {"items": bridge.saved}
            elif self.command == "POST" and self.path in ("/preview", "/confirm"):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                if self.path == "/preview":
                    bridge.transcripts.append(body["transcript"])
                    if body["transcript"] == "clarify-test":
                        data = {"status": "clarification", "message": "Which saved item should be removed?"}
                    else:
                        token = secrets.token_hex(16)
                        data = {"status": "preview", "token": token, "items": MERGED,
                                "actions": ACTIONS, "message": "Review merged groceries; nothing saved yet.",
                                "expires_in": 300}
                        bridge.previews[token] = copy.deepcopy(data)
                elif body.get("token") in bridge.previews and bridge.run_token is None:
                    bridge.confirm_count += 1
                    bridge.run_token = body["token"]
                    bridge.saved = copy.deepcopy(bridge.previews[bridge.run_token]["items"])
                    data = {"status": "running", "message": "Mock cart preparation running."}
                else:
                    bridge.unexpected += 1
                    code = 409
            elif self.command == "GET" and self.path == "/status/" + str(bridge.run_token):
                bridge.status_tokens.append(bridge.run_token)
                data = {"status": "completed", "message": "Mock terminal result; no cart touched."}
            else:
                bridge.unexpected += 1
                code = 404
            raw = json.dumps(data).encode()
        # Keep bridge I/O in flight so concurrent Worker approval exercises its reservation.
        if self.path == "/confirm" and code == 200:
            time.sleep(0.3)
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def stop_group(process):
    # Kill descendants too, even if the Wrangler parent already exited.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


def checks(port, bridge):
    origin = f"http://localhost:{port}"
    cookie = ""

    def request(path, body=None, *, authenticated=True, supplied_origin=origin, extra=None):
        headers = dict(extra or {})
        if supplied_origin is not None:
            headers["Origin"] = supplied_origin
        if authenticated and cookie:
            headers["Cookie"] = cookie
        raw = None if body is None else json.dumps(body)
        if raw is not None:
            headers["Content-Type"] = "application/json"
        # Direct stdlib connection ignores system HTTP proxy settings.
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        headers["Host"] = f"localhost:{port}"
        try:
            conn.request("GET" if body is None else "POST", path, raw, headers)
            response = conn.getresponse()
            payload = response.read()
            data = json.loads(payload) if response.getheader("Content-Type", "").startswith("application/json") else payload.decode()
            return response.status, dict(response.getheaders()), data
        finally:
            conn.close()

    def api(path, body=None, status=200):
        code, headers, data = request(path, body)
        require(code == status, f"{path}: expected HTTP {status}, got {code}")
        if path != "/api/config":
            require(headers.get("Cache-Control") == "no-store", f"{path}: missing no-store")
        return data

    for path in ("/api/state", "/api/list", "/api/config", "/agents/shopping-agent/local"):
        require(request(path, authenticated=False)[0] == 401, "Unauthenticated route accepted")
    require(request("/api/confirm", {"token": "0" * 32}, authenticated=False)[0] == 401,
            "Unauthenticated approval accepted")
    require(request("/session", {}, supplied_origin="http://other.invalid")[0] == 403,
            "Cross-origin bootstrap accepted")
    require(request("/session", {}, supplied_origin=None)[0] == 403, "Originless bootstrap accepted")
    code, headers, data = request("/session", {}, authenticated=False)
    require(code == 200 and data == {"authenticated": True}, "Session bootstrap failed")
    session = headers.get("Set-Cookie", "")
    require(all(flag in session for flag in ("HttpOnly", "SameSite=Strict", "Path=/")), "Cookie protections missing")
    cookie = session.split(";", 1)[0]
    websocket = {"Upgrade": "websocket", "Connection": "Upgrade",
                 "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="}
    for supplied in ("http://other.invalid", None):
        require(request("/agents/shopping-agent/local", supplied_origin=supplied, extra=websocket)[0] == 403,
                "Cross-origin or originless WebSocket accepted")
    require(request("/agents/shopping-agent/local", authenticated=False, extra=websocket)[0] == 401,
            "Unauthenticated WebSocket accepted")
    require(request("/api/state", supplied_origin="http://other.invalid")[0] == 403,
            "Cross-origin API accepted")
    for path in ("/agents/shopping-agent/local", "/agents/shopping-agent/local/chat",
                 "/agents/shopping-agent/other"):
        for body in (None, {}):
            require(request(path, body)[0] == 404, "Generic agent HTTP route accepted")
    require(api("/api/config") == {"speechConfigured": False, "modelConfigured": False,
                                   "bridgeConfigured": True}, "Provider isolation failed")
    require(api("/api/state")["run"] is None, "Worker reused previous state")
    require(api("/api/list")["items"] == SAVED, "Saved preferences/caps lost")
    require(request("/")[0] == 200, "Built frontend not served")
    print("PASS session, origin/WebSocket guards, generic agent routes, isolated providers, frontend HTTP")

    draft = "Milk 3 units, eggs 1 unit under 12 SAR; prepare cart."
    state = api("/api/preview", {"transcript": draft})
    preview = state["preview"]
    require(preview and preview["status"] == "preview" and preview["items"] == MERGED
            and preview["actions"] == ACTIONS and preview["expiresAt"] > time.time() * 1000,
            "Structured merged preview lost fields or expired")
    require(state["draft"] == draft and not state["busy"] and state["error"] is None,
            "Preview state inconsistent")
    require(api("/api/list")["items"] == SAVED, "Preview mutated saved list")
    rejected = api("/api/confirm", {"token": "0" * 32}, status=409)
    require(rejected["run"] is None and rejected["preview"]["token"] == preview["token"]
            and bridge.confirm_count == 0, "Wrong token launched or consumed preview")
    state = api("/api/invalidate", {"speech": True})
    require(state["preview"] is None and state["draft"] == draft, "Speech invalidation erased draft")
    api("/api/confirm", {"token": preview["token"]}, status=409)
    edited = draft + " Keep saved bread."
    state = api("/api/invalidate", {"transcript": edited})
    require(state["draft"] == edited and state["preview"] is None, "Manual edit not retained")
    fresh = api("/api/preview", {"transcript": edited})["preview"]
    require(fresh["token"] != preview["token"] and fresh["items"] == MERGED, "Fresh preview incorrect")
    api("/api/confirm", {"token": preview["token"]}, status=409)
    require(bridge.confirm_count == 0, "Invalidated token reached bridge confirmation")
    print("PASS structured merged preview with preferences/caps, wrong/stale tokens, speech/manual invalidation")

    barrier = threading.Barrier(2)

    def approve():
        barrier.wait(timeout=5)
        return request("/api/confirm", {"token": fresh["token"]})

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(approve) for _ in range(2)]
        responses = [future.result(timeout=20) for future in futures]
    require(sorted(result[0] for result in responses) == [200, 409], "Concurrent approval was not reserved once")
    accepted = next(result[2] for result in responses if result[0] == 200)
    require(accepted["run"]["token"] == fresh["token"] and accepted["run"]["status"] == "running"
            and accepted["preview"] is None and accepted["draft"] == "" and not accepted["busy"],
            "Approval state incorrect")
    require(bridge.confirm_count == 1, "Backend confirm count != 1")
    api("/api/confirm", {"token": fresh["token"]}, status=409)
    state = api("/api/status", {})
    require(state["run"] == {"token": fresh["token"], "status": "completed",
                             "message": "Mock terminal result; no cart touched."}, "Terminal status not received")
    require(api("/api/state")["run"] == state["run"], "Terminal state not persisted")
    api("/api/confirm", {"token": fresh["token"]}, status=409)
    require(api("/api/list")["items"] == MERGED, "Approved merged items not returned")
    require(bridge.confirm_count == 1 and bridge.status_tokens == [fresh["token"]]
            and bridge.transcripts == [draft, edited] and bridge.unexpected == 0,
            "Unexpected bridge calls or duplicate launch")
    print("PASS duplicate concurrent approval: backend confirm_count=1; terminal status and merged list")

    from websockets.sync.client import connect
    with connect(f"ws://localhost:{port}/agents/shopping-agent/local", origin=origin,
                 additional_headers={"Cookie": cookie}, open_timeout=10) as socket:
        socket.send(json.dumps({"type": "cf_agent_state", "state": {**state, "draft": "forged", "busy": True}}))
        time.sleep(0.1)
        require(api("/api/state")["draft"] != "forged", "Client overwrote server-owned state")
        socket.send(json.dumps({"type": "text_message", "text": "Eggs one pack"}))
        deadline = time.monotonic() + 15
        received = None
        while time.monotonic() < deadline:
            message = json.loads(socket.recv(timeout=10))
            if message.get("type") == "shopping_state" and message["state"].get("preview") and not message["state"]["busy"]:
                received = message["state"]
                break
        require(received and received["draft"] == "Eggs one pack", "SDK voice text turn did not produce a preview")
        require(bridge.confirm_count == 1, "Voice text turn launched shopping")
    print("PASS actual SDK voice text-turn protocol and client state-write rejection; no speech providers called")
    question = api("/api/preview", {"transcript": "clarify-test"})
    require(question["clarification"] == "Which saved item should be removed?" and question["error"] is None
            and question["preview"] is None, "Clarification was hidden as a technical error")
    require(bridge.confirm_count == 1, "Clarification launched a run")
    print("PASS clarification remains visible and cannot execute")


def main():
    with tempfile.TemporaryDirectory(prefix="shopping-voice-smoke-") as directory:
        temp = Path(directory)
        # No inherited credentials, repo .env/.dev.vars, Wrangler login, or persistent state.
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": directory,
               "XDG_CONFIG_HOME": directory, "TMPDIR": directory, "CI": "true",
               "WRANGLER_SEND_METRICS": "false", "CLOUDFLARE_LOAD_DEV_VARS_FROM_DOT_ENV": "false"}
        with (temp / "build.log").open("wb") as log:
            build = subprocess.Popen(["npm", "run", "build"], cwd=ROOT, env=env,
                                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                require(build.wait(timeout=120) == 0, "Frontend npm build failed (logs withheld)")
            finally:
                stop_group(build)
        print("PASS npm run build")
        source = json.loads((ROOT / "wrangler.jsonc").read_text())
        config = {key: source[key] for key in ("compatibility_date", "compatibility_flags",
                                             "durable_objects", "migrations")}
        require(config["durable_objects"] == {"bindings": [{"name": "ShoppingAgent", "class_name": "ShoppingAgent"}]},
                "Unexpected Worker bindings; review isolation before running")
        config.update(name="shopping-voice-smoke", main=str(ROOT / "src/server.ts"),
                      assets={"directory": str(ROOT / "dist"), "binding": "ASSETS", "run_worker_first": True},
                      vars={"OPENAI_BASE_URL": "http://127.0.0.1:1/v1", "OPENAI_MODEL": "smoke-only",
                            "ELEVENLABS_VOICE_ID": "smoke-only", "OPENAI_API_KEY": "", "ELEVENLABS_API_KEY": ""})
        config_path = temp / "wrangler.json"
        config_path.write_text(json.dumps(config))
        (temp / ".dev.vars").write_text("")
        bridge = Bridge(secrets.token_hex(32))
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        worker = None
        try:
            port = free_port()
            args = [str(ROOT / "node_modules/.bin/wrangler"), "dev", "--local", "--config", str(config_path),
                    "--ip", "127.0.0.1", "--port", str(port), "--inspector-port", "0",
                    "--persist-to", str(temp / "state"), "--log-level", "error",
                    "--var", f"LOCAL_ORIGIN:http://localhost:{port}",
                    "--var", f"VOICE_BRIDGE_URL:http://127.0.0.1:{bridge.server_port}",
                    "--var", "LOCAL_SESSION_SECRET:" + secrets.token_hex(32),
                    "--var", "VOICE_BRIDGE_TOKEN:" + bridge.token,
                    "--var", "OPENAI_API_KEY:", "--var", "ELEVENLABS_API_KEY:"]
            with (temp / "worker.log").open("wb") as log:
                worker = subprocess.Popen(args, cwd=temp, env=env, stdin=subprocess.DEVNULL,
                                          stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                deadline = time.monotonic() + 60
                while True:
                    require(worker.poll() is None, "Wrangler exited during startup (logs withheld)")
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                    try:
                        conn.request("GET", "/", headers={"Host": f"localhost:{port}"})
                        response = conn.getresponse()
                        response.read()
                        if response.status == 200:
                            break
                    except (OSError, http.client.HTTPException):
                        pass
                    finally:
                        conn.close()
                    require(time.monotonic() < deadline, "Wrangler startup exceeded 60 seconds (logs withheld)")
                    time.sleep(0.2)
                checks(port, bridge)
        finally:
            if worker is not None:
                stop_group(worker)
            bridge.shutdown()
            bridge.server_close()
            thread.join(timeout=5)
    print("PASS cleanup: Worker process group and mock bridge stopped; temporary state removed")
    print("UI rendering, microphone/audio, and real provider integration not exercised.")


if __name__ == "__main__":
    main()
