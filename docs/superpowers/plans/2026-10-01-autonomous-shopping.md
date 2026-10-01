# Autonomous Shopping Implementation Plan

> **For agentic workers:** Use subagent-driven-development or inline execution for these tasks; preserve existing uncommitted changes.

**Goal:** Implement the committed autonomous-shopping design with pre-add validation, bounded item recovery, attempt evidence, confirmed learning, and sanitized Telegram results.

**Architecture:** Keep browser orchestration and SQLite ownership in `shopping.py`. Validate one item-outcome contract before product-plus clicks, during assessment, and before purchase-confirmation promotion. Keep attempt evidence in existing summary JSON; use the existing alternatives table for confirmed learning.

**Tech Stack:** Python, Browser Use, Pydantic, SQLite, unittest.

---

### Task 1: Selection policy and evidence

**Files:** `shopping.py`, `test_shopping.py`.

- [x] Add observed product and item-outcome schemas. Require requested type, normalized package coverage, selected product, observed candidates and comparison, availability of exact/approved options, and brand evidence for automatic selections.
- [x] Validate exact/SKU and approved choices first; validate automatic type, closest sufficient size, brand, explicit cap or Decimal-based 115% comparison cap before clicking.
- [x] Require every requested item to have one outcome; assess final quantity and observed price against that evidence. Save validated outcomes and safe unresolved reasons in attempt JSON.
- [x] Cover policy acceptance, mismatched/ambiguous types, undersized packages, preferred-brand priority, invalid numbers, missing evidence, and both caps.

### Task 2: Recovery and cart integration

**Files:** `shopping.py`, `test_shopping.py`.

- [x] Track active item and a per-item set of at most three distinct recovery actions. Use previous executed action results to detect failures; reject unchanged failed clicks and bypassed recovery actions.
- [x] Convert exhausted recovery into an unresolved-item tool result, allowing the same agent to continue later items. Retain run-level unsafe-action and product-plus stops.
- [x] Pass validated outcome evidence to `add_product_plus`; retain its exact selector and single-match requirement. Reconcile final automatic outcomes with pre-add evidence.
- [x] Test a mocked agent recovering one failed item, exhausting its budget, then processing a later item; test rejected evidence causes no click.

### Task 3: Confirmed learning

**Files:** `shopping.py`, `test_shopping.py`.

- [x] Save the requested-item snapshot with each attempt. In one SQLite transaction, mark an existing attempt confirmed and promote only valid, cart-observed automatic outcomes from that saved attempt.
- [x] Use existing alternative uniqueness for idempotency; repeated confirmation leaves the original purchase timestamp intact. Ignore removed items and reject missing attempts.
- [x] Test no promotion while saving, confirmed SKU promotion, repeat confirmation, malformed evidence, and independent attempts.

### Task 4: Telegram and documentation

**Files:** `telegram_bot.py`, `test_telegram_bot.py`, `README.md`.

- [x] Display automatic substitutions with package and observed price; render unresolved reasons through an allowlist. Keep malformed-result fallback and private diagnostics.
- [x] Require manual checkout approval in every result and keep no automatic retry.
- [x] Document selection order, price/package rules, recovery stops, and confirmation-scoped learning.

### Verification

- [x] Run `uv run --no-sync python -m unittest -q test_shopping test_telegram_bot`.
- [x] Run `uv run --no-sync python -m unittest -q` and `git diff --check`.
- [x] Review incremental edits against the supplied spec. No live shopping verification.

Verification: 60 tests run, 1 skipped; suite passed. Real Browser Use tool registration and dispatch exercised with mocked browser operations.
