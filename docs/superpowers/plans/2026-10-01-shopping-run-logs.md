# Shopping Run Logs Implementation Plan

**Goal:** Automatic private debugging evidence for each CLI shopping run.

**Architecture:** A focused `run_logging.py` owns per-run files, redaction, HTTP
hooks, logging handlers and retention. A ContextVar exposes the active run to
`shopping.py` without changing existing function signatures or cart logic.

**Tech stack:** Python stdlib logging/JSON/contextvars plus existing httpx.

1. Implement `RunLogs` and tests in `test_run_logging.py`: private files, events,
   HTTP completion hook before parsing, transport attempt timing, redaction,
   completion-only retention, idempotent cleanup. Test with localhost-free
   `httpx.MockTransport`, never a live model/browser.
2. Integrate `main`, `create_llm`, `write_result`, readiness/shopping callbacks and
   `run_browser_cli` in `shopping.py`: start before validation, attach after
   Browser Use logging setup, emit actions/results and phases, close HTTP client
   inside its owning event loop, finalize logs on all exits.
3. Update ignore/image exclusions and README with paths, contents, retention and
   privacy. Verify integration with mocked CLI workflow and interruptions.
4. Run `uv run --no-sync python -m unittest -q`; review only intended diff.

No live shopping test or commits required.
