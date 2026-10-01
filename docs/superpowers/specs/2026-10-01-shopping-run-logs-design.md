# Shopping run diagnostics

Approved design: every CLI `shop` invocation creates a private folder beside its
database at `shopping-runs/<UTC timestamp>-<unique run ID>/`. Readiness and shopping
share the run. Retain 20 completed run folders; never prune an active run.

`run.log` contains timestamped agent/tool logs and application lifecycle messages.
`events.jsonl` contains configuration/dependency metadata, phase changes, model
HTTP attempts and durations, raw returned completion text before JSON validation,
finish reasons, parsed actions, tool results, exceptions, cleanup and final outcome.
HTTP response hooks capture completions without modifying parsing or retry behavior.

Redact secrets before persistence, including environment secrets, bearer values,
credential patterns and URL credentials/query strings. Never persist request
headers, cookies, browser storage, screenshots, images or full request messages.
Directories use 0700 and files 0600. Logs contain private shopping/account text.
Run paths are printed and included in structured results; Telegram continues using
canned replies without raw debug text. Diagnostics stay outside SQLite and images.

Checks: malformed JSON captured before rejection; secret redaction and permissions;
HTTP retry/transport failures; actions/results including failed steps; interrupted
run finalization; bounded retention; full existing unittest suite. No live cart tests.
