# Local voice shopping

Telegram is now the primary interface; see [Telegram voice setup](telegram-voice.md).
This optional local page is retained and remains stopped while Telegram owns its profile.

Speak Arabic, English, or a mix; hear replies; correct the transcript; review the
**entire merged list** and its changes. Stop the microphone and press **Run shop**.
Only that button commits the approved changes and starts cart preparation.

The Cloudflare Agents SDK and `@cloudflare/think` run under local Wrangler with a
SQLite-backed Durable Object. The SDK voice channel uses ElevenLabs Scribe v2
Realtime and multilingual v2 speech output. Audio and model requests go to their
online paid providers; this is local hosting, not offline AI. The configured
OpenAI-compatible endpoint powers Think and the existing Python interpretation
and shopping agents. ElevenLabs STT logging is disabled where the provider supports it.

## Start

Requires Node.js 22.12+ (or a newer supported Node release), npm, and the existing
Python/uv environment. Install the frontend/Worker dependencies once:

```sh
npm --prefix voice-agent ci
```

Keep the existing `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `OPENAI_MODEL` in
`.env.local`. Add your speech key there, or to `voice-agent/.dev.vars`:

```dotenv
ELEVENLABS_API_KEY=your-key
# Optional; defaults to the SDK's George voice.
ELEVENLABS_VOICE_ID=JBFqnCBsd6RMkjVDRZzb
```

Use the model ID exposed by your configured endpoint; the default is `gpt-4.1`.
Mixed speech and Saudi product-name recognition need verification with your own
recordings. Arabic support does not guarantee dialect or grocery-name accuracy.

Close other setup/shop browsers and stop any Telegram process that shares this
Mac's profile/database. The launcher reads `.env.local` and `voice-agent/.dev.vars`,
with environment variables taking precedence. It generates and retains random
local session and bridge secrets in the ignored, owner-only `.dev.vars` file.
Nonempty `.dev.vars` values override `.env.local` values; update that file when
changing a previously saved provider setting.

```sh
uv run --no-sync python voice_local.py
```

Open **http://localhost:8787**. Use `localhost`, not the LAN address or
`127.0.0.1`: requests must match the configured origin. Localhost permits browser
microphone access without HTTPS. Services bind to loopback; no Cloudflare Access,
Tunnel, account login, or remote deployment is required.

To keep services running in the background:

```sh
uv run --no-sync python voice_local.py --background
uv run --no-sync python voice_local.py --stop
```

Background startup logs are private at `voice-agent/.local.log`. Shutdown waits for
shopping-worker/browser cleanup. Speech controls remain unavailable until an
ElevenLabs key exists; typing works with the model and bridge configured.

## Review and execution

- Quantities are absolute target purchasable units, not increments or cups inside packs.
- Merge retains saved items, preferences, alternatives, and caps unless explicitly changed.
- Missing quantities, ambiguous references, unsupported budgets, and unclear caps require clarification.
- Transcript edits and new utterances invalidate approval. Previews expire after ten minutes.
- The merged list includes saved preferences, approved alternatives, and explicit per-unit caps.
- A stale database snapshot prevents applying the preview. Regenerate and review it.
- **Run shop** adds a shopping action even when the dictation does not say “shop.”
- Shopping status is independent of the page. Reconnect or **Check run status** observes the same run.
- Duplicate approvals, network uncertainty, and restarts never automatically launch/retry shopping.
- **Mute spoken replies** keeps text visible. **Read result aloud** works after stopping the microphone.
- Cart preparation preserves the live cart and stops before payment/order placement.
  Record a real purchase separately with `shopping.py confirm-purchase ATTEMPT_ID`.

`shopping.db` remains authoritative. Cloudflare state stores conversation, the
unapplied preview and run references—not a second saved shopping list. Local
conversation state lives in `voice-agent/.wrangler/`. Python preview/run records
live in `voice-shortcut/` beside the selected database. Keep both private.

If a launch is uncertain, inspect its run reference and private diagnostics before
trying again. Do not delete `active.json`, `started.json`, or run tokens to bypass
the replay guard. Telegram now runs the same preview/approval workflow as the
primary interface. The earlier Python web interface and Compose override were removed.

## Checks

```sh
uv run --no-sync python -m unittest -q
npm --prefix voice-agent run check
npm --prefix voice-agent run test
npm --prefix voice-agent run build
uv run --no-sync python voice-agent/smoke_test.py
```

The smoke test starts a real local Worker with temporary state, fake secrets and
a mocked bridge. It verifies preview/approval/status and security without provider
calls or shopping. It does not establish real STT/TTS quality or end-to-end Tamimi
shopping success. Never use a live shopping run as an automated test.

The installed SDK still carries a moderate `sprintf-js` denial-of-service advisory
through its shell dependency. Shopping exposes only read-only list/preview/status
tools to Think; its shell tools are not model-callable. The patched `undici`
dependency is pinned with an override; no high/critical production advisories
remained in the installation audit. Review the shell advisory when upgrading the SDK.
