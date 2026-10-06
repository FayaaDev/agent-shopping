"""Start the local Cloudflare voice page and Python bridge; never launches shop."""

import argparse
import contextlib
import json
import os
from pathlib import Path
import secrets
import shlex
import signal
import subprocess
import sys
import time
import urllib.request

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent
APP = ROOT / "voice-agent"
PID = APP / ".local.pid"
LOG = APP / ".local.log"
VARIABLES = ("LOCAL_SESSION_SECRET", "VOICE_BRIDGE_TOKEN", "OPENAI_API_KEY",
             "OPENAI_BASE_URL", "OPENAI_MODEL", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID")


def configuration():
    path = APP / ".dev.vars"
    values = {**dotenv_values(ROOT / ".env.local"), **{name: value for name, value in dotenv_values(path).items() if value}}
    values.update({name: os.environ[name] for name in VARIABLES if name in os.environ})
    for name in ("LOCAL_SESSION_SECRET", "VOICE_BRIDGE_TOKEN"):
        if not values.get(name):
            values[name] = secrets.token_hex(32)
        if len(values[name]) < 32:
            raise ValueError(f"{name} must contain at least 32 characters.")
    values.setdefault("OPENAI_BASE_URL", "https://cli.fayaa92.sa/v1")
    values.setdefault("OPENAI_MODEL", "gpt-4.1")
    values.setdefault("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
    temporary = path.with_suffix(".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        for name in VARIABLES:
            file.write(name + "=" + json.dumps(values.get(name) or "") + "\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    return {**os.environ, **{name: values[name] for name in VARIABLES if values.get(name)}}


def alive(pid):
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "args="], capture_output=True,
                                text=True, timeout=3)
        return result.returncode == 0 and any(Path(arg).name == "voice_local.py"
                                             for arg in shlex.split(result.stdout))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False


def available():
    try:
        with urllib.request.urlopen("http://localhost:8787/", timeout=1) as response:
            return response.status == 200
    except Exception:
        return False


def run():
    env = configuration()
    children = []
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        children.append(subprocess.Popen([sys.executable, str(ROOT / "voice_bridge.py")], cwd=ROOT, env=env))
        children.append(subprocess.Popen(["npm", "run", "dev"], cwd=APP, env=env, start_new_session=True))
        deadline = time.monotonic() + 90
        while not stopping and all(child.poll() is None for child in children):
            if available():
                print("Voice shopping: http://localhost:8787", flush=True)
                if not env.get("ELEVENLABS_API_KEY"):
                    print("Speech needs ELEVENLABS_API_KEY in .env.local or voice-agent/.dev.vars.", flush=True)
                break
            if time.monotonic() > deadline:
                raise RuntimeError("Local Worker startup timed out.")
            time.sleep(0.5)
        while not stopping and all(child.poll() is None for child in children):
            time.sleep(0.5)
        if not stopping:
            raise RuntimeError("A local service exited. Inspect voice-agent/.local.log.")
    finally:
        for index, child in enumerate(children):
            if child.poll() is None:
                if index == 0:
                    child.terminate()
                else:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(child.pid, signal.SIGTERM)
        for index, child in enumerate(children):
            # Bridge awaits the worker and its browser cleanup. Never kill it early.
            if index == 0:
                child.wait()
            else:
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
        PID.unlink(missing_ok=True)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    pid = int(PID.read_text()) if PID.exists() else None
    if args.stop:
        if pid and alive(pid):
            os.kill(pid, signal.SIGTERM)
            print("Shutdown requested; browser cleanup will finish before exit.")
            while alive(pid):
                time.sleep(0.2)
        else:
            print("Local voice services are not running.")
        return
    if pid and pid != os.getpid() and alive(pid):
        print("Local voice services already running: http://localhost:8787")
        return
    if not (APP / "node_modules/.bin/wrangler").exists():
        parser.error("Install dependencies first: npm --prefix voice-agent ci")
    if available():
        parser.error("Port 8787 is already serving an app. Stop its owner before starting.")
    if args.background:
        with LOG.open("ab") as log:
            child = subprocess.Popen([sys.executable, str(__file__)], cwd=ROOT, stdout=log, stderr=log, start_new_session=True)
        PID.write_text(str(child.pid))
        deadline = time.monotonic() + 90
        while child.poll() is None and time.monotonic() < deadline:
            if available():
                print("Voice shopping running at http://localhost:8787")
                print("Private startup log: voice-agent/.local.log")
                return
            time.sleep(0.5)
        if child.poll() is None:
            child.terminate()
        parser.error("Startup failed. Inspect voice-agent/.local.log.")
    else:
        PID.write_text(str(os.getpid()))
        run()


if __name__ == "__main__":
    main()
