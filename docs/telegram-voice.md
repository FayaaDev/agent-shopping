# Telegram voice shopping

Send a voice message in Arabic, English, or a mix to your existing shopping bot.
Text grocery requests work too. The bot returns the transcript, proposed changes,
and **the full merged shopping list** with quantities, saved preferences,
alternatives and price caps. Press **Run shop** to apply that exact plan and prepare
the cart, or **Cancel** to discard it. Nothing changes before approval.

If a quantity or item reference is unclear, answer the clarification in text or
another voice message. Corrections regenerate the preview; older buttons stop
working. `/cancel` clears the draft so the next message starts a new request.
Quantities are target purchasable units, not increments or cups inside a pack.

## Provider configuration

The Docker bot already uses `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_ID`,
`OPENAI_API_KEY`, `OPENAI_BASE_URL` and `OPENAI_MODEL` from its private runtime
environment. Add these speech settings to `/srv/docker/agent-shopping/.env.server`:

```dotenv
ELEVENLABS_API_KEY=your-key
# Optional voice; the default is George.
ELEVENLABS_VOICE_ID=JBFqnCBsd6RMkjVDRZzb
```

Edit the file on the server; never paste keys into chat or commit them. After
changing credentials, recreate the container so it receives the new environment:

```sh
docker compose --project-directory /srv/docker/agent-shopping \
  -f /srv/docker/agent-shopping/compose.yaml up -d --force-recreate --no-build bot
```

Read-only configuration/authentication check, with no shopping or audio request:

```sh
docker exec agent-shopping-bot-1 python telegram_bot.py --check
```

It prints configuration booleans and the public bot username, never credentials.
Missing speech configuration leaves voice unavailable; text previews still work.
Only the allowed user in a private chat can access messages or approval callbacks.

## Commands and boundaries

- `/start` or `/help`: voice/text instructions and available commands.
- `/list`: saved list, which is unchanged until approval.
- `/status`: current recorded run; observes status without launching anything.
- `/cancel`: discard the pending draft. Does not cancel an already approved run.
- Existing `/add`, `/clear` and `/shop [location]` remain available as explicit commands.
- Previews expire after ten minutes and reject a changed database snapshot.
- New input, `/cancel`, or restart invalidates earlier approval buttons.
- List mutations are blocked during a run or while its launch remains uncertain.
- Shopping is never retried automatically; pending Telegram updates are dropped on restart.
- Confirmed shopping is independent of the Telegram reply. A lost reply does not replay it.
- Oversized full previews offer no approval button; shorten the request/list first.
- Voice messages are limited to 180 seconds and 8 MB. Audio stays in memory during
  transcription; the bot does not archive it. ElevenLabs receives the audio through
  its paid batch transcription service; provider retention follows your account settings.
- Text replies remain available even if optional spoken audio replies fail.
- Cart preparation preserves existing cart items and stops before payment or ordering.
  Record an actual purchase separately with `shopping.py confirm-purchase ATTEMPT_ID`.

`shopping.db`, the stable `shopping-browser` hostname and existing browser profile
remain authoritative. Keep other owners of the same profile stopped. Private run
records live under `voice-shortcut/` beside the database; diagnostics remain in
`shopping-diagnostics.jsonl`. Do not delete reservation files to bypass uncertainty.

## Verification

```sh
uv run --no-sync python -m unittest -q
```

Tests mock Telegram, speech providers and shopping processes. They check private
authorization, size limits, clarification, full previews, caps/preferences,
stale/duplicate callbacks, restart uncertainty and browser cleanup. They never
change the live Tamimi cart. Real speech quality and end-to-end shopping require
an intentional user run; successful startup does not establish either.
