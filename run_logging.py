"""Private per-run logs. Call close_client() before finish()."""

import contextvars
import json
import logging
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx


CURRENT_RUN = contextvars.ContextVar("CURRENT_RUN", default=None)
_OWNED = re.compile(r"\d{8}T\d{12}Z-[0-9a-f]{32}")
_PRIVATE_FIELD = re.compile(
    r"(?:^key$|api[_-]?key|access[_-]?key)|token|password|secret|authorization|headers?|cookies?|storage|"
    r"image|screenshot|base64", re.I
)
_TOKEN_COUNTS = {"prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens",
                 "audio_tokens", "reasoning_tokens", "accepted_prediction_tokens",
                 "rejected_prediction_tokens"}
_TOKEN_DETAILS = {"prompt_tokens_details", "completion_tokens_details"}
_UNSAFE_LOG = re.compile(
    r"data:image/|<html\b|<!doctype|<body\b|"
    r"(?:headers?|cookies?|localStorage|sessionStorage|storage_state|"
    r"storage-state|screenshot|image(?:_url|_data)?|base64|html|"
    r"dom(?:_snapshot|_content)?)[\"']?\s*[:=]|"
    r"(?:^|\n)\s*(?:authorization|set-cookie|cookie)\s*:", re.I
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _url(match):
    raw = match.group(0)
    try:
        parts = urlsplit(raw)
    except ValueError:
        return "[REDACTED URL]"
    host = parts.netloc.rsplit("@", 1)[-1]
    if "@" in parts.netloc:
        host = "[REDACTED]@" + host
    query = urlencode([(key, "[REDACTED]") for key, _ in parse_qsl(parts.query)])
    return urlunsplit((parts.scheme, host, parts.path, query,
                       "[REDACTED]" if parts.fragment else ""))


def _scrub(value):
    if isinstance(value, dict):
        return {str(key): item if key in _TOKEN_COUNTS and type(item) in (int, float)
                else _scrub(item) if key in _TOKEN_DETAILS and isinstance(item, dict)
                else "[REDACTED]" if _PRIVATE_FIELD.search(str(key)) else _scrub(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(item) for item in value]
    if not isinstance(value, str):
        return value
    # Resolve environment secrets at every write, including dotenv loaded later.
    secrets = {secret for key, secret in os.environ.items()
               if re.search(r"KEY|TOKEN|PASSWORD|SECRET", key, re.I)
               and secret}
    for secret in sorted(secrets, key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"https?://[^\s<>\"']+", _url, value)
    value = re.sub(r"\bBearer\s+[^\s,;\"']+", "Bearer [REDACTED]", value, flags=re.I)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", value)
    value = re.sub(r"data:image/[^\s\"']+", "[REDACTED]", value, flags=re.I)
    value = re.sub(
        r"(?i)(\b(?:authorization|cookies?|set-cookie|[\w-]*(?:password|api[_-]?key|"
        r"token|secret)|base64|image(?:_url|_data)?|screenshot)\b[\"']?\s*[:=]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,'\";&}\]]+)",
        r"\1[REDACTED]", value,
    )
    return value


class _RunHandler(logging.Handler):
    def __init__(self, run):
        super().__init__(logging.DEBUG)
        self.run = run
        self.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"
        ))
        self.formatter.converter = time.gmtime

    def emit(self, record):
        if CURRENT_RUN.get() is not self.run:
            return
        name = record.name
        app = name == "shopping" or name.startswith("shopping.")
        browser = any(name == prefix or name.startswith(prefix + ".") for prefix in (
            "browser_use.agent", "browser_use.tools", "browser_use.service",
            "browser_use.utils",
        ))
        if not (app or browser and record.levelno >= logging.INFO):
            return
        if getattr(record, "_run_log_handler", None) is self:
            return
        record._run_log_handler = self
        text = self.format(record)
        if _UNSAFE_LOG.search(text):
            return
        # Deliberately don't use StreamHandler.emit: write failures must propagate.
        self.run._log.write(_scrub(text) + "\n")
        self.run._log.flush()


class _AttemptTransport(httpx.AsyncBaseTransport):
    def __init__(self, run, transport):
        self.run = run
        self.transport = transport

    async def handle_async_request(self, request):
        try:
            return await self.transport.handle_async_request(request)
        except BaseException as exc:
            if "run_logging" in request.extensions:
                self.run._model_error(request, exc)
            raise

    async def aclose(self):
        await self.transport.aclose()


class RunLogs:
    def __init__(self, db_path):
        parent = Path(db_path).resolve().parent / "shopping-runs"
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent.chmod(0o700)
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex
        self.path = parent / self.run_id
        self.path.mkdir(mode=0o700)
        self.path.chmod(0o700)
        self.phase = "config"
        self._finished = False
        self._clients = []
        self._levels = {}
        self._console_handlers = set()
        self._console_filter = self._original_console_level
        self._handler = _RunHandler(self)
        self._events = self._open_private("events.jsonl")
        try:
            self._log = self._open_private("run.log")
        except BaseException:
            self._events.close()
            raise
        self._token = CURRENT_RUN.set(self)

    def _open_private(self, name):
        fd = os.open(self.path / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "w", encoding="utf-8")

    def event(self, name, **data):
        if self._finished:
            raise RuntimeError("Run logs already finished")
        entry = {**_scrub(data), "timestamp": _now(), "run_id": self.run_id,
                 "phase": _scrub(self.phase), "event": _scrub(name)}
        self._events.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._events.flush()

    def attach_logging(self):
        if self._finished:
            raise RuntimeError("Run logs already finished")
        for name, level in (("", logging.DEBUG), ("browser_use", logging.INFO),
                            ("bubus", logging.INFO), ("shopping", logging.DEBUG)):
            logger = logging.getLogger(name)
            if logger not in self._levels:
                self._levels[logger] = logger.level
            for handler in logger.handlers:
                if handler is not self._handler and handler not in self._console_handlers:
                    handler.addFilter(self._console_filter)
                    self._console_handlers.add(handler)
            logger.setLevel(level)
            if name != "shopping" and self._handler not in logger.handlers:
                # Browser Use's console formatter mutates record.name in place.
                logger.handlers.insert(0, self._handler)

    def _original_console_level(self, record):
        if record.name == "shopping" or record.name.startswith("shopping."):
            return False
        logger = logging.getLogger(record.name)
        while logger is not None:
            level = self._levels.get(logger, logger.level)
            if level:
                return record.levelno >= level
            logger = logger.parent
        return True

    def make_client(self, transport=None):
        client = httpx.AsyncClient(
            transport=_AttemptTransport(self, transport if transport is not None
                                        else httpx.AsyncHTTPTransport()),
            event_hooks={"request": [self._request], "response": [self._response]},
        )
        self._clients.append(client)
        return client

    async def _request(self, request):
        if request.method != "POST" or not request.url.path.endswith("/chat/completions"):
            return
        attempt = {"call_id": uuid.uuid4().hex, "start": time.monotonic()}
        request.extensions["run_logging"] = attempt
        try:
            body = json.loads(await request.aread())
        except (ValueError, UnicodeError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        response_format = body.get("response_format")
        self.event("model_request", call_id=attempt["call_id"],
                   model=body.get("model") if isinstance(body.get("model"), str) else None,
                   response_format_type=response_format.get("type")
                   if isinstance(response_format, dict) and isinstance(response_format.get("type"), str)
                   else None)

    def _attempt_fields(self, request):
        attempt = request.extensions["run_logging"]
        return {"call_id": attempt["call_id"],
                "duration_ms": (time.monotonic() - attempt["start"]) * 1000}

    def _model_error(self, request, exc):
        self.event("model_error", **self._attempt_fields(request),
                   exception_class=type(exc).__name__, message=str(exc))

    async def _response(self, response):
        if "run_logging" not in response.request.extensions:
            return
        try:
            await response.aread()
        except BaseException as exc:
            self._model_error(response.request, exc)
            raise
        try:
            body = response.json()
        except (ValueError, UnicodeError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        choices = []
        for choice in body.get("choices", []) if isinstance(body.get("choices"), list) else []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            message = message if isinstance(message, dict) else {}
            choices.append({"index": choice.get("index") if type(choice.get("index")) is int else None,
                            "finish_reason": choice.get("finish_reason")
                            if isinstance(choice.get("finish_reason"), str) else None,
                            "content": message.get("content") if isinstance(message.get("content"), str) else None,
                            "refusal": message.get("refusal") if isinstance(message.get("refusal"), str) else None})
        usage = body.get("usage")
        usage = {
            key: value if type(value) in (int, float) else {
                count: amount for count, amount in value.items()
                if count in _TOKEN_COUNTS and type(amount) in (int, float)
            }
            for key, value in usage.items()
            if (key in _TOKEN_COUNTS and type(value) in (int, float))
            or (key in _TOKEN_DETAILS and isinstance(value, dict))
        } if isinstance(usage, dict) else None
        self.event("model_response", **self._attempt_fields(response.request),
                   status=response.status_code,
                   id=body.get("id") if isinstance(body.get("id"), str) else None,
                   model=body.get("model") if isinstance(body.get("model"), str) else None,
                    usage=usage, choices=choices,
                    error={key: body["error"].get(key) for key in ("message", "type", "param", "code")}
                    if isinstance(body.get("error"), dict) else None)

    async def close_client(self):
        try:
            for client in self._clients:
                await client.aclose()
        finally:
            self._clients = [client for client in self._clients if not client.is_closed]

    def finish(self, status, **details):
        if self._finished:
            return
        try:
            self.event("run_end", status=status, **details)
            with self._open_private(".complete"):
                pass
            completed = sorted(
                path for path in self.path.parent.iterdir()
                if _OWNED.fullmatch(path.name) and not path.is_symlink() and path.is_dir()
                and (path / ".complete").is_file() and not (path / ".complete").is_symlink()
            )
            for path in completed[:-20]:
                shutil.rmtree(path)
        finally:
            self._finished = True
            for logger, level in self._levels.items():
                logger.removeHandler(self._handler)
                logger.setLevel(level)
            for handler in self._console_handlers:
                handler.removeFilter(self._console_filter)
            self._handler.close()
            try:
                self._events.close()
            finally:
                try:
                    self._log.close()
                finally:
                    CURRENT_RUN.reset(self._token)
