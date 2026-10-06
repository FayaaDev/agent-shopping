# iPhone voice shopping over SSH

Tap a Shortcut, dictate groceries, approve the interpreted plan, and let the
server prepare the cart in the background. A second Shortcut checks the result.
Telegram and the web app are not used. Dictation produces text on the iPhone;
audio recordings are not uploaded to the server.

Supported requests:

- “Set milk one litre to two cartons and Greek yogurt three-pack to ten packs.”
- “Remove milk from my list.”
- “Show my list.”
- “Clear my saved list.”
- “Start shopping.”
- “Set eggs to two cartons, maximum twenty riyals per carton, then start shopping.”

Quantities are **target purchasable units**. Two cartons means a target of two,
not two additional cartons. “Add two more” asks for clarification. Say the target
quantity instead. Shopping starts only when explicitly requested and confirmed.
Checkout requires human approval; no payment or order placement is supported.

## 1. Prepare the server

These are deployment instructions, not actions performed by the coding agent.
Use the existing Linux project at `/srv/docker/agent-shopping`. Upload
`voice_shortcut.py` alongside `shopping.py`. The gateway bind-mounts the script
read-only into existing one-off Compose containers; no image rebuild is needed
if the current image already contains this project's dependencies and code.

Requirements:

- Host Python 3.10+, Docker Engine, Docker Compose v2.
- Existing working shopping image, private `.env.server` with OpenAI settings,
  and restored Tamimi session. Telegram credentials are not needed for this path.
- An SSH account with **UID 1000**, Docker access, and read/write access to
  `server-data/data`. Container workers also run as UID 1000. Check `id -u`
  before choosing the account; do not change an existing account's UID.
- `SHOPPING_DB=/data/shopping.db` (the existing Compose default).
- Network access from your iPhone to the SSH host. If SSH is reachable only on
  your private network, connect the phone to that network/VPN first.

The gateway and Compose files must be maintained by an administrator. Do not
make the project or SSH authorization files writable by unrelated users.

Before switching interfaces, stop the bot and any local voice service, and verify that no
other setup, readiness, shopping, or browser process owns the shared profile:

```sh
docker compose stop bot
docker ps --format '{{.Names}}  {{.Status}}'
```

Keep `hostname: shopping-browser` in Compose. Do not delete profile locks while
a browser owns them. The gateway rejects competing containers and browser
processes rather than stopping them.

Verify the transport without opening a browser, from the project directory as
the chosen SSH account:

```sh
SSH_ORIGINAL_COMMAND=list python3 voice_shortcut.py gateway \
  --project /srv/docker/agent-shopping
```

Expect one JSON object containing the saved list. This confirms Docker/data
access, not login or end-to-end shopping success. Session refresh follows the
README's Mac setup and snapshot-transfer procedure.

## 2. Configure a dedicated iPhone SSH key

In Apple's **Shortcuts**, add a **Run Script over SSH** action. Expand its
connection settings:

1. Enter the host, port, and username for the UID-1000 account above.
2. Select SSH-key authentication and generate a dedicated key if needed.
3. Copy/share its **public key** to the server administrator. Keep the private
   key on the iPhone; do not put server passwords in Shortcut text actions.

Add that public key as one line in the account's `~/.ssh/authorized_keys`, with
the following prefix (replace the example key):

```text
restrict,command="/usr/bin/python3 /srv/docker/agent-shopping/voice_shortcut.py gateway --project /srv/docker/agent-shopping" ssh-ed25519 YOUR_IPHONE_PUBLIC_KEY iphone-shopping
```

Use permissions `700` for `~/.ssh` and `600` for `authorized_keys`. The
restriction applies to this key, so existing administrative keys can remain.
This key can invoke only the shopping protocol, not arbitrary shell commands,
port forwarding, or an interactive shell. Verify the server's SSH host key
before accepting its fingerprint on the phone.

Use this same restricted key in every shopping Shortcut. To revoke phone
access, remove its authorization line.

## 3. Build “Shopping voice”

Create a Shortcut with these actions, in order. Action labels can vary by iOS
language/version; use the named outputs as variables, not literal placeholders.

1. **Dictate Text** — choose your spoken language; stop listening after a pause
   or on tap. Arabic and English text are accepted by the server.
2. **Base64 Encode** the dictated text — choose **no line breaks**.
3. **Text** containing `preview ` followed immediately by the encoded-text
   variable. This must be one line with one space after `preview`.
4. **Run Script over SSH** using that Text as its script, with the restricted
   connection above. Do not append a newline or any shell syntax.
5. **Get Dictionary from Input** using the SSH result.
6. **Get Dictionary Value** for `message`; save it as `Preview message`.
7. **Get Dictionary Value** for `token`.
8. **If** the token has no value: **Show Result** with `Preview message`, then
   **Stop This Shortcut**. Clarification/error responses do not execute anything.
9. **Save File** containing the token to
   `iCloud Drive/Shortcuts/shopping-last-token.txt`. Disable “Ask Where to Save”
   and enable overwrite. Save **before** confirming, so a lost connection does
   not lose the status reference.
10. **Choose from Menu**, using `Preview message` as the prompt:
    - **Confirm**: continue with the actions below.
    - **Cancel**: **Stop This Shortcut**.
11. Inside **Confirm**, make a **Text** action containing `confirm ` followed by
    the saved token variable.
12. **Run Script over SSH** with that Text and the same connection.
13. **Get Dictionary from Input**, retrieve `message`, and **Show Result**.

Add the Shortcut to your Home Screen or Action Button. You can close Shortcuts
after launch; the server worker continues independently of the SSH connection.
There is no shopping retry when the connection drops.

## 4. Build “Shopping status”

1. **Get File** from `iCloud Drive/Shortcuts/shopping-last-token.txt`.
2. **Get Text from Input** to read the file contents as the token.
3. **Text**: `status ` followed by the token variable.
4. **Run Script over SSH** with that Text and the same restricted connection.
5. **Get Dictionary from Input** → **Get Dictionary Value** `message` →
   **Show Result**. Optionally add **Speak Text**.

For a standalone “Shopping list” Shortcut, send the literal one-line script
`list`, then display its JSON `message` the same way.

## Protocol and recovery

The server accepts exactly four command forms, with no trailing newline:

```text
preview BASE64_UTF8_TEXT
confirm TOKEN
status TOKEN
list
```

Tokens are 32 lowercase hexadecimal characters. Preview approval expires after
10 minutes. A changed saved list invalidates the preview: dictate again.
Repeated confirmation of the same token retrieves its state; it never launches
shopping again. List edits are transactional, and concurrent confirmed work is
rejected instead of queued. Keep manual CLI use serialized with this interface;
older CLI/bot/web entry points do not participate in the voice workflow lock.

Status meanings:

| Status | Meaning |
| --- | --- |
| `preview` | Awaiting confirmation. |
| `expired` | Dictate again; nothing launched. |
| `running` | Worker active, including browser cleanup. |
| `completed` | List-only plan finished, or validated cart and slot prepared. Read the message. |
| `incomplete` | Cart/slot was not fully verified. Review manually before another request. |
| `failed` / `interrupted` | Execution failed or stopped; the cart may have changed. |
| `clarification` / `rejected` | Read the message; no new run authorized. |

Private plans and status live in `server-data/data/voice-shortcut/`; diagnostics
remain in `server-data/data/shopping-diagnostics.jsonl`. Keep both private.
Stopped containers named `shopping-voice-TOKEN` are retained to establish run
exit state. Removing a retained container makes its status conservatively
interrupted; it does not authorize replay.

If launch fails with “Previous launch uncertain,” an administrator must inspect
the retained container, processes, profile ownership, and referenced diagnostics.
Only after verifying that no worker/browser remains may they remove the
`voice-shortcut/active.json` reservation. Leave that token's `started.json` and
database replay record intact. Submit a new explicitly approved request after
reviewing the cart; never retry the old worker.

To switch back to Telegram/web, finish or gracefully stop voice workers, verify
browser cleanup, and then start the desired service. Do not run both interfaces
against the same profile concurrently.

## Local verification

```sh
uv run --no-sync python -m unittest -q
```

Voice tests use temporary databases and mocked model/Docker/browser calls. They
do not change the live Tamimi cart. A first phone connection can use `list`;
testing `preview` consumes a model request but does not execute the plan. Confirm
a shopping plan only when you intend to change the live cart.
