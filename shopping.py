"""Build a Tamimi grocery cart from SQLite, then pause at the selected slot."""

import argparse
import asyncio
import json
import math
import os
import re
import signal
import sqlite3
import sys
import traceback
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


PROFILE = Path(__file__).resolve().parent / ".browser-profile"
SITE = "https://shop.tamimimarkets.com/"
PRODUCT_PLUS_SELECTOR = '[class*="ProductDetails__ImgAndCarouselDiv"] svg[class*="Counter__StyledAddToCart"]'


async def click_product_plus(browser):
    if urlsplit(await browser.get_current_page_url()).hostname != "shop.tamimimarkets.com":
        raise ValueError("Product + is only supported on Tamimi")
    page = await browser.get_current_page()
    if page is None:
        raise ValueError("No current product page")
    controls = await page.get_elements_by_css_selector(PRODUCT_PLUS_SELECTOR)
    if len(controls) != 1:
        raise ValueError(f"Product + control missing or ambiguous (match count: {len(controls)}); no click performed")
    await controls[0].click()


async def verify_product_page(browser, product):
    """Bind observed evidence to its product page before any cart mutation."""
    observed_url = product.get("product_url")
    if not isinstance(observed_url, str) or blocked_action({"navigate": {"url": observed_url}}, SITE, {}):
        raise ValueError("Observed product page unavailable")
    if await browser.get_current_page_url() != observed_url:
        raise ValueError("Current product page differs from observed selection")


def create_browser(headless=None):
    from browser_use import Browser

    if headless is None:
        value = os.getenv("BROWSER_USE_HEADLESS", "false").strip().lower()
        if value not in {"true", "false"}:
            raise ValueError("BROWSER_USE_HEADLESS must be true or false")
        headless = value == "true"
    PROFILE.mkdir(mode=0o700, parents=True, exist_ok=True)
    PROFILE.chmod(0o700)
    # Intentional: the snapshot restores session cookies/storage lost on profile restart.
    return Browser(user_data_dir=PROFILE, headless=headless, keep_alive=True,
                   storage_state=PROFILE / "storage-state.json")


async def start_browser(browser, site):
    await browser.start()
    if Path(browser.browser_profile.user_data_dir).resolve() != PROFILE:
        raise ValueError("Shared browser profile unavailable; close other setup/shop browsers and retry")
    await browser.navigate_to(site)


async def setup():
    if not sys.stdin.isatty():
        raise ValueError("Run setup in an interactive terminal for manual login")
    browser = create_browser(headless=False)
    try:
        await start_browser(browser, SITE)
        print("Log in manually and select your delivery address or pickup branch in the browser.")
        await asyncio.to_thread(input, "When login and fulfillment are ready, press Enter here to close and save the session...")
    finally:
        await browser.kill()
    print("Browser profile saved. Run shop to verify login/address and prepare the cart.")


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS items (
    name TEXT PRIMARY KEY COLLATE NOCASE,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    preferred_name TEXT,
    sku TEXT,
    brand TEXT,
    max_price REAL CHECK(max_price IS NULL OR max_price > 0)
);
CREATE TABLE IF NOT EXISTS alternatives (
    item_name TEXT NOT NULL REFERENCES items(name) ON DELETE CASCADE,
    name TEXT NOT NULL,
    sku TEXT,
    PRIMARY KEY (item_name, name)
);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    summary TEXT NOT NULL,
    purchased_at TEXT
);
CREATE TABLE IF NOT EXISTS prices (
    attempt_id INTEGER NOT NULL REFERENCES attempts(id),
    product_name TEXT NOT NULL,
    unit_price REAL NOT NULL,
    PRIMARY KEY (attempt_id, product_name)
);
"""


def open_db(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def read_items(db):
    items = [dict(row) for row in db.execute("SELECT * FROM items ORDER BY name")]
    for item in items:
        item["alternatives"] = [dict(row) for row in db.execute(
            "SELECT name, sku FROM alternatives WHERE item_name = ? ORDER BY name", (item["name"],)
        )]
    return items


def rank_alternatives(item, api_key=None):
    choices = item["alternatives"]
    if len(choices) < 2 or not api_key:
        return choices
    payload = {
        "model": "jev-latest",
        "state": {"request": item["name"], "preferred_product": item["preferred_name"],
                  "brand": item["brand"], "approved_alternatives": choices},
        "questions": {"best": {"type": "choice",
                               "instructions": "Which approved alternative best matches the requested product and preferred brand/package? Choose none if uncertain.",
                               "criteria": {**{f"item_{i}": choice["name"] for i, choice in enumerate(choices)},
                                            "none": "No safe match"}}},
    }
    request = Request("https://api.typesafe.ai/v1/systemone",
                      json.dumps(payload).encode(), {"Authorization": f"Bearer {api_key}",
                                                    "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=15) as response:
            answer = json.load(response)["answers"]["best"]
        if answer["choice"] == "none" or answer["confidence"] < 0.8:
            return []
        chosen = int(answer["choice"].removeprefix("item_"))
        if not 0 <= chosen < len(choices):
            return []
        return [choices[chosen]] + [choice for i, choice in enumerate(choices) if i != chosen]
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return []  # Never guess when the semantic decision fails.


def selection_reason(item, outcome):
    """Validate observed selection evidence; return a safe unresolved reason."""
    def text(value):
        return isinstance(value, str) and bool(value.strip())

    def number(value, positive=False):
        return type(value) in (int, float) and math.isfinite(value) and (value > 0 if positive else value >= 0)

    if (not isinstance(outcome, dict) or not text(outcome.get("item_name")) or
            outcome["item_name"].casefold() != item["name"].casefold()):
        return "insufficient_evidence"
    if outcome.get("unresolved_reason"):
        reason = outcome["unresolved_reason"]
        return reason if reason in ITEM_REASONS else "insufficient_evidence"
    product = outcome.get("product")
    if not isinstance(product, dict) or not text(product.get("name")) or not number(product.get("unit_price")):
        return "insufficient_evidence"
    cap = item.get("max_price")
    if cap is not None and (not number(cap, True) or Decimal(str(product["unit_price"])) > Decimal(str(cap))):
        return "price_cap"
    if item.get("brand") and str(product.get("brand", "")).casefold() != item["brand"].casefold():
        return "no_safe_candidate"

    def matches(choice):
        if choice.get("sku"):
            return product.get("sku") == choice["sku"]
        return product["name"].casefold() == choice["name"].casefold()

    mode = outcome.get("selection_mode")
    if mode == "exact":
        return None if matches({"name": item.get("preferred_name") or item["name"], "sku": item.get("sku")}) else "no_safe_candidate"
    if mode == "approved_alternative":
        return None if any(matches(choice) for choice in item["alternatives"]) else "no_safe_candidate"
    if mode != "automatic_substitution":
        return "insufficient_evidence"
    if ((item.get("preferred_name") or item.get("sku")) and outcome.get("exact_unavailable") is not True or
            item["alternatives"] and outcome.get("approved_unavailable") is not True):
        return "insufficient_evidence"
    requested_type = outcome.get("requested_type")
    if not text(requested_type) or not text(product.get("product_type")):
        return "ambiguous_type"
    if product.get("matches_request") is not True:
        return "ambiguous_type"
    need, unit = outcome.get("required_package_quantity"), outcome.get("package_unit")
    if need is not None and (not number(need, True) or not text(unit)):
        return "insufficient_evidence"
    units = {"ml": ("volume", 1), "l": ("volume", 1000), "g": ("weight", 1),
             "kg": ("weight", 1000), "count": ("count", 1), "pack": ("count", 1), "eggs": ("count", 1)}
    size_pattern = r"(?:(\d+)\s*[x×]\s*)?(\d+(?:\.\d+)?)\s*(ml|kg|l|g|count|pack|eggs)\b"
    requested_size = (re.search(size_pattern, item["name"], re.I) or
                      re.search(size_pattern, item.get("preferred_name") or "", re.I))
    if requested_size:
        if not number(need, True) or not text(unit):
            return "insufficient_package"
        required_unit, scale = units[requested_size[3].lower()]
        observed_unit, observed_scale = units.get(unit.casefold(), (None, 0))
        required_quantity = Decimal(requested_size[1] or "1") * Decimal(requested_size[2]) * scale
        if observed_unit != required_unit or Decimal(str(need)) * observed_scale != required_quantity:
            return "insufficient_package"

    def observed(candidate):
        if not isinstance(candidate, dict):
            return False
        evidence = candidate.get("match_evidence")
        visible = " ".join(str(candidate.get(key) or "") for key in ("name", "brand", "package_size"))
        return (isinstance(candidate, dict) and text(candidate.get("name")) and
                text(candidate.get("package_size")) and
                number(candidate.get("package_quantity"), True) and number(candidate.get("unit_price")) and
                text(candidate.get("product_type")) and text(candidate.get("package_unit")) and
                candidate.get("matches_request") is True and text(candidate.get("match_reason")) and
                isinstance(evidence, list) and bool(evidence) and
                all(text(quote) and quote.casefold() in visible.casefold() for quote in evidence))

    def package_quantity(candidate):
        dimension, scale = units.get(candidate["package_unit"].casefold(), (None, 0))
        required_dimension, required_scale = units.get((unit or "").casefold(), (None, 0))
        if dimension is None or dimension != required_dimension:
            return None
        return Decimal(str(candidate["package_quantity"])) * scale / required_scale

    if not observed(product):
        return "insufficient_evidence"
    if need is not None and (package_quantity(product) is None or package_quantity(product) < Decimal(str(need))):
        return "insufficient_package"
    brand = item.get("brand") or outcome.get("requested_brand")
    if brand is not None and not text(brand):
        return "insufficient_evidence"
    if brand and str(product.get("brand", "")).casefold() != brand.casefold():
        return "no_safe_candidate"
    candidates = outcome.get("candidates")
    comparison = outcome.get("comparison")
    if (not isinstance(candidates, list) or not candidates or not all(observed(row) for row in candidates)
            or product not in candidates or comparison not in candidates):
        return "insufficient_evidence"
    sufficient = [row for row in candidates if (not brand or str(row.get("brand", "")).casefold() == brand.casefold())
                  and (need is None or package_quantity(row) is not None and package_quantity(row) >= Decimal(str(need)))]
    if need is not None:
        nearest_size = min(package_quantity(row) for row in sufficient)
        sufficient = [row for row in sufficient if package_quantity(row) == nearest_size]
    if product not in sufficient:
        return "no_safe_candidate"
    baseline = min(sufficient, key=lambda row: row["unit_price"])
    if comparison != baseline:
        return "insufficient_evidence"
    if cap is None and Decimal(str(product["unit_price"])) > Decimal(str(baseline["unit_price"])) * Decimal("1.15"):
        return "price_cap"
    return None


ITEM_REASONS = frozenset("ambiguous_type insufficient_evidence insufficient_package price_cap no_safe_candidate recovery_exhausted item_unavailable quantity_unverified".split())


class ItemRecovery:
    """One run's recovery budget, keyed by the requested item."""

    def __init__(self):
        self.actions = {}
        self.unresolved = {}

    def allow(self, item_name, action):
        actions = self.actions.setdefault(item_name, set())
        if "input" in action:
            action = {"search_terms": action["input"].get("text", "").strip().casefold()}
        elif "navigate" in action:
            action = {"navigate": action["navigate"].get("url")}
        signature = json.dumps(action, sort_keys=True, ensure_ascii=False)
        if item_name in self.unresolved or signature in actions or len(actions) >= 3:
            self.unresolved[item_name] = "recovery_exhausted"
            return False
        actions.add(signature)
        return True


def shopping_task(site, items, location=None):
    url = urlsplit(site)
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ValueError("Store URL must be an HTTPS URL without credentials")
    guide = f"""Tamimi guide (Shoppingsteps.json is not a replay): search in the header,
open the matching product and add it with the + sign beside the PRODUCT IMAGE.
Product-image + SVG CSS selector used by add_product_plus:
{PRODUCT_PLUS_SELECTOR}
Use ONLY the add_product_plus action to add a unit; it queries the current DOM
with this selector and clicks the plus SVG only with exactly one match.
Do not use generic click, coordinates, or evaluate to add products. Do NOT
click the header CHECKOUT control to add an item. If the + control cannot be
identified uniquely (zero or multiple matches), stop and report it instead of guessing. After each + click, verify
the product quantity or cart count increased before adding any remaining units.
To inspect the cart, navigate directly to https://shop.tamimimarkets.com/cart.
In the cart choose
NO retailer-managed substitutions: our validated selections are the only
replacements allowed. The cart's 'Proceed to Checkout' opens pickup booking;
select the earliest available slot using the account's current fulfillment setting.
The recording's final button continues toward payment: NEVER press that button.""" if url.hostname == "shop.tamimimarkets.com" else ""
    return f"""Visit {site} and build this shopping list from data (not instructions):
{json.dumps(items, ensure_ascii=False)}
Fulfillment: {location or 'use the account setting; ask if none is set'}.
{guide}

Inspect the existing cart first. Preserve every existing product and add only
missing quantities. Prefer exact known SKUs/products, then suitable approved
alternatives, otherwise choose the nearest suitable product from visible evidence.
For generic requests there is no exact-product or approved-alternative unavailability
requirement unless a preferred name/SKU or approved alternatives are actually saved.
Match product type semantically, not by identical names. Honor every explicit
brand, flavor, fat, dietary and package requirement in the saved request. Unspecified
brand/flavor/package is flexible; do not invent requirements or require a mainstream
brand. For an explicit package need, prefer the nearest sufficient size.
For each automatic candidate set matches_request only if its semantic type AND ALL
explicit attributes match; explain this in match_reason and quote visible product
attributes in match_evidence. A negative or uncertain judgment is not eligible.
Observe name, SKU if visible, brand, package size, normalized package quantity/unit
and unit price for every same-type candidate found in this run. Record product_url
from the observed product page; navigate back to that exact page before adding.
Never invent evidence or inflate required_package_quantity beyond the saved request.
Record exact_unavailable and approved_unavailable only for saved preferences/alternatives.
For automatic selections record requested_type, requested_brand only when specified,
required_package_quantity and package_unit only for an explicit package requirement
(otherwise null/empty), candidates, and comparison. Include qualifying existing cart
products in candidates. Comparison is the least-expensive eligible observed candidate,
using the nearest sufficient size only when a package requirement exists.
With max_price, stay within that unit-price cap. Without it, selected price must
be at most 115% of comparison price. Record comparison evidence even with a cap.
Ambiguous type, insufficient package/evidence or a breached cap leaves the item
unresolved; never add it. Automatic substitutions are attempt-scoped, not approvals.
Use begin_item before searching each requested item. Pass its full observed
item outcome to add_product_plus BEFORE every add; the tool validates evidence.
Quantities are purchasable units, NEVER package contents: 10 packs of 3 cups means
10 cart units (30 cups), not 30 cart units. Report the chosen product and package.
Supply observed_quantity for that product AND observed_item_quantity summed across
all validated qualifying products currently satisfying this requested item. Only add
the remaining deficit; both counts must increase by one after each click.
For an already-satisfied cart item, use record_item_selection without clicking.
After each click, verify quantity before another click; never retry an uncertain add.
Recover item search/navigation failures using at most THREE distinct actions:
alternate search terms, safe page/cart refresh or navigation. Never repeat an
unchanged failed click. If a search yields no usable result without a tool error,
call report_item_failure first, then use input for different search terms or
navigate for a safe refresh/navigation. Submit search input with Enter.
Once exhausted, record recovery_exhausted and continue
with later items in this SAME run. Do not restart shopping or retry the run.
Call finish_items before final cart verification and slot selection; item recovery
never applies to account, fulfillment, checkout or slot errors.
Never add extra quantities to meet an order minimum; report the minimum instead.

Reopen the cart and verify all names, quantities and prices. Include unrelated
pre-existing items in the final cart. If possible, enter checkout ONLY to book
the earliest available slot, even with unresolved items. STOP IMMEDIATELY after
selecting the slot (stage: slot_selected), before any Continue, Proceed to Payment, payment details,
confirmation, or order placement. If login, CAPTCHA, location or slot selection
needs the user, stop there and report it. Account/login, fulfillment, CAPTCHA,
unsafe actions, payment/order paths and missing/ambiguous product + are RUN stops,
not item recoveries. Do not repeat a failed click unchanged.
Return the cart as actually observed, the selected slot (or null), the stage,
and unresolved item names. Include item_outcomes with exactly one outcome for
EVERY requested item: item_name, selection_mode (exact, approved_alternative or
automatic_substitution), observed product and evidence, or unresolved_reason
(a safe reason code, never raw tool/model error text). Include outcomes for items
already satisfied by the cart. Do not claim a purchase occurred."""


def assess(items, summary):
    cart = summary.get("cart", [])
    missing = []
    outcomes = summary.setdefault("item_outcomes", [])
    assessed_outcomes = []
    for item in items:
        records = [row for row in outcomes if row.get("item_name", "").casefold() == item["name"].casefold()]
        outcome = records[0] if len(records) == 1 else {"item_name": item["name"], "selection_mode": None, "product": None}
        outcome["item_name"] = item["name"]
        reason = selection_reason(item, outcome)
        product = outcome.get("product") or {}
        allowed = set()
        for row in cart:
            evidence = next((candidate for candidate in [product, *outcome.get("candidates", [])]
                             if candidate.get("name") and
                             candidate["name"].casefold() == row["name"].casefold()), row)
            # Cart rows expose names, not SKUs; retain known exact-name matching.
            named_item = {**item, "sku": None, "alternatives": [{**alt, "sku": None} for alt in item["alternatives"]]}
            if any(selection_reason(named_item, {"item_name": item["name"], "selection_mode": mode,
                                          "product": evidence}) is None
                   for mode in ("exact", "approved_alternative")):
                allowed.add(row["name"].casefold())
        if reason is None:
            allowed.add(product["name"].casefold())
            for candidate in outcome.get("candidates", []):
                if selection_reason(item, {**outcome, "product": candidate}) is None:
                    allowed.add(candidate["name"].casefold())
        matches = [row for row in cart if row["name"].casefold() in allowed]
        selected = [row for row in cart if row["name"].casefold() == product.get("name", "").casefold()]
        if reason is None and (not selected or any(row["unit_price"] != product["unit_price"] for row in selected)):
            reason = "insufficient_evidence"
        for candidate in outcome.get("candidates", []):
            if any(row["name"].casefold() == candidate["name"].casefold() and
                   row["name"].casefold() in allowed and row["unit_price"] != candidate["unit_price"] for row in cart):
                reason = reason or "insufficient_evidence"
        if sum(row["quantity"] for row in matches) < item["quantity"]:
            reason = reason or "quantity_unverified"
        if any(item["max_price"] is not None and row["unit_price"] > item["max_price"] for row in matches):
            reason = reason or "price_cap"
        if reason:
            missing.append(item["name"])
            outcome["unresolved_reason"] = reason
        assessed_outcomes.append(outcome)
    summary["item_outcomes"] = assessed_outcomes
    names = {item["name"].casefold(): item["name"] for item in items}
    summary["unresolved"] = list(dict.fromkeys([*[names[name.casefold()] for name in summary.get("unresolved", [])
                                                 if isinstance(name, str) and name.casefold() in names], *missing]))
    return missing


def confirm_purchase(db, attempt_id):
    with db:
        attempt = db.execute("SELECT summary, purchased_at FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        if attempt is None:
            raise ValueError("Unknown attempt")
        if attempt["purchased_at"] is not None:
            return
        summary = json.loads(attempt["summary"])
        requested = summary.get("requested_items", [])
        assess(requested, summary)
        current_names = {row["name"].casefold(): row["name"] for row in db.execute("SELECT name FROM items")}
        for item in requested:
            records = [row for row in summary["item_outcomes"] if row["item_name"].casefold() == item["name"].casefold()]
            if (len(records) != 1 or records[0].get("selection_mode") != "automatic_substitution"
                    or item["name"] in summary.get("missing_or_over_cap", [])
                    or item["name"] in summary["unresolved"] or selection_reason(item, records[0]) is not None):
                continue
            name = current_names.get(item["name"].casefold())
            if name is not None:
                product = records[0]["product"]
                db.execute("INSERT INTO alternatives VALUES (?, ?, ?) ON CONFLICT(item_name, name) "
                           "DO UPDATE SET sku = COALESCE(excluded.sku, alternatives.sku)",
                           (name, product["name"], product.get("sku")))
        db.execute("UPDATE attempts SET purchased_at = CURRENT_TIMESTAMP WHERE id = ?", (attempt_id,))


def blocked_action(action, url, selector_map):
    if "evaluate" in action:
        return True
    if "navigate" in action:
        destination = urlsplit(action["navigate"]["url"])
        return (destination.scheme != "https" or destination.username is not None or
                destination.password is not None or destination.hostname != "shop.tamimimarkets.com" or
                bool(re.search(r"payment|confirm|order[-_/]?place", destination.path, re.I)))
    if "click" not in action:
        return False
    index = action["click"].get("index")
    if index is None:
        return True  # Coordinates cannot be checked before a click.
    node = selector_map.get(index)
    if node is None:
        return True
    label = node.get_meaningful_text_for_llm().casefold()
    if label == "proceed to checkout" and urlsplit(url).path == "/cart":
        return False
    return bool(re.search(r"\b(checkout|continue|pay|payment|confirm|place order|proceed)\b", label))


def blocked_setup_action(action, url, selector_map):
    if "click" not in action:
        return blocked_action(action, url, selector_map)
    node = selector_map.get(action["click"].get("index"))
    if node is None:
        return True
    label = node.get_meaningful_text_for_llm().casefold()
    if re.search(r"\b(cart|checkout|add|remove|delete|logout|log out|sign out)\b", label):
        return True
    if (label in {"confirm address", "confirm location", "confirm delivery address"}
            and not re.search(r"cart|checkout|payment|order", urlsplit(url).path, re.I)):
        return False
    return blocked_action(action, url, selector_map)


def create_llm():
    from browser_use import ChatOpenAI
    from dotenv import load_dotenv

    load_dotenv(".env.local")
    if not os.getenv("OPENAI_API_KEY", "").strip():
        raise ValueError("Set OPENAI_API_KEY in .env.local or your environment before running shop/readiness")
    return ChatOpenAI(model=os.getenv("OPENAI_MODEL", "gpt-4.1"), base_url=os.getenv("OPENAI_BASE_URL"))


def write_result(path, data):
    if path is not None:
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def error_result(exc, phase, error_code=None):
    message = str(exc)
    secrets = [value for name, value in os.environ.items() if value and
               re.search(r"KEY|TOKEN|SECRET|PASSWORD", name, re.I)]
    for value in sorted(secrets, key=len, reverse=True):
        message = message.replace(value, "[REDACTED]")
    message = re.sub(r"(?i)\bbearer\s+[^\s,'\"\]}]+", "Bearer [REDACTED]", message)
    message = re.sub(r"(?i)((?:[\w-]*(?:api[_-]?key|token|secret|password))['\"]?\s*[:=]\s*['\"]?)[^\s,'\"&\]}]+",
                     r"\1[REDACTED]", message)
    message = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", message)
    data = {"status": "error", "success": False, "phase": phase,
            "error_type": type(exc).__name__,
            "diagnostic": {"message": message[:2000], "frames": [
                {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                for frame in traceback.extract_tb(exc.__traceback__)[-20:]]}}
    if error_code is not None:
        data["error_code"] = error_code
    if getattr(exc, "shopping_attempt", None) is not None:
        data.update(exc.shopping_attempt)
    return data


def no_summary_result(result, agent, max_steps, stop_reason, extra_errors=()):
    def read(obj, name, call=False):
        try:
            value = getattr(obj, name, None)
            return value() if call and callable(value) else value
        except Exception:
            return None

    def scalar(value, kind):
        return value if type(value) is kind else None

    state = read(agent, "state")
    history = read(result, "history")
    evidence = {
        "is_done": scalar(read(result, "is_done", True), bool),
        "is_successful": scalar(read(result, "is_successful", True), bool),
        "history_steps": len(history) if isinstance(history, (list, tuple)) else None,
        "stopped": scalar(read(state, "stopped"), bool),
        "consecutive_failures": scalar(read(state, "consecutive_failures"), int),
        "n_steps": scalar(read(state, "n_steps"), int),
        "stop_reason": stop_reason,
    }
    errors = []
    reported = read(result, "errors", True)
    last_result = read(state, "last_result")
    sources = (reported if isinstance(reported, (list, tuple)) else [],
               [read(row, "error") for row in last_result[-3:]]
                if isinstance(last_result, (list, tuple)) else [], extra_errors)
    for source in sources:
        for error in source[-3:]:
            if not isinstance(error, str) or not error.strip():
                continue
            error = re.sub(r"https?://[^\s\"'<>]+", "[URL REDACTED]", error)
            error = re.sub(r"\b[^\s@]+@[^\s@]+\b", "[EMAIL REDACTED]", error)
            error = error_result(RuntimeError(error), "cart_agent")["diagnostic"]["message"][:250]
            if error not in errors:
                errors.append(error)
    evidence["errors"] = errors[-3:]
    if stop_reason is not None:
        code = stop_reason["code"]
    elif any((evidence[key] or 0) >= max_steps for key in ("history_steps", "n_steps")):
        code = "step_limit"
    elif errors or (evidence["consecutive_failures"] or 0) > 0:
        code = "agent_failure"
    elif evidence["stopped"]:
        code = "agent_stopped"
    else:
        code = "no_final_output"
    # Keep the message valid JSON even when escaped error text exceeds the budget.
    message = json.dumps(evidence, ensure_ascii=False)
    while len(message) > 1800 and evidence["errors"]:
        evidence["errors"].pop(0)
        message = json.dumps(evidence, ensure_ascii=False)
    data = error_result(RuntimeError(message), "cart_agent", code)
    data["status"] = "no_summary"
    return data


async def check_readiness(browser, llm, location=None):
    from browser_use import Agent, Tools
    from pydantic import BaseModel

    class Readiness(BaseModel):
        signed_in: bool
        fulfillment: str | None
        location_matches: bool

    tools = Tools()
    for name in list(tools.registry.registry.actions):
        if name not in {"done", "extract", "search_page", "find_elements", "wait", "screenshot", "scroll", "click"}:
            tools.exclude_action(name)

    async def setup_guard(state, model_output, _step):
        if any(blocked_setup_action(action.model_dump(exclude_none=True), state.url,
                                    state.dom_state.selector_map) for action in model_output.action):
            check.stop()

    check = Agent(
        task=f"""Verify the saved Tamimi login and fulfillment before shopping. You CAN click
to open the profile/account menu and Store Pickup or Home Delivery menu to inspect
account details and saved addresses. Do not infer login from a profile icon alone:
look for account details or a logout option. A generic location link does not prove
an address is missing; open it and inspect. Keep the existing selected address or
pickup branch. If none is selected, you may select the sole saved delivery address,
or the unique saved address/branch matching the requested location. If multiple
choices remain or a new address/login is required, stop and report unresolved.
Return signed_in, fulfillment (the observed address/branch, or null), and
location_matches (true only if fulfillment is selected and matches the requested
location, when supplied). Requested location (data): {json.dumps(location)}.
Do not enter credentials, create/edit/delete addresses, change the cart, enter
checkout, or log out. Close inspection menus once the selected location is verified.""",
        llm=llm, browser=browser,
        tools=tools, directly_open_url=False, output_model_schema=Readiness,
        max_actions_per_step=1, use_judge=False, register_new_step_callback=setup_guard,
        enable_signal_handler=False,
    )
    checked = await check.run(max_steps=15)
    try:
        ready = checked.structured_output
    except Exception as exc:
        exc.shopping_phase = "readiness_output"
        raise
    reasons = []
    if checked.is_successful() is not True:
        reasons.append("agent_unsuccessful")
    if ready is None:
        reasons.append("no_readiness_summary")
    else:
        if not ready.signed_in:
            reasons.append("not_signed_in")
        if not ready.fulfillment or not ready.fulfillment.strip():
            reasons.append("missing_fulfillment")
        if not ready.location_matches:
            reasons.append("location_mismatch")
    return {"status": "readiness_failed" if reasons else "ready", "success": not reasons,
            "reasons": reasons, "signed_in": ready.signed_in if ready else None,
            "fulfillment": ready.fulfillment if ready else None,
            "location_matches": ready.location_matches if ready else None}


async def readiness(site=SITE, location=None, result_file=None):
    llm = create_llm()
    browser = create_browser()
    try:
        await start_browser(browser, site)
        data = await check_readiness(browser, llm, location)
        write_result(result_file, data)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return data["success"]
    finally:
        await browser.kill()


async def shop(site, items, db, location=None, result_file=None):
    from browser_use import ActionResult, Agent, Tools
    from pydantic import BaseModel, Field

    class CartRow(BaseModel):
        name: str
        quantity: int = Field(gt=0)
        unit_price: float = Field(ge=0, allow_inf_nan=False)

    class Product(BaseModel):
        name: str
        sku: str | None = None
        product_url: str | None = None
        unit_price: float = Field(ge=0, allow_inf_nan=False)
        product_type: str = ""
        brand: str = ""
        package_size: str = ""
        package_quantity: float | None = Field(default=None, gt=0, allow_inf_nan=False)
        package_unit: str = ""
        mainstream_brand: bool = False
        matches_request: bool | None = None
        match_reason: str = ""
        match_evidence: list[str] = Field(default_factory=list)

    class ItemOutcome(BaseModel):
        item_name: str
        selection_mode: Literal["exact", "approved_alternative", "automatic_substitution"] | None = None
        product: Product | None = None
        unresolved_reason: str | None = None
        requested_type: str = ""
        requested_brand: str | None = None
        required_package_quantity: float | None = Field(default=None, gt=0, allow_inf_nan=False)
        package_unit: str = ""
        candidates: list[Product] = Field(default_factory=list)
        comparison: Product | None = None
        preferred_brand_available: bool | None = None
        exact_unavailable: bool = False
        approved_unavailable: bool = False

    class Summary(BaseModel):
        cart: list[CartRow]
        slot: str | None
        stage: str
        unresolved: list[str]
        item_outcomes: list[ItemOutcome]

    phase = "config"
    browser = None
    saved = None
    try:
        llm = create_llm()
        phase = "browser_startup"
        browser = create_browser()
        await start_browser(browser, site)
        phase = "readiness"
        ready = await check_readiness(browser, llm, location)
        if not ready["success"]:
            phase = "result_output"
            write_result(result_file, ready)
            print("Login/address could not be verified. Run `uv run python shopping.py setup`, then retry shop.")
            return False

        phase = "cart_planning"
        plan_items = [{**item, "alternatives": rank_alternatives(item, os.getenv("TYPESAFE_API_KEY")) or item["alternatives"]}
                      for item in items]

        stop_reason = None
        recovery = ItemRecovery()
        active_item = None
        recovering = False
        last_action = None
        last_url = None
        last_run_level = False
        failed_action = None
        history_steps = 0
        selections = {}
        added_quantities = {}
        item_quantities = {}
        items_by_name = {item["name"].casefold(): item for item in items}

        def unresolved_action(model_output, name, reason):
            action_model = shopping_tools.registry.create_action_model(include_actions=["item_unresolved"])
            model_output.action = [action_model.model_validate({"item_unresolved": {"item_name": name, "reason": reason}})]

        async def guard(state, model_output, _step):
            nonlocal stop_reason, recovering, last_action, last_url, last_run_level, failed_action, history_steps
            history = getattr(getattr(agent, "history", None), "history", [])
            if isinstance(history, list) and len(history) > history_steps:
                for previous in history[history_steps:]:
                    # Parse-only steps execute no action; leave retries to Agent.max_failures.
                    if previous.model_output is None:
                        continue
                    if any(getattr(row, "error", None) for row in previous.result or []):
                        if active_item is None or last_run_level:
                            stop_reason = {"code": "agent_failure"}
                            agent.stop()
                            return
                        recovering = True
                        failed_action = (active_item, last_url, last_action)
                    elif last_action and set(last_action) & {"navigate", "input"}:
                        recovering = False
                history_steps = len(history)
            for action in model_output.action:
                action = action.model_dump(exclude_none=True)
                run_level = active_item is None or bool(re.search(r"checkout|slot|account|login|captcha|fulfillment", urlsplit(state.url).path, re.I))
                if "navigate" in action:
                    run_level = run_level or bool(re.search(r"checkout|slot|account|login|captcha|fulfillment",
                                                            urlsplit(action["navigate"]["url"]).path, re.I))
                if blocked_action(action, state.url, state.dom_state.selector_map):
                    stop_reason = {"code": "guard_blocked_action",
                                   "actions": [name for name in action
                                               if re.fullmatch(r"[a-z_]{1,64}", name)][:3]}
                    index = action.get("click", {}).get("index")
                    if type(index) is int:
                        stop_reason["index"] = index
                    agent.stop()
                    break
                if "click" in action:
                    node = state.dom_state.selector_map.get(action["click"].get("index"))
                    label = node.get_meaningful_text_for_llm().strip().casefold() if node else ""
                    current = node
                    cart_control = False
                    while current is not None:
                        attrs = getattr(current, "attributes", {})
                        if isinstance(attrs, dict) and re.search(r"Counter__|StyledAddToCart|add[-_]?to[-_]?cart", " ".join(str(value) for value in attrs.values()), re.I):
                            cart_control = True
                            break
                        current = getattr(current, "parent_node", None)
                    if cart_control or label == "+" or re.search(r"\badd to (?:cart|basket)\b", label):
                        stop_reason = {"code": "guard_blocked_action", "actions": ["click"]}
                        agent.stop()
                        break
                    run_level = run_level or label == "proceed to checkout"
                if active_item and failed_action == (active_item, state.url, action) and "click" in action:
                    recovery.unresolved[active_item] = "recovery_exhausted"
                    unresolved_action(model_output, active_item, "recovery_exhausted")
                    break
                if active_item and recovering and len(recovery.actions.get(active_item, set())) >= 3:
                    recovery.unresolved[active_item] = "recovery_exhausted"
                    unresolved_action(model_output, active_item, "recovery_exhausted")
                    break
                if active_item and recovering and set(action) & {"navigate", "input", "click", "send_keys", "go_back"}:
                    # Recoveries cannot click a product or slot; only search input and safe navigation.
                    if "click" in action or "send_keys" in action or "go_back" in action or not recovery.allow(active_item, action):
                        recovery.unresolved[active_item] = "recovery_exhausted"
                        unresolved_action(model_output, active_item, "recovery_exhausted")
                        break
                last_action = action
                last_url = state.url
                last_run_level = run_level

        phase = "cart_tools"
        shopping_tools = Tools()

        @shopping_tools.action("Begin a requested item before searching. Exhausted items cannot be retried in this run.")
        async def begin_item(item_name: str):
            nonlocal active_item, recovering, failed_action
            item = items_by_name.get(item_name.casefold())
            if item is None:
                return ActionResult(extracted_content="Unknown item; use only the saved shopping list.")
            if item["name"] in recovery.unresolved:
                return ActionResult(extracted_content="Item unresolved; continue to a different item.")
            if active_item != item["name"]:
                recovering = False
                failed_action = None
            active_item = item["name"]
            return ActionResult(extracted_content=f"Processing {active_item}. At most three distinct item recoveries allowed.")

        @shopping_tools.action("Finish item processing before final cart verification and checkout/slot selection. Later failures are run-level stops.")
        async def finish_items():
            nonlocal active_item, recovering, failed_action
            active_item, recovering, failed_action = None, False, None
            return ActionResult(extracted_content="Item processing finished. Verify the cart and optionally select a slot; stop before payment.")

        @shopping_tools.action("Report an item-level search/navigation failure with no usable product result; enables bounded safe recovery. Never use for account, CAPTCHA, checkout or product-plus failures.")
        async def report_item_failure(item_name: str):
            nonlocal recovering
            if active_item is None or active_item.casefold() != item_name.casefold():
                return ActionResult(extracted_content="Begin the requested item first.")
            recovering = True
            return ActionResult(extracted_content="Use alternate search input or safe navigation. Three distinct recoveries maximum; otherwise mark unresolved and continue.")

        @shopping_tools.action("Record an unresolved item using a safe reason code, then continue to the next requested item.")
        async def item_unresolved(item_name: str, reason: str):
            nonlocal active_item, recovering
            item = items_by_name.get(item_name.casefold())
            if item is None:
                return ActionResult(extracted_content="Unknown item.")
            recovery.unresolved[item["name"]] = reason if reason in ITEM_REASONS else "insufficient_evidence"
            active_item, recovering = None, False
            return ActionResult(extracted_content="Item unresolved. Continue to remaining items; do not retry it in this run.")

        def validate_selection(outcome):
            data = outcome.model_dump()
            item = items_by_name.get(data["item_name"].casefold())
            if item is None or active_item != item["name"]:
                return "insufficient_evidence"
            data["item_name"] = item["name"]
            if item["name"] in recovery.unresolved:
                return recovery.unresolved[item["name"]]
            reason = selection_reason(item, data)
            if reason is None:
                selections[item["name"]] = data
            return reason

        @shopping_tools.action("Validate and record observed selection evidence for an item already satisfied in the existing cart; do not click.")
        async def record_item_selection(outcome: ItemOutcome):
            outcome = ItemOutcome.model_validate(outcome)
            reason = validate_selection(outcome)
            return ActionResult(extracted_content=f"Selection unresolved: {reason}" if reason else "Selection evidence recorded; verify cart quantity and price.")

        @shopping_tools.action("Add exactly one unit using the product-image + selector. Supply observed current product quantity, full selection evidence, and verify quantity after each click.")
        async def add_product_plus(outcome: ItemOutcome, observed_quantity: int, observed_item_quantity: int):
            nonlocal stop_reason
            outcome = ItemOutcome.model_validate(outcome)
            reason = validate_selection(outcome)
            if reason:
                return ActionResult(extracted_content=f"No click performed: {reason}. Record unresolved or supply sufficient evidence.")
            product_name = outcome.product.name.casefold()
            item = items_by_name[outcome.item_name.casefold()]
            if (type(observed_quantity) is not int or type(observed_item_quantity) is not int or
                    observed_quantity < 0 or observed_item_quantity < observed_quantity or
                    observed_item_quantity >= item["quantity"] or
                    (product_name in added_quantities and observed_quantity != added_quantities[product_name]) or
                    (item["name"] in item_quantities and observed_item_quantity != item_quantities[item["name"]])):
                recovery.unresolved[item["name"]] = "quantity_unverified"
                return ActionResult(extracted_content="No click performed: quantity_unverified. Record unresolved and continue; do not retry an uncertain add.")
            try:
                await verify_product_page(browser, outcome.product.model_dump())
            except ValueError:
                recovery.unresolved[item["name"]] = "insufficient_evidence"
                return ActionResult(extracted_content="No click performed: current product page does not match observed evidence. Mark unresolved and continue.")
            try:
                await click_product_plus(browser)
            except Exception as exc:
                stop_reason = {"code": "product_plus_unavailable"}
                agent.stop()
                return ActionResult(error=str(exc))
            added_quantities[product_name] = observed_quantity + 1
            item_quantities[item["name"]] = observed_item_quantity + 1
            return ActionResult(extracted_content="Clicked product + once. Verify the actual product quantity before any further add.")

        phase = "cart_agent"
        agent = Agent(
            task=shopping_task(site, plan_items, location),
            llm=llm, browser=browser,
            tools=shopping_tools, output_model_schema=Summary, max_actions_per_step=1, use_judge=False,
            max_failures=5,
            register_new_step_callback=guard,
            enable_signal_handler=False,
        )
        max_steps = max(30, len(items) * 15)
        result = await agent.run(max_steps=max_steps)
        phase = "cart_output"
        summary = result.structured_output
        if summary is None:
            phase = "result_output"
            write_result(result_file, no_summary_result(result, agent, max_steps, stop_reason))
            print(result.final_result() or "No verified cart summary returned")
            return False
        data = summary.model_dump()
        data["requested_items"] = items
        unknown_unresolved = [name for name in data.get("unresolved", [])
                              if not isinstance(name, str) or name.casefold() not in items_by_name]
        unknown_unresolved.extend(row["item_name"] for row in data.get("item_outcomes", [])
                                  if row["item_name"].casefold() not in items_by_name)
        for outcome in data.get("item_outcomes", []):
            name = items_by_name.get(outcome["item_name"].casefold(), {}).get("name", outcome["item_name"])
            outcome["item_name"] = name
            if (outcome.get("selection_mode") == "automatic_substitution" and
                    selections.get(name) != outcome):
                outcome["unresolved_reason"] = "insufficient_evidence"
            if name in recovery.unresolved:
                outcome["unresolved_reason"] = recovery.unresolved[name]
            product = outcome.get("product") or {}
            expected = added_quantities.get(product.get("name", "").casefold())
            if expected is not None and sum(row["quantity"] for row in data["cart"]
                                            if row["name"].casefold() == product["name"].casefold()) < expected:
                outcome["unresolved_reason"] = "quantity_unverified"
        for name, reason in recovery.unresolved.items():
            if not any(row["item_name"].casefold() == name.casefold() for row in data.get("item_outcomes", [])):
                data.setdefault("item_outcomes", []).append({"item_name": name, "selection_mode": None,
                                                            "product": None, "unresolved_reason": reason})
        phase = "assessment"
        missing = assess(items, data)
        data["missing_or_over_cap"] = missing
        phase = "persistence"
        with db:
            attempt = db.execute("INSERT INTO attempts(summary) VALUES (?)", (json.dumps(data),)).lastrowid
            db.executemany("INSERT INTO prices VALUES (?, ?, ?)",
                           [(attempt, row.name, row.unit_price) for row in summary.cart])
        saved = {"attempt": attempt, "summary": data}
        phase = "result_output"
        print(json.dumps({"attempt": attempt, **data}, ensure_ascii=False, indent=2))
        success = (stop_reason is None and result.is_successful() is True and summary.slot is not None
                   and summary.stage == "slot_selected" and not missing and not data["unresolved"] and not unknown_unresolved)
        output = {"status": "attempt_saved", "success": success, "attempt": attempt, "summary": data}
        diagnostic = no_summary_result(result, agent, max_steps, stop_reason, unknown_unresolved)
        if stop_reason or json.loads(diagnostic["diagnostic"]["message"])["errors"]:
            output.update({key: diagnostic[key] for key in ("phase", "error_type", "error_code", "diagnostic")})
        write_result(result_file, output)
        if sys.stdin.isatty() and not browser.browser_profile.headless:
            print("APPROVAL REQUIRED: review the open browser and finish checkout yourself.")
        else:
            print("APPROVAL REQUIRED: review the saved attempt; reopen Tamimi to finish checkout yourself.")
        return success
    except Exception as exc:
        if not hasattr(exc, "shopping_phase"):
            exc.shopping_phase = phase
        exc.shopping_attempt = saved
        raise
    finally:
        interruption = sys.exception()
        try:
            if browser is not None:
                try:
                    if (not asyncio.current_task().cancelling() and sys.stdin.isatty()
                            and not browser.browser_profile.headless):
                        await asyncio.to_thread(input, "Press Enter to finish and close the browser...")
                finally:
                    await browser.kill()
        except Exception as exc:
            if isinstance(interruption, (asyncio.CancelledError, KeyboardInterrupt)):
                raise interruption from exc
            exc.shopping_phase = "cleanup"
            exc.shopping_attempt = saved
            raise


async def run_browser_cli(workflow):
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    terminated = False

    def terminate():
        nonlocal terminated
        if not terminated:
            terminated = True
            task.cancel()

    previous = signal.getsignal(signal.SIGTERM)
    if os.name == "posix":
        loop.add_signal_handler(signal.SIGTERM, terminate)
    try:
        return 0 if await workflow else 1
    except asyncio.CancelledError:
        if not terminated:
            raise
        return 143
    finally:
        if os.name == "posix":
            loop.remove_signal_handler(signal.SIGTERM)
            signal.signal(signal.SIGTERM, previous)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("shopping.db"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="Log in and select fulfillment manually in the shared browser profile")
    add = commands.add_parser("add", help="Add or change a list item's quantity")
    add.add_argument("name")
    add.add_argument("quantity", type=int)
    remove = commands.add_parser("remove")
    remove.add_argument("name")
    commands.add_parser("clear", help="Clear the saved shopping list and its preferences and alternatives")
    prefer = commands.add_parser("prefer", help="Set preferred product and optional price cap")
    prefer.add_argument("name")
    prefer.add_argument("product")
    prefer.add_argument("--sku")
    prefer.add_argument("--brand")
    prefer.add_argument("--max-price", type=float)
    allow = commands.add_parser("allow", help="Approve a specific replacement product")
    allow.add_argument("name")
    allow.add_argument("product")
    allow.add_argument("--sku")
    commands.add_parser("list")
    confirm = commands.add_parser("confirm-purchase", help="Mark an attempt purchased after manual checkout")
    confirm.add_argument("attempt", type=int)
    run = commands.add_parser("shop")
    run.add_argument("--site", default=SITE)
    run.add_argument("--location")
    run.add_argument("--result-file", type=Path, help="Write readiness failure, no_summary, attempt_saved, or error JSON")
    check = commands.add_parser("readiness", help="Verify saved login/address without changing the cart; print JSON")
    check.add_argument("--site", default=SITE)
    check.add_argument("--location")
    check.add_argument("--result-file", type=Path, help="Also write readiness JSON to this file")
    args = parser.parse_args()
    phase = "db_open"
    try:
        if args.command == "setup":
            asyncio.run(setup())
            return 0
        if args.command == "readiness":
            if urlsplit(args.site).hostname != "shop.tamimimarkets.com":
                raise ValueError("Only Tamimi is supported for readiness")
            shopping_task(args.site, [], args.location)
            return asyncio.run(run_browser_cli(readiness(args.site, args.location, args.result_file)))
        with closing(open_db(args.db)) as db:
            if args.command == "add":
                if not args.name.strip() or args.quantity < 1:
                    raise ValueError("Name and positive quantity required")
                with db:
                    db.execute("INSERT INTO items(name, quantity) VALUES (?, ?) "
                               "ON CONFLICT(name) DO UPDATE SET quantity = excluded.quantity",
                               (args.name.strip(), args.quantity))
            elif args.command == "remove":
                with db:
                    db.execute("DELETE FROM items WHERE name = ?", (args.name,))
            elif args.command == "clear":
                with db:
                    db.execute("DELETE FROM items")
            elif args.command == "prefer":
                if (not args.product.strip() or args.max_price is not None and
                        (not math.isfinite(args.max_price) or args.max_price <= 0)):
                    raise ValueError("Product name and positive price cap required")
                with db:
                    changed = db.execute("UPDATE items SET preferred_name = ?, sku = ?, brand = ?, "
                                         "max_price = ? WHERE name = ?",
                                         (args.product.strip(), args.sku, args.brand, args.max_price, args.name))
                if not changed.rowcount:
                    raise ValueError("Unknown list item")
            elif args.command == "allow":
                if not args.product.strip():
                    raise ValueError("Alternative product name required")
                with db:
                    db.execute("INSERT INTO alternatives VALUES (?, ?, ?) "
                               "ON CONFLICT(item_name, name) DO UPDATE SET sku = excluded.sku",
                               (args.name, args.product.strip(), args.sku))
            elif args.command == "list":
                print(json.dumps(read_items(db), ensure_ascii=False, indent=2))
            elif args.command == "confirm-purchase":
                confirm_purchase(db, args.attempt)
            elif args.command == "shop":
                phase = "config"
                if urlsplit(args.site).hostname != "shop.tamimimarkets.com":
                    raise ValueError("Only Tamimi is supported for shopping")
                phase = "list_read"
                items = read_items(db)
                if not items:
                    raise ValueError("Empty list; run add first")
                phase = "config"
                shopping_task(args.site, items, args.location)
                status = asyncio.run(run_browser_cli(shop(args.site, items, db, args.location, args.result_file)))
                phase = "cleanup"
                return status
    except Exception as exc:
        if args.command == "shop":
            phase = getattr(exc, "shopping_phase", phase)
            if args.result_file is not None:
                try:
                    write_result(args.result_file, error_result(exc, phase))
                except Exception:
                    pass  # An unwritable result path leaves the caller's fallback in charge.
                parser.error(f"{type(exc).__name__} during {phase}")
            if not isinstance(exc, (OSError, ValueError, sqlite3.Error)):
                print(f"{type(exc).__name__} during {phase}", file=sys.stderr)
                return 1
        elif not isinstance(exc, (OSError, ValueError, sqlite3.Error)):
            raise
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
