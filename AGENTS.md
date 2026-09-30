# Project guidance

- This is a Python CLI for preparing a Tamimi grocery cart. `shopping.py` owns the workflow, SQLite schema, and CLI; `README.md` documents usage.
- Use the existing Browser Use session (`.browser-profile`) for shopping. Do not add Ego or replay `Shoppingsteps.json` literally; it is a navigation reference.
- Both `shop` agents use `ChatOpenAI` with `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `OPENAI_MODEL` from `.env.local` or the environment. The current OpenAI-compatible endpoint is `https://cli.fayaa92.sa/v1`; use the model name exposed by that server, with image and structured JSON output support. The default model is `gpt-4.1`. No Browser Use API key or Codex subscription adapter is required. Never expose or commit API keys.
- The persistent profile and `.browser-profile/storage-state.json` intentionally work together to retain session cookies and storage across restarts. The warning about supplying both is expected; snapshot values take precedence over matching profile values. Each normal run updates the same snapshot, not a session archive. Close other setup/shop browsers before starting.
- `shopping.db` is the sole source of truth for the active list, preferred products/SKUs, approved alternatives, price caps, observed prices, and purchase confirmations. Manage list items with the `add`, `remove`, and `list` CLI commands.
- Preserve existing cart items and add only missing quantities. Never add an unapproved substitute or exceed an item price cap. Jev may rank explicitly approved alternatives; exact known products use deterministic rules.
- The cart's checkout button may be used to select the earliest available slot. Stop there for human approval: never continue to payment, enter card details, or place an order. Record a purchase only after explicit confirmation with `confirm-purchase`.
- Run `uv run --no-sync python -m unittest -q` for local verification. Do not run `shopping.py shop` or change the live Tamimi cart as a test.
