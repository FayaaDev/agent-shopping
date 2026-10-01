# Flexible Product Matching Implementation Plan

**Goal:** Implement the approved flexible-matching policy without replacing existing dirty work.

**Architecture:** Retain `selection_reason` as the shared validation boundary and
existing outcome modes. Extend observed product evidence, condition only genuinely
optional criteria, and reuse candidate evidence for final aggregate cart counting.

**Tech stack:** Python, SQLite, Pydantic, unittest; existing Browser Use/Telegram integration.

- [x] Reproduce generic Greek yogurt rejection in `test_shopping.py` before changing validation.
- [x] Update `shopping.py` validation, product schema, agent instructions and final matching;
  retain recorded-selection equality, add guards and purchase revalidation.
- [x] Update `telegram_bot.py` optional-evidence validation and purchasable-unit wording.
- [x] Exercise existing-cart acceptance, deficit-only additions, changed evidence rejection,
  mixed products, explicit requirements/caps, confirmation and Telegram in existing test files.
- [x] Update `README.md`, `AGENTS.md`, and the approved design spec.
- [x] Run `uv run --no-sync python -m unittest -q` (67 tests, 1 skipped) and
  `git diff --check` (clean); inspect scope against the initial dirty-file inventory.

Execute inline; no subagent tool is available. No commits, remote work or live shopping.
