# Autonomous Shopping Design

## Goal

Allow a Tamimi shopping run to complete more of a list without manual product
preapproval. The workflow may safely recover from item-level search/navigation
failures and select bounded automatic substitutes. It must still stop before
payment or order placement.

## Scope

This changes the existing `shopping.py` and Telegram workflow only. It does
not add a second agent, a product catalog integration, automatic order retries,
or payment automation.

## Automatic Substitution Policy

An exact SKU or preferred product remains first choice. Existing explicitly
approved alternatives remain next choice. When neither is available, the cart
agent may select an automatic substitute only when all conditions hold:

- Same product type as the requested item.
- Preferred/requested brand when available; otherwise closest comparable
  mainstream brand.
- A package size that meets or exceeds the requested need, preferring the
  closest sufficient package size.
- Price does not exceed the item `max_price`, when set.
- Without `max_price`, price does not exceed 115% of the least-expensive
  same-type candidate with the nearest sufficient package size found during
  that shopping run.
- The agent can observe and report the product name, package size, price, and
  comparison product/price before adding it.

Ambiguous product type, insufficient evidence, a price-cap breach, or no safe
candidate leaves that item unresolved. The agent must not add a substitute in
those cases.

## Workflow

The cart agent first inspects the current cart and preserves unrelated items.
For each missing list item it searches for the exact/preapproved products, then
applies the automatic substitution policy when necessary. It uses the existing
single-match `add_product_plus` tool and verifies cart quantity after each add.

Recoverable item-level failures use at most three distinct recovery actions in
the same run: alternate search terms and safe page/cart refresh or navigation.
The agent never repeats an unchanged failed click. Once the budget is spent,
it records the item as unresolved and continues with remaining items.

Account/login, fulfillment, CAPTCHA, unsafe navigation/clicks, payment/order
paths, or an unavailable/ambiguous product-plus control remain run-level
stops. Failed, incomplete, and interrupted runs are never retried by Telegram
or by a new process automatically.

After items are processed, the workflow reopens the cart, verifies quantities
and prices, optionally selects the earliest slot, and stops at `slot_selected`.

## Attempt Records And Learning

Each cart summary records an outcome for every requested item: its selected
product and selection mode (`exact`, `approved_alternative`, or
`automatic_substitution`), or its unresolved reason. Automatic substitutions
also record observed package size and price plus the comparison candidate and
price used for the 15% rule.

Assessment validates these records alongside current quantity and configured
price-cap checks. Missing evidence makes the item unresolved and prevents a
complete attempt.

Automatic substitutions are attempt-scoped. `confirm-purchase` promotes only
automatic substitutions from that confirmed attempt into the existing
`alternatives` table, including SKU when observed. Promotion is idempotent and
does not occur before explicit purchase confirmation or for failed/interrupted
runs without a saved attempt.

## Telegram Output

Telegram continues to send sanitized results only. A shopping result lists
automatic substitutions, their observed prices, and unresolved items with a
safe reason. Raw browser/model failures remain only in the rotating private
diagnostic log. Every result states that manual checkout approval is required.

## Verification

Add focused unit coverage for:

- automatic-substitution acceptance and rejection for type, package coverage,
  explicit cap, and the implicit 15% cap;
- bounded recovery that continues to later items after one item becomes
  unresolved;
- attempt-summary evidence and assessment of automatic substitutions;
- promotion after `confirm-purchase`, including idempotency;
- no promotion before purchase confirmation.

Retain existing safety-guard, product-plus, no-summary, and Telegram diagnostic
tests. Run `uv run --no-sync python -m unittest -q`; do not use a live Tamimi
shopping run as verification.
