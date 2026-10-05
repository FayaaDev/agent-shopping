# Mobile shopping app

Private, list-first frontend for the existing shopping engine. Plain HTML/CSS/JavaScript and an aiohttp server; SQLite remains authoritative.

## Run locally

1. `uv sync`
2. Add these settings to the ignored `.env.local` file:

   ```dotenv
   WEB_PASSWORD=replace-with-a-unique-password-of-at-least-16-characters
   WEB_ORIGIN=http://127.0.0.1:8080
   WEB_COOKIE_SECURE=false
   ```

3. Close other setup/shop browsers and stop any Telegram controller using this database/profile.
4. `uv run python web_app.py`
5. Open `http://127.0.0.1:8080` and sign in.

The configured `WEB_ORIGIN` must exactly match the address you open, including scheme and port, without a trailing slash. `WEB_COOKIE_SECURE=false` is for local HTTP development only. The password is server configuration, never stored in browser storage. Sessions expire after 12 hours and on server restart.

## Use from your phone

Serve through an HTTPS reverse proxy to port 8080. Set `WEB_ORIGIN` to your actual HTTPS origin and `WEB_COOKIE_SECURE=true`. Use your phone browser's **Add to Home Screen** option. The app requires a network connection; it does not queue offline shopping actions.

For a trusted local network, `WEB_HOST=0.0.0.0` allows other devices to connect; origin and cookie settings must match the address and HTTPS configuration. Do not expose plaintext HTTP/password login publicly.

## Existing Docker server

Use the existing profile/data directories and `.env.server`. Set a unique `WEB_PASSWORD` of at least 16 characters plus the actual HTTPS `WEB_ORIGIN` and `WEB_COOKIE_SECURE=true` there.

```sh
docker compose stop bot
# Verify no active shopping/browser process owns the profile before proceeding.
docker compose -f compose.yaml -f compose.web.yaml up -d --build bot
```

The override replaces Telegram inside the **same service** (`bot`) and retains hostname `shopping-browser`, profile/data mounts, and cleanup timeout. Port 8080 is published to server loopback only; the reverse proxy must reach it from the host. A containerized proxy needs appropriate private-network routing instead.

Do not launch a separate Telegram service, second web process/replica, or CLI setup/shop command against the shared profile while this service runs. The application lock coordinates requests within one web process. To use setup or the CLI for mutations, stop the web service first. Existing login-snapshot transfer instructions still apply.

These files prepare deployment; they do not configure a domain, HTTPS proxy, or change the running server automatically.

## Workflow

- Add a grocery item; quantity is the target number of purchasable units. Adding an existing name sets its quantity.
- Use quantity buttons, remove items, or edit the preferred product, brand, SKU, and price cap. Preference saving replaces those fields; blank optional fields clear them. The existing CLI requires a preferred product to save preferences.
- Clear list asks for confirmation and removes saved preferences/alternatives; the live cart and purchase history stay unchanged.
- Choose an optional location and tap **Prepare cart**. List mutations remain blocked until browser cleanup finishes.
- Review cart rows, packages, substitutions, unresolved items, and the slot. Running status is run-level, not simulated item-by-item progress.
- Finish checkout yourself in Tamimi. Only then use **I completed checkout** to explicitly record the saved attempt as purchased. This never places an order.

The latest sanitized run survives restart in `<database>.web-run.json` (private permissions). Interrupted runs become incomplete and are never retried automatically. Failures show a run reference; private diagnostics remain beside the database in `shopping-diagnostics.jsonl`. Missing summary is never treated as success.

## Verification

```sh
uv run --no-sync python -m unittest -q
node --check web/app.js
```

Tests use temporary databases and mocked shopping subprocesses. Do not use a live `shop` run as a frontend test.
