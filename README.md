# Grocery cart assistant

Tamimi cart preparation using one persistent Browser Use profile. The shopping
list, preferred products/SKUs, approved alternatives, price caps, observed
prices, and purchase confirmations live in `shopping.db` (ignored by Git).

**Mobile web app:** manage the list with quantity buttons, prepare a cart, and
review results in your phone browser. See [web setup and deployment](docs/web-app.md).

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
preferred product and optional unit-price cap. Selection order is exact preferred
product, suitable explicitly approved alternative, then the nearest suitable
automatic selection from visible evidence. Unavailability evidence is required
only for saved preferred products/SKUs and saved alternatives. A generic request
does not require an imaginary exact product to be unavailable. With two or more approved
alternatives and `TYPESAFE_API_KEY` set, Jev ranks them; uncertain decisions
retain the saved approved alternatives in their original order.

Automatic selections match product type semantically, not by identical names.
The agent must judge all explicit attributes (including brand, flavor, fat/dietary
requirements and package) against visible evidence and record its reason and
quoted product attributes. Unspecified brand, flavor and package are flexible;
no mainstream-brand requirement applies. Saved brand requirements are mandatory.
An explicit package need uses the nearest sufficient size; absent one, no required
package quantity is invented. An explicit unit-price cap always applies; without
one, the selection must cost no more than 115% of the cheapest eligible observed
comparison (nearest sufficient size when specified). Missing or contradictory
evidence leaves the item unresolved. Code validates evidence structure/quotes,
saved brand, numeric package needs and prices; semantic interpretation is the agent's judgment.
Evidence quotes use attribute values from name, brand or package size only, without
labels, prices or SKUs. Selected product and comparison are unchanged candidate
copies; equal-price comparisons use the first eligible candidate in observed order.

Count all validated qualifying existing cart products and add only the deficit.
Quantities are purchasable units: `Greek yogurt` quantity 10 can be satisfied by
10 Nada Greek Yogurt Assorted Pack3X160G packs (30 cups). Never convert that target
to 30 cart units. Results identify the selected product, observed package and price
per purchasable unit. Recorded evidence is checked again against the final cart;
only explicit `confirm-purchase` can promote a validated automatic selection.
Cart verification uses observed SKUs/product links when available, rejecting
conflicting IDs. Otherwise it matches the full product name, tolerating whitespace,
hyphens and the recorded brand prefix. Different flavors/packages and ambiguous
candidate matches stay unresolved; prices and quantities are checked separately.

Each item gets at most three distinct recovery actions. Exhausting that budget
leaves the item unresolved and allows other items to continue. Login/fulfillment
failures, CAPTCHA, unsafe-action guards, missing/ambiguous product-plus controls,
checkout failures, and the overall step budget stop the run; cart work is never
automatically retried.
The product-image plus selector handles both the initial unclassed two-bar SVG and
the classed quantity-counter plus. It excludes mobile/card controls outside the
image section and requires exactly one match before clicking.

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
from reaching the slot page. Learning is scoped to that attempt and applied only
by explicit `confirm-purchase`; repeated confirmation is idempotent and does not
duplicate purchase history or learning. An unresolved item prevents a prepared
claim even if the producer reports success. Every shopping result requires
manual checkout approval; the assistant never pays or places an order.
The browser remains available for review until you press Enter in the terminal,
then closes; its profile and the retailer's cart persist.

Local checks (no retailer interaction): `uv run --no-sync python -m unittest -q`.
Browser restart regression (temporary profile, localhost only):
`TEST_BROWSER_SESSION=1 uv run --no-sync python -m unittest -q test_browser_session`.

## Linux server deployment (Docker Compose)

The image uses Python 3.13 on Debian Bookworm slim, frozen production dependencies
from `uv.lock`, and Debian Chromium with Arabic-capable Noto fonts. It runs as
UID/GID `1000:1000`. Browser Use detects `/usr/bin/chromium`; the image build
checks detection without starting a browser. Python is pinned to its minor version;
Debian packages and base-image tags can receive updates.

One `bot` service runs `telegram_bot.py` using outbound Telegram polling; no
inbound ports are published. Keep `pyproject.toml` and `uv.lock` together, including
the `python-telegram-bot` dependency. Only runtime scripts and dependency metadata
enter the build context: secrets, local virtualenvs, profiles, databases, tests,
and navigation references are excluded.

The bot and one-off readiness containers share the stable hostname
`shopping-browser`. Chromium's persistent-profile locks include the hostname;
changing it between containers can prevent startup after a stale lock remains.
Keep this hostname for all containers using the profile, and serialize them.

### Prepare the server

For iPhone voice commands without Telegram or a web UI, see
[iPhone voice shopping over SSH](docs/iphone-shortcut.md): dictation, confirmation,
background shopping, and status retrieval through a restricted SSH key.

From the project directory on Linux, with Docker Engine and Compose installed:

```sh
cp .env.server.example .env.server
chmod 600 .env.server
# Edit .env.server locally: set OPENAI_API_KEY, TELEGRAM_BOT_TOKEN,
# and TELEGRAM_ALLOWED_USER_ID (your numeric Telegram user ID).
sudo install -d -o 1000 -g 1000 -m 700 server-data \
  server-data/browser-profile server-data/data
docker compose config --quiet
docker compose build --pull bot
```

Never copy `.env.local` into the image or transfer it as part of session setup.
`.env.server` supplies credentials at runtime; keep it private and out of Git.
The bind mounts must exist and be writable by UID 1000 before starting Compose.
The database persists at `server-data/data/shopping.db`; a fresh directory starts
with an empty list. Manage that list with the CLI below.

### Transfer the login snapshot from Mac

On the Mac, close other setup/shop browsers, then run:

```sh
uv run python shopping.py setup
```

Complete login/OTP and select the delivery address or pickup branch. Press Enter
so setup saves `.browser-profile/storage-state.json` and closes the browser.
Transfer **only this snapshot**, not the Mac Chromium profile, cache, or lock files.
On the server, stop the bot and any one-off CLI containers before importing:

```sh
docker compose stop bot
```

The initial Linux profile directory must be fresh, as created above. From the Mac,
upload to a temporary file in the server user's home (replace `USER@HOST`):

```sh
scp .browser-profile/storage-state.json USER@HOST:storage-state.upload.json
```

On the server, from the project directory, install privately and replace atomically
on the same filesystem:

```sh
sudo install -o 1000 -g 1000 -m 600 "$HOME/storage-state.upload.json" \
  server-data/browser-profile/storage-state.json.new
sudo mv -f server-data/browser-profile/storage-state.json.new \
  server-data/browser-profile/storage-state.json
rm "$HOME/storage-state.upload.json"
```

Repeat this stopped-bot procedure when refreshing an expired login. With an existing
Linux profile, snapshot values take precedence over matching stale profile cookies
and storage; unrelated profile entries may remain. To discard all old Linux state,
move the stopped profile directory aside and create a fresh UID-1000, mode-700
directory before importing. Never overwrite the active snapshot while a browser is
running: its shutdown save could replace the import. Each normal run updates the
same snapshot. Snapshot transfer does not guarantee a portable login;
the retailer may expire or reject it, requiring another Mac setup and import.

### Readiness, start, and maintenance

With the bot stopped, verify the imported session explicitly:

```sh
docker compose run --rm --no-deps bot python shopping.py readiness
docker compose up -d bot
docker compose logs --tail=100 -f bot
```

`readiness` launches headless Chromium and checks login/fulfillment without changing
the cart. It prints JSON and exits 0 when ready, 1 on failure. It needs model
credentials and makes live retailer/model requests; it is a deployment check,
not a local packaging test. Add `--location "ADDRESS OR BRANCH"` to verify a specific
location. Do not use `shop` as a readiness check or automatically retry cart work.

Telegram `/add <multiword name> <positive quantity>` sets a list item's target
quantity; `/clear` (no arguments) empties the saved list, product preferences, and
approved alternatives while preserving purchase history, observed prices, and the
live cart. `/list` reads the list, and `/help` shows usage. `/shop [location]`
explicitly starts a cart-changing attempt. A completed attempt is not a purchase: use
`confirm-purchase ATTEMPT_ID` only after explicitly confirming that you placed the
order. Failed or interrupted attempts need review before another request; bot
restart does not authorize retrying a shopping attempt. Pending Telegram messages
are dropped on startup, so commands sent while the bot is offline must be resent.
List changes (`/add` and `/clear`) are blocked during `/shop`. The bot's nonretry lock
rejects concurrent work rather than queuing another attempt. Manual CLI containers
must also be serialized: stop the bot and finish other CLI/browser processes before
using the same database/profile.

Telegram reports automatic substitutions as requested item → chosen product,
including the observed package and unit price. Unresolved entries show requested
item names with canned explanations only; unknown reason codes require manual
review. Raw reason/model text is never sent. Malformed outcome fields use the
sanitized `result_format` error reply and private diagnostic reference. Exact and
approved selections retain the cart display. Every result states:
“Manual approval required for checkout.”

Shopping errors send Telegram a safe stage/type, child exit code, and diagnostic
reference ID. Exception messages and traceback details stay local.
Incomplete runs without a verified summary are logged too; Telegram receives only
a canned reason (unknown for older results) and diagnostic reference, never raw diagnostics.
Saved partial attempts with recovered browser/model failure diagnostics also log
privately and append only a diagnostic reference to the validated shopping reply.
The private logger redacts secrets, URLs, and email addresses before saving.
The cart may already have changed: review it manually before another request. The bot never
automatically reruns failed, incomplete, or interrupted cart work, even with a saved attempt.
Private structured diagnostics live beside `SHOPPING_DB`: `/data/shopping-diagnostics.jsonl`
in the container (`server-data/data/shopping-diagnostics.jsonl` on the host).
Each record includes the reference ID, exit code, result status/existence,
stage/type, allowlisted error code (unknown if absent or unsupported), redacted
bounded message and basename-only traceback frames, and
attempt ID when available; full shopping summaries and subprocess output are omitted.
Files have owner-only mode `600`, rotate at 128 KiB, and retain two backups
(`.1` and `.2`). Correlate Telegram's reference with these local records; a log
write failure still returns a sanitized reply and releases the shopping lock.

### Detailed shopping run logs

Every `shop` command automatically prints a unique log directory beside its database:
`shopping-runs/<UTC timestamp>-<run ID>/`. Readiness and shopping share this directory.
In Docker it lives under `/data/shopping-runs/` (host: `server-data/data/shopping-runs/`).
No `tee` or debug flag is needed. `--result-file` results include `run_id` and `run_logs`.

- `run.log`: timestamped agent/tool messages, phases and exception tracebacks.
- `events.jsonl`: model/endpoint and dependency versions, HTTP attempts and durations,
  returned completion text **before JSON validation**, finish reasons/token counts,
  HTTP errors, parsed actions/tool results, cleanup and final outcome. Match HTTP
  requests and responses by `call_id`; filter `model_response` events to inspect the
  complete malformed JSON rather than Pydantic's abbreviated error.

Secrets are redacted before writing. Request messages/headers, cookies, browser
storage, screenshots and images are excluded. Logs still contain private model,
account and shopping text; review before sharing. Directories are owner-only (`700`)
and files owner-only (`600`). The latest 20 completed runs are retained; active or
abruptly killed runs are preserved. Telegram replies do not expose raw debug logs.

```sh
docker compose stop bot
docker compose run --rm --no-deps bot python shopping.py --db /data/shopping.db list
docker compose run --rm --no-deps bot python shopping.py --db /data/shopping.db add "milk 1 L" 2
docker compose run --rm --no-deps bot python shopping.py --db /data/shopping.db confirm-purchase ATTEMPT_ID
docker compose up -d bot
```

Manual CLI commands explicitly pass `--db`: the bot uses `SHOPPING_DB`, while
the CLI's default database is relative to its working directory.

Compose enables an init process, a 3 GiB memory limit, two CPUs, 512 MiB shared
memory, and logs bounded to three 10 MiB files. Shutdown allows 45 seconds;
the bot waits up to 30 seconds for a terminated shopping subprocess before
force-killing it. SIGTERM cancels the workflow and saves the browser session during
cleanup. Keep total cleanup below Compose's deadline. Forced shutdown
can interrupt snapshot saving, so review interrupted attempts before resuming.
Back up the database and private profile with the bot and CLI processes stopped.
Run the isolated packaging check without any external network or saved session:

```sh
docker run --rm --init --network none --memory 3g --cpus 2 \
  --mount "type=bind,source=$PWD/docker_smoke_test.py,target=/tmp/docker_smoke_test.py,readonly" \
  --entrypoint python agent-shopping-bot /tmp/docker_smoke_test.py
```

This checks headless launch and synthetic cookie/storage restoration into a fresh
profile using localhost only. It does not verify Tamimi authentication.
