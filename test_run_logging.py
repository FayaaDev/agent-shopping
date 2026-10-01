import asyncio
import io
import json
import logging
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from run_logging import CURRENT_RUN, RunLogs


class RunLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = RunLogs(Path(self.temp.name) / "nested" / "shopping.db")
        self.addCleanup(self.run.finish, "test_cleanup")

    def events(self):
        return [json.loads(line) for line in (self.run.path / "events.jsonl").read_text().splitlines()]

    def test_private_recursive_redaction_and_full_suffix(self):
        self.assertIs(CURRENT_RUN.get(), self.run)
        secret = "late-loaded-secret"
        with patch.dict(os.environ, {"OPENAI_API_KEY": secret}):
            self.run.event("sample", nested=[{"password": "private", "headers": {"X": "private"},
                                              "accessTokens": ["private"],
                                              "send_keys": {"keys": "Enter"},
                                              "content": secret + " sk-abcdef Bearer abcdef "
                                              "token=unknown-token client_secret=unknown-secret "
                                             "https://user:pass@example.com/v1?key=private "
                                             "data:image/png;base64,AAA " + "x" * 20000 + " END"}])
        text = (self.run.path / "events.jsonl").read_text()
        for value in (secret, "private", "sk-abcdef", "Bearer abcdef", "user:pass", "base64,AAA",
                      "unknown-token", "unknown-secret"):
            self.assertNotIn(value, text)
        self.assertTrue(self.events()[0]["nested"][0]["content"].endswith("x" * 20000 + " END"))
        self.assertEqual(self.events()[0]["phase"], "config")
        self.assertEqual(self.events()[0]["nested"][0]["send_keys"], {"keys": "Enter"})
        self.assertEqual(stat.S_IMODE(self.run.path.stat().st_mode), 0o700)
        self.run.finish("done")
        for name in ("events.jsonl", "run.log", ".complete"):
            self.assertEqual(stat.S_IMODE((self.run.path / name).stat().st_mode), 0o600)
        self.assertIsNone(CURRENT_RUN.get())
        before = self.events()
        self.run.finish("again")
        self.assertEqual(self.events(), before)

    def test_capture_precedes_schema_validation_and_each_retry_is_timed(self):
        class Parsed(BaseModel):
            quantity: int

        async def check():
            count = 0

            async def respond(request):
                nonlocal count
                count += 1
                await asyncio.sleep(0.002)
                return httpx.Response(429 if count == 1 else 200, json={
                    "id": "response-id", "model": "returned-model", "usage": {
                        "total_tokens": 7, "prompt_tokens_details": {"cached_tokens": 2},
                        "extra": "DO-NOT-CAPTURE",
                    },
                    "choices": [{"index": 0, "finish_reason": "stop", "message": {
                        "content": '{"quantity":1} EXTRA', "refusal": None,
                        "tool_calls": [{"secret": "DO-NOT-CAPTURE"}]}}],
                    "extra": "DO-NOT-CAPTURE",
                })

            client = self.run.make_client(transport=httpx.MockTransport(respond))
            sdk = AsyncOpenAI(api_key="test-only", base_url="https://example.com/v1",
                              http_client=client, max_retries=1)
            with patch.object(sdk, "_calculate_retry_timeout", return_value=0), \
                    self.assertRaises(ValidationError):
                await sdk.beta.chat.completions.parse(
                    model="requested-model", response_format=Parsed,
                    messages=[{"role": "user", "content": "DO-NOT-CAPTURE"}],
                )
            await client.get("https://example.com/chat/completions")
            await client.post("https://example.com/other", json={"model": "ignored"})
            await self.run.close_client()
            self.assertTrue(client.is_closed)
            await self.run.close_client()

        asyncio.run(check())
        events = self.events()
        requests = [event for event in events if event["event"] == "model_request"]
        responses = [event for event in events if event["event"] == "model_response"]
        self.assertEqual(len(requests), 2)
        self.assertEqual(len({event["call_id"] for event in requests}), 2)
        self.assertEqual([event["call_id"] for event in requests], [event["call_id"] for event in responses])
        self.assertEqual([event["status"] for event in responses], [429, 200])
        self.assertTrue(all(event["duration_ms"] >= 2 for event in responses))
        self.assertEqual(responses[1]["choices"][0]["content"], '{"quantity":1} EXTRA')
        self.assertEqual(responses[1]["model"], "returned-model")
        self.assertEqual(responses[1]["usage"], {"total_tokens": 7,
                                                "prompt_tokens_details": {"cached_tokens": 2}})
        self.assertEqual(requests[0]["response_format_type"], "json_schema")
        self.assertNotIn("DO-NOT-CAPTURE", json.dumps(events))

    def test_api_error_details_are_preserved_without_credentials(self):
        async def check():
            client = self.run.make_client(httpx.MockTransport(lambda request: httpx.Response(400, json={
                "error": {"message": "Unsupported temperature Bearer hidden", "type": "invalid_request_error",
                          "param": "temperature", "code": "unsupported_value", "headers": "DO-NOT-CAPTURE"}})))
            await client.post("https://example.com/chat/completions", json={"model": "test"})
            await self.run.close_client()
        asyncio.run(check())
        response = self.events()[-1]
        self.assertEqual(response["status"], 400)
        self.assertEqual(response["error"]["param"], "temperature")
        self.assertNotIn("hidden", json.dumps(response))
        self.assertNotIn("DO-NOT-CAPTURE", json.dumps(response))

    def test_cli_records_raw_response_actions_result_and_closes_client(self):
        from browser_use import ChatOpenAI
        from browser_use.llm.messages import UserMessage
        from browser_use.llm.exceptions import ModelProviderError
        from shopping import create_llm, log_agent_step, main, write_result

        # End the setup run so this check can assert CLI context cleanup.
        self.run.finish("test_setup")
        db = Path(self.temp.name) / "shopping.db"
        result_path = Path(self.temp.name) / "result.json"
        clients = []
        original = RunLogs.make_client

        class Parsed(BaseModel):
            quantity: int

        def make_client(run):
            client = original(run, httpx.MockTransport(lambda request: httpx.Response(200, json={
                "id": "test", "object": "chat.completion", "created": 1, "model": "test-model",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": '{"quantity":1} EXTRA'}}]})))
            clients.append(client)
            return client

        async def workflow(*args):
            llm = create_llm()
            self.assertIsInstance(llm, ChatOpenAI)
            with self.assertRaises(ModelProviderError):
                await llm.ainvoke([UserMessage(content="DO-NOT-CAPTURE")], output_format=Parsed)
            action = SimpleNamespace(model_dump=lambda **kw: {"send_keys": {"keys": "Enter"}})
            agent = SimpleNamespace(state=SimpleNamespace(n_steps=2), history=SimpleNamespace(history=[
                SimpleNamespace(model_output=SimpleNamespace(action=[action]), result=[
                    SimpleNamespace(error="evidence rejected", extracted_content=None, is_done=False, success=False)])]))
            await log_agent_step(agent)
            write_result(result_path, {"status": "no_summary", "success": False})
            return False

        with patch("sys.argv", ["shopping.py", "--db", str(db), "shop", "--result-file", str(result_path)]), \
                patch("shopping.read_items", return_value=[{"name": "yogurt"}]), \
                patch("shopping.shop", side_effect=workflow), patch("dotenv.load_dotenv"), \
                patch.dict(os.environ, {"OPENAI_API_KEY": "test-only", "OPENAI_MODEL": "test-model",
                                        "OPENAI_BASE_URL": "https://example.com/v1"}), \
                patch.object(RunLogs, "make_client", make_client), patch("builtins.print"):
            self.assertEqual(main(), 1)
        self.assertIsNone(CURRENT_RUN.get())
        self.assertTrue(clients[0].is_closed)
        result = json.loads(result_path.read_text())
        path = Path(result["run_logs"])
        events = [json.loads(line) for line in (path / "events.jsonl").read_text().splitlines()]
        response = next(row for row in events if row["event"] == "model_response")
        self.assertEqual(response["choices"][0]["content"], '{"quantity":1} EXTRA')
        step = next(row for row in events if row["event"] == "agent_step")
        self.assertEqual(step["actions"], [{"send_keys": {"keys": "Enter"}}])
        self.assertEqual(step["results"][0]["error"], "evidence rejected")
        self.assertEqual(events[-1]["event"], "run_end")
        self.assertIs(events[-1]["result"]["success"], False)
        self.assertEqual(result["run_id"], path.name)
        self.assertNotIn("test-only", (path / "events.jsonl").read_text())

    def test_cli_interrupt_is_finalized(self):
        from shopping import main
        self.run.finish("test_setup")
        db = Path(self.temp.name) / "shopping.db"
        with patch("sys.argv", ["shopping.py", "--db", str(db), "shop"]), \
                patch("shopping.read_items", side_effect=KeyboardInterrupt), patch("builtins.print"):
            with self.assertRaises(KeyboardInterrupt):
                main()
        self.assertIsNone(CURRENT_RUN.get())
        path = next((db.parent / "shopping-runs").iterdir())
        events = [json.loads(line) for line in (path / "events.jsonl").read_text().splitlines()]
        self.assertEqual(events[-1]["status"], "interrupted")
        self.assertTrue((path / ".complete").exists())

    def test_transport_errors_and_interruption(self):
        async def check():
            for exception in (httpx.ConnectError("failed https://u:p@example.com/?token=hidden"),
                              asyncio.CancelledError("interrupted sk-secret")):
                async def fail(request):
                    raise exception

                client = self.run.make_client(httpx.MockTransport(fail))
                with self.assertRaises(type(exception)):
                    await client.post("https://example.com/chat/completions", json={"model": "test"})
            await self.run.close_client()

        asyncio.run(check())
        errors = [event for event in self.events() if event["event"] == "model_error"]
        self.assertEqual([event["exception_class"] for event in errors], ["ConnectError", "CancelledError"])
        self.assertTrue(all(event["duration_ms"] >= 0 for event in errors))
        self.assertNotIn("hidden", json.dumps(errors))
        self.assertNotIn("u:p", json.dumps(errors))
        self.assertNotIn("sk-secret", json.dumps(errors))

    def test_logging_reattach_filters_traceback_and_restore(self):
        loggers = [logging.getLogger(name) for name in ("", "browser_use", "bubus", "shopping")]
        shopping_level = loggers[-1].level
        self.addCleanup(loggers[-1].setLevel, shopping_level)
        loggers[-1].setLevel(logging.INFO)
        levels = [logger.level for logger in loggers]
        handlers = [list(logger.handlers) for logger in loggers]
        console = logging.StreamHandler(io.StringIO())
        console.setLevel(logging.NOTSET)
        loggers[0].addHandler(console)
        self.addCleanup(loggers[0].removeHandler, console)
        self.run.attach_logging()
        self.run.attach_logging()
        logging.getLogger("shopping").debug("application debug")
        agent = logging.getLogger("browser_use.agent.service")
        agent.info("agent normal")
        agent.debug("unsafe debug")
        logging.getLogger("httpx").warning("network raw")
        logging.getLogger("browser_use.browser").warning("browser raw")
        agent.info("headers: {'Cookie': 'private'}")
        agent.info('dump {"headers": {"X-Private": "private"}}')
        agent.info("image_data=" + "A" * 1000)
        agent.info("data:image/png;base64,AAAA")
        try:
            raise ValueError("Bearer abcdef " + "z" * 10000 + " END")
        except ValueError:
            logging.getLogger("shopping").exception("failed sk-secret")
        loggers[0].removeHandler(self.run._handler)
        self.run.attach_logging()
        logging.getLogger("shopping").debug("after root reconfiguration")
        self.assertEqual(console.level, logging.NOTSET)
        self.assertNotIn("application debug", console.stream.getvalue())
        self.assertNotIn("after root reconfiguration", console.stream.getvalue())
        self.run.finish("done")
        self.assertEqual(console.filters, [])
        text = (self.run.path / "run.log").read_text()
        self.assertEqual(text.count("agent normal"), 1)
        for value in ("application debug", "Traceback", "ValueError", "z" * 10000 + " END",
                      "after root reconfiguration"):
            self.assertIn(value, text)
        for value in ("unsafe debug", "network raw", "browser raw", "private", "image_data",
                      "base64", "abcdef", "sk-secret"):
            self.assertNotIn(value, text)
        self.assertEqual([logger.level for logger in loggers], levels)
        for logger, original in zip(loggers, handlers):
            self.assertEqual(logger.handlers, original + ([console] if logger is loggers[0] else []))

    def test_write_failure_surfaces_and_finish_cleans_up(self):
        self.run.attach_logging()
        root = logging.getLogger()
        original_level = self.run._levels[root]
        self.run._log.close()
        with self.assertRaises(ValueError):
            logging.getLogger("shopping").debug("cannot write")
        self.run._events.close()
        with self.assertRaises(ValueError):
            self.run.finish("failed")
        self.assertNotIn(self.run._handler, root.handlers)
        self.assertEqual(root.level, original_level)
        self.assertIsNone(CURRENT_RUN.get())
        self.assertFalse((self.run.path / ".complete").exists())
        self.run.finish("again")

    def test_retention_preserves_active_unowned_and_symlinks(self):
        parent = self.run.path.parent
        active = parent / ("20000101T000000000000Z-" + "a" * 32)
        active.mkdir()
        unowned = parent / "unrelated"
        unowned.mkdir()
        (unowned / ".complete").touch()
        link = parent / ("20000101T000000000000Z-" + "b" * 32)
        link.symlink_to(unowned, target_is_directory=True)
        for number in range(25):
            path = parent / (f"20000101T000000{number:06d}Z-" + f"{number:032x}")
            path.mkdir()
            (path / ".complete").touch()
        self.run.finish("done")
        self.assertTrue(active.is_dir())
        self.assertTrue(unowned.is_dir())
        self.assertTrue(link.is_symlink())
        completed = [path for path in parent.iterdir() if path != unowned and not path.is_symlink()
                     and (path / ".complete").exists()]
        self.assertEqual(len(completed), 20)
        self.assertIn(self.run.path, completed)
        self.assertFalse((parent / ("20000101T000000000000Z-" + "0" * 32)).exists())


if __name__ == "__main__":
    unittest.main()
