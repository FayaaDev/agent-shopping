# Flexible product matching — approved design

## Policy

Prefer exact known products, then suitable approved alternatives; otherwise let
the agent choose the nearest suitable product from visible evidence. Type matching
is semantic. Explicit brand, flavor, fat/dietary, package and price requirements
remain binding. Unspecified attributes are flexible. Generic Greek yogurt can
match Nada Greek Yogurt Assorted Pack3X160G without a preferred SKU or brand.

Require exact/approved unavailability only where those saved choices exist.
Require a package need only when specified; use the nearest sufficient size then.
Observe actual product/package/price even when the request leaves them flexible.
Explicit price caps always apply; otherwise retain the 115% observed comparison
cap, using the cheapest eligible candidate (nearest sufficient size when specified).

## Evidence and lifecycle

Keep the existing outcome modes and SQLite schema. Each automatic candidate adds
`matches_request`, `match_reason`, and `match_evidence` (quotes from observed name,
brand or package). The agent judges semantic type and all explicit attributes;
code requires a positive judgment, nonempty reason, verifiable quotes, observed
package/type/price, candidate membership, saved brand, numeric package consistency
and price limits. Code does not pretend to independently understand natural language
or verify the retailer DOM from saved text. Unknown or negative matches are unresolved.

Use the same validator before adding, recording an already-satisfied item, final
assessment, and purchase confirmation. Final automatic evidence must equal the
recorded selection. Include eligible existing products among observed candidates;
count only validated cart matches with consistent observed prices. Legacy automatic
outcomes lacking semantic evidence are not silently promoted on confirmation.

## Quantities and safety

Targets and cart quantities remain purchasable units. Ten packs of three cups means
10 cart units / 30 cups; no target conversion. Add only the aggregate deficit across
qualifying products. Preserve unrelated cart contents. Telegram identifies product,
observed package and price per purchasable unit and accepts absent optional criteria.

Preserve quantity increment checks, no uncertain replay, recovery limits, product-plus
selector guards, and stop after slot selection before payment/order placement.
Only explicit purchase confirmation promotes a validated saved automatic choice.

## Verification

Regression checks cover generic yogurt already satisfied at 10 packs, one missing
pack, mixed qualifying cart products, semantic evidence rejection, explicit brand,
flavor/diet judgment, package and caps, changed final evidence, purchase promotion,
and Telegram output. Run `uv run --no-sync python -m unittest -q` and
`git diff --check`. No live shopping, remote operations or commits.
