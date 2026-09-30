# Grocery cart assistant

Tamimi cart preparation using one persistent Browser Use profile. The shopping
list, preferred products/SKUs, approved alternatives, price caps, observed
prices, and purchase confirmations live in `shopping.db` (ignored by Git).

```sh
uv run python shopping.py add "milk 1 L" 2
uv run python shopping.py list
uv run python shopping.py prefer "milk 1 L" "Almarai Fresh Milk Full Fat - 1L" --sku 1000971 --brand Almarai --max-price 9
uv run python shopping.py allow "milk 1 L" "Nadec Fresh Milk Full Fat - 1L"
uv run python shopping.py setup                       # manual login + address, once
uv run python shopping.py shop
```

`shopping.db` is the sole source of truth. Use `add` to create list items or
update their quantities, and `list` to view them.
`add` sets the target quantity, including units already in the cart; `remove`
deletes an item from the list, not the retailer's cart. Run
`uv run python shopping.py clear` to empty the saved list, including its product
preferences and approved alternatives. Purchase history, observed prices, and
the retailer's cart are preserved. `prefer` sets an exact
preferred product and optional unit-price cap. An approved alternative is used
only if the preferred product is unavailable. With two or more approved
alternatives and `TYPESAFE_API_KEY` set, Jev ranks them; uncertain decisions
leave the item unresolved.

## Model configuration

Both `shop` agents use `ChatOpenAI`. Configure `.env.local` in the directory
where you run the command, or set the same variables in your environment:

```dotenv
OPENAI_API_KEY=your-key
OPENAI_BASE_URL=https://cli.fayaa92.sa/v1
OPENAI_MODEL=your-server-model-name
```

Use the model name exposed by your server. It must support images and structured
JSON output. If `OPENAI_MODEL` is unset, the default is `gpt-4.1`.
Environment variables take precedence over `.env.local`. The file is Git-ignored;
never commit or share API keys.
Requests and billing use that provider; without `OPENAI_BASE_URL`, they use
the OpenAI API. No Browser Use API key or Codex subscription adapter is required.

## Login and session persistence

Login and fulfillment settings stay in the persistent `.browser-profile`.

Run `setup` in an interactive terminal. In its browser, log in, complete any OTP,
and select your delivery address or pickup branch. Press Enter in the terminal
when ready; the browser closes and saves cookies (including session cookies),
local storage, and session storage to `.browser-profile/storage-state.json`.
Both commands restore that snapshot before opening Tamimi and update it on exit.
Browser Use's warning about passing both `storage_state` and `user_data_dir` is
expected: the snapshot takes precedence over matching profile values. Each run
updates the same snapshot, not a session archive. Avoid restoring an old snapshot
unless you intend to replace newer authentication or fulfillment state.
Profile reuse alone does not retain session cookies or session storage on restart.
If you used setup before snapshot support, run it again to capture your login.
Use this browser rather than ordinary Chrome: both `setup` and `shop` share the project's
`.browser-profile`, regardless of the working directory. Keep that directory
private; it contains authentication tokens, has owner-only directory access, and
is ignored by Git. Close other setup/shop browsers before starting.
Setup is manual and needs no model API key.

Before cart changes, `shop` opens account/fulfillment menus to verify login and
the selected location. If no location is selected, it may choose a single saved
delivery address or a unique saved location matching `--location`. Ambiguous
choices require your input. It never creates or edits addresses.
Missing or uncertain state stops shopping and asks you to rerun `setup`.
Use `shop --location "ADDRESS OR BRANCH"` to also verify the selected location
matches your request. Session expiration requires another manual login.

`shop` may change the live cart: it keeps existing items, adds missing amounts,
uses `add_product_plus` to click the exact user-identified product-page SVG +,
and selects the earliest available slot. It reports the full cart, prices,
unresolved items, slot, and attempt ID, then stops for manual checkout. After
*actually placing* the order yourself, record it with
`uv run python shopping.py confirm-purchase ATTEMPT_ID`. No purchase is inferred
from reaching the slot page.
The browser remains available for review until you press Enter in the terminal,
then closes; its profile and the retailer's cart persist.

Local checks (no retailer interaction): `uv run --no-sync python -m unittest -q`.
Browser restart regression (temporary profile, localhost only):
`TEST_BROWSER_SESSION=1 uv run --no-sync python -m unittest -q test_browser_session`.
