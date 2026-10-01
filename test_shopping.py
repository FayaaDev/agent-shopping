import asyncio
import copy
import json
import os
import signal
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from shopping import ItemRecovery, PRODUCT_PLUS_SELECTOR, SITE, assess, blocked_action, blocked_setup_action, check_readiness, click_product_plus, confirm_purchase, create_browser, error_result, main, open_db, rank_alternatives, read_items, readiness, selection_reason, setup, shop, shopping_task


def substitution():
    item = {"name": "milk 1 L", "preferred_name": "Preferred milk 1 L", "sku": "preferred",
            "brand": None, "quantity": 2, "max_price": None, "alternatives": []}
    product = {"name": "Nadec milk 1 L", "sku": "123", "product_type": "fresh full fat milk",
               "brand": "Nadec", "package_size": "1 L", "package_quantity": 1000,
               "package_unit": "ml", "unit_price": 11.5, "mainstream_brand": True,
               "matches_request": True, "match_reason": "Milk in the requested 1 L package.",
               "match_evidence": ["milk", "1 L"]}
    comparison = {**product, "name": "Other milk 1 L", "sku": "456", "brand": "Other", "unit_price": 10}
    outcome = {"item_name": item["name"], "selection_mode": "automatic_substitution", "product": product,
               "unresolved_reason": None, "requested_type": "fresh full fat milk", "requested_brand": None,
               "required_package_quantity": 1000, "package_unit": "ml", "candidates": [comparison, product],
               "comparison": comparison, "preferred_brand_available": False,
               "exact_unavailable": True, "approved_unavailable": True}
    return item, outcome


def generic_yogurt():
    item = {"name": "Greek yogurt", "preferred_name": None, "sku": None, "brand": None,
            "quantity": 10, "max_price": None, "alternatives": []}
    product = {"name": "Nada Greek Yogurt Assorted Pack3X160G", "product_type": "assorted Greek yogurt",
               "brand": "Nada", "package_size": "3 x 160 g", "package_quantity": 480,
               "package_unit": "g", "unit_price": 9.95, "matches_request": True,
               "match_reason": "Greek yogurt; no flavor, brand or package specified.",
               "match_evidence": ["Greek Yogurt", "Pack3X160G"]}
    outcome = {"item_name": item["name"], "selection_mode": "automatic_substitution", "product": product,
               "requested_type": "Greek yogurt", "required_package_quantity": None,
               "preferred_brand_available": None, "candidates": [product], "comparison": product}
    return item, outcome


class ShoppingTest(unittest.TestCase):
    def test_generic_yogurt_existing_cart_uses_purchasable_units(self):
        item, outcome = generic_yogurt()
        self.assertIsNone(selection_reason(item, outcome))
        summary = {"cart": [{"name": outcome["product"]["name"], "quantity": 10, "unit_price": 9.95}],
                   "item_outcomes": [outcome], "unresolved": []}
        self.assertEqual(assess([item], summary), [])
        self.assertEqual(item["quantity"], 10)
        self.assertEqual(summary["cart"][0]["quantity"], 10)  # 10 packs, not 10 cups or 30 packs.
        summary["cart"][0]["quantity"] = 9
        self.assertEqual(assess([item], summary), [item["name"]])

    def test_generic_yogurt_requires_evidence_and_honors_explicit_constraints(self):
        for change, reason in (({"matches_request": False}, "ambiguous_type"),
                               ({"match_evidence": ["invented label"]}, "insufficient_evidence"),
                               ({"match_reason": ""}, "insufficient_evidence")):
            item, outcome = generic_yogurt()
            outcome["product"].update(change)
            self.assertEqual(selection_reason(item, outcome), reason)
        for change, reason in (({"brand": "Almarai"}, "no_safe_candidate"),
                               ({"max_price": 9.94}, "price_cap")):
            item, outcome = generic_yogurt()
            item.update(change)
            self.assertEqual(selection_reason(item, outcome), reason)

    def test_generic_yogurt_counts_multiple_qualifying_cart_products(self):
        item, outcome = generic_yogurt()
        other = {**outcome["product"], "name": "Other Greek Yogurt Plain", "brand": "Other",
                 "package_size": "160 g", "package_quantity": 160,
                 "match_evidence": ["Greek Yogurt", "160 g"], "unit_price": 10}
        outcome["candidates"].append(other)
        summary = {"cart": [{"name": outcome["product"]["name"], "quantity": 6, "unit_price": 9.95},
                            {"name": other["name"], "quantity": 4, "unit_price": 10},
                            {"name": "Unrelated milk", "quantity": 20, "unit_price": 5}],
                   "item_outcomes": [outcome], "unresolved": []}
        self.assertEqual(assess([item], summary), [])
        summary["cart"][1]["unit_price"] = 12
        self.assertEqual(assess([item], summary), [item["name"]])

    def test_explicit_flavor_diet_and_package_judgment_cannot_be_negative(self):
        for request in ("plain Greek yogurt", "fat free Greek yogurt", "Greek yogurt 500 g"):
            item, outcome = generic_yogurt()
            item["name"] = outcome["item_name"] = request
            outcome["product"]["matches_request"] = False
            self.assertEqual(selection_reason(item, outcome), "ambiguous_type")
        item, outcome = generic_yogurt()
        item["name"] = outcome["item_name"] = "Greek yogurt 500 g"
        outcome.update(required_package_quantity=500, package_unit="g")
        self.assertEqual(selection_reason(item, outcome), "insufficient_package")
        item["name"] = outcome["item_name"] = "Greek yogurt 3 x 160 g"
        outcome["required_package_quantity"] = 480
        self.assertIsNone(selection_reason(item, outcome))
        outcome["required_package_quantity"] = 160
        self.assertEqual(selection_reason(item, outcome), "insufficient_package")

    def test_generic_yogurt_observed_comparison_cap(self):
        item, outcome = generic_yogurt()
        cheaper = {**outcome["product"], "name": "Other Greek Yogurt Pack3X160G", "unit_price": 8}
        outcome["candidates"].append(cheaper)
        outcome["comparison"] = cheaper
        self.assertEqual(selection_reason(item, outcome), "price_cap")
        item["max_price"] = 10
        self.assertIsNone(selection_reason(item, outcome))

    def test_automatic_substitution_policy(self):
        item, outcome = substitution()
        self.assertIsNone(selection_reason(item, outcome))
        for change, reason in (({"matches_request": False}, "ambiguous_type"),
                               ({"package_quantity": 500}, "insufficient_package"),
                               ({"package_size": ""}, "insufficient_evidence"),
                               ({"unit_price": 11.51}, "price_cap"),
                               ({"unit_price": float("nan")}, "insufficient_evidence")):
            with self.subTest(change=change):
                changed = copy.deepcopy(outcome)
                changed["product"].update(change)
                self.assertEqual(selection_reason(item, changed), reason)
        for key in ("requested_type", "required_package_quantity", "comparison", "candidates", "exact_unavailable"):
            with self.subTest(missing=key):
                changed = copy.deepcopy(outcome)
                del changed[key]
                self.assertIsNotNone(selection_reason(item, changed))
        item["max_price"] = 12
        outcome["product"]["unit_price"] = 12
        self.assertIsNone(selection_reason(item, outcome))  # Explicit cap replaces 15% rule.
        item["max_price"] = 11.99
        self.assertEqual(selection_reason(item, outcome), "price_cap")

    def test_substitution_nearest_size_brand_and_comparison(self):
        item, outcome = substitution()
        larger = {**outcome["product"], "name": "Milk 2 L", "package_size": "2 L", "package_quantity": 2000,
                  "match_evidence": ["Milk", "2 L"]}
        outcome["candidates"].append(larger)
        changed = copy.deepcopy(outcome)
        changed["product"] = changed["candidates"][-1]
        self.assertEqual(selection_reason(item, changed), "no_safe_candidate")
        changed = copy.deepcopy(outcome)
        changed["comparison"] = changed["product"]
        self.assertEqual(selection_reason(item, changed), "insufficient_evidence")
        preferred = {**outcome["product"], "name": "Preferred milk", "brand": "Preferred"}
        outcome["candidates"].append(preferred)
        item["brand"] = "Preferred"
        self.assertEqual(selection_reason(item, outcome), "no_safe_candidate")
        outcome["preferred_brand_available"] = True
        self.assertEqual(selection_reason(item, outcome), "no_safe_candidate")
        outcome["product"] = preferred
        outcome["comparison"] = preferred
        self.assertIsNone(selection_reason(item, outcome))
        outcome["required_package_quantity"] = 1
        self.assertEqual(selection_reason(item, outcome), "insufficient_package")

    def test_substitution_cannot_inflate_saved_package_need(self):
        item, outcome = substitution()
        larger = {**outcome["product"], "name": "Milk 2 L", "package_size": "2 L",
                  "package_quantity": 2000, "unit_price": 11, "match_evidence": ["Milk", "2 L"]}
        outcome["candidates"].append(larger)
        outcome.update(product=larger, comparison=larger, required_package_quantity=2000)
        self.assertEqual(selection_reason(item, outcome), "insufficient_package")
        outcome["required_package_quantity"] = 1000
        outcome["comparison"] = outcome["candidates"][0]
        self.assertEqual(selection_reason(item, outcome), "no_safe_candidate")

    def test_substitution_assessment_requires_every_item_and_observed_price(self):
        item, outcome = substitution()
        summary = {"cart": [{"name": outcome["product"]["name"], "quantity": 2, "unit_price": 11.5}],
                   "item_outcomes": [outcome], "unresolved": []}
        self.assertEqual(assess([item], summary), [])
        for field, value in (("unit_price", 10), ("quantity", 1)):
            changed = copy.deepcopy(summary)
            changed["cart"][0][field] = value
            self.assertEqual(assess([item], changed), [item["name"]])
            self.assertIn(item["name"], changed["unresolved"])
        changed = copy.deepcopy(summary)
        changed["item_outcomes"] = []
        self.assertEqual(assess([item], changed), [item["name"]])
        self.assertEqual(changed["item_outcomes"][0]["unresolved_reason"], "insufficient_evidence")
        changed = copy.deepcopy(summary)
        changed["item_outcomes"].append(copy.deepcopy(outcome))
        self.assertEqual(assess([item], changed), [item["name"]])

    def test_recovery_budget_is_per_item_and_distinct(self):
        budget = ItemRecovery()
        for term in ("milk", "fresh milk", "full fat milk"):
            self.assertTrue(budget.allow("milk", {"input": {"text": term}}))
        self.assertFalse(budget.allow("milk", {"navigate": {"url": SITE}}))
        self.assertTrue(budget.allow("bread", {"input": {"text": "bread"}}))
        self.assertFalse(budget.allow("bread", {"input": {"text": " BREAD ", "index": 9}}))
        self.assertEqual(budget.unresolved, {"milk": "recovery_exhausted", "bread": "recovery_exhausted"})

    def test_confirm_purchase_promotes_only_confirmed_attempt_idempotently(self):
        item, outcome = substitution()
        db = open_db(":memory:")
        self.addCleanup(db.close)
        with db:
            db.execute("INSERT INTO items VALUES (?, ?, ?, ?, ?, ?)",
                       tuple(item[key] for key in ("name", "quantity", "preferred_name", "sku", "brand", "max_price")))
            summary = {"requested_items": [item], "item_outcomes": [outcome], "unresolved": [],
                       "cart": [{"name": outcome["product"]["name"], "quantity": 2, "unit_price": 11.5}]}
            attempt = db.execute("INSERT INTO attempts(summary) VALUES (?)", (json.dumps(summary),)).lastrowid
            other = copy.deepcopy(summary)
            other["item_outcomes"][0]["product"]["unit_price"] = 100
            db.execute("INSERT INTO attempts(summary) VALUES (?)", (json.dumps(other),))
        self.assertEqual(read_items(db)[0]["alternatives"], [])
        confirm_purchase(db, attempt)
        self.assertEqual(read_items(db)[0]["alternatives"], [{"name": "Nadec milk 1 L", "sku": "123"}])
        timestamp = db.execute("SELECT purchased_at FROM attempts WHERE id = ?", (attempt,)).fetchone()[0]
        confirm_purchase(db, attempt)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM alternatives").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT purchased_at FROM attempts WHERE id = ?", (attempt,)).fetchone()[0], timestamp)
        self.assertIsNone(db.execute("SELECT purchased_at FROM attempts WHERE id = 2").fetchone()[0])
        confirm_purchase(db, 2)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM alternatives").fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, "Unknown attempt"):
            confirm_purchase(db, 99)

    def test_mocked_cart_flow_recovery_evidence_and_quantity_guards(self):
        class Action:
            def __init__(self, data):
                self.data = data

            @classmethod
            def model_validate(cls, data):
                return cls(data)

            def model_dump(self, **kwargs):
                return self.data

        for scenario in ("generic_satisfied", "generic_missing", "generic_changed",
                         "recovery", "accepted", "rejected", "fabricated", "quantity", "aggregate", "aggregate_limit", "satisfied",
                         "svg", "svg_ancestor", "scoped_item", "scoped_page", "checkout", "checkout_path",
                         "slot_path", "account_path", "proceed_checkout", "checkout_destination", "no_active", "raw_unresolved"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                item, outcome = substitution()
                if scenario.startswith("generic_"):
                    item, outcome = generic_yogurt()
                bread = {"name": "bread", "preferred_name": "bread", "quantity": 2,
                         "max_price": 5, "alternatives": []}
                bread_outcome = {"item_name": "bread", "selection_mode": "exact",
                                 "product": {"name": "bread", "unit_price": 4}}
                items = ([item, bread] if scenario in {"recovery", "scoped_item"}
                         else [bread] if scenario == "quantity" else [item])
                db = open_db(":memory:")
                self.addCleanup(db.close)
                with db:
                    for requested in items:
                        db.execute("INSERT INTO items(name, quantity) VALUES (?, ?)", (requested["name"], requested["quantity"]))
                registered = {}
                tools = SimpleNamespace(
                    action=lambda description: lambda fn: registered.setdefault(fn.__name__, fn),
                    registry=SimpleNamespace(create_action_model=lambda **kwargs: Action))
                agent = SimpleNamespace(history=SimpleNamespace(history=[]), stop=MagicMock())
                browser = SimpleNamespace(kill=AsyncMock(), browser_profile=SimpleNamespace(headless=True))
                calls = []
                executed = []
                node = SimpleNamespace(get_meaningful_text_for_llm=lambda: "Milk details", attributes={}, parent_node=None)

                def make_agent(**kwargs):
                    self.assertEqual(kwargs["max_failures"], 5)
                    async def run(max_steps):
                        async def perform(action, error=None, url=SITE, controls=None):
                            output = SimpleNamespace(action=[Action(action)])
                            state = SimpleNamespace(url=url, dom_state=SimpleNamespace(selector_map=controls or {}))
                            await kwargs["register_new_step_callback"](state, output, None)
                            selected = output.action[0].model_dump()
                            calls.append(selected)
                            if agent.stop.called:
                                return None
                            executed.append(selected)
                            name, params = next(iter(selected.items()))
                            returned = None
                            if name in registered:
                                params = dict(params)
                                if "outcome" in params:
                                    schema = registered[name].__annotations__["outcome"]
                                    params["outcome"] = schema.model_validate(params["outcome"])
                                returned = await registered[name](**params)
                            agent.history.history.append(SimpleNamespace(model_output=output, result=[SimpleNamespace(error=error)]))
                            return returned

                        cart, outcomes = [], []
                        if scenario == "recovery":
                            await perform({"begin_item": {"item_name": item["name"]}})
                            await perform({"report_item_failure": {"item_name": item["name"]}})
                            for term in ("milk", "fresh milk", "full fat milk"):
                                await perform({"input": {"index": 1, "text": term}}, error="search failed")
                            await perform({"navigate": {"url": SITE + "cart"}})
                            self.assertEqual(calls[-1], {"item_unresolved": {"item_name": item["name"], "reason": "recovery_exhausted"}})
                            outcomes.append({"item_name": item["name"], "unresolved_reason": "recovery_exhausted"})
                            await perform({"begin_item": {"item_name": "bread"}})
                            await perform({"add_product_plus": {"outcome": bread_outcome, "observed_quantity": 0,
                                                                 "observed_item_quantity": 0}})
                            await perform({"add_product_plus": {"outcome": bread_outcome, "observed_quantity": 1,
                                                                 "observed_item_quantity": 1}})
                            cart = [{"name": "bread", "quantity": 2, "unit_price": 4}]
                            outcomes.append(bread_outcome)
                        elif scenario == "quantity":
                            await perform({"begin_item": {"item_name": "bread"}})
                            await perform({"add_product_plus": {"outcome": bread_outcome, "observed_quantity": 0,
                                                                 "observed_item_quantity": 0}})
                            await perform({"add_product_plus": {"outcome": bread_outcome, "observed_quantity": 0,
                                                                 "observed_item_quantity": 0}})
                            cart = [{"name": "bread", "quantity": 1, "unit_price": 4}]
                            outcomes = [bread_outcome]
                        elif scenario.startswith("generic_"):
                            await perform({"begin_item": {"item_name": item["name"]}})
                            if scenario == "generic_missing":
                                added = await perform({"add_product_plus": {"outcome": outcome, "observed_quantity": 9,
                                                                            "observed_item_quantity": 9}})
                                self.assertIn("Clicked product + once", added.extracted_content)
                            else:
                                recorded = await perform({"record_item_selection": {"outcome": outcome}})
                                self.assertIn("Selection evidence recorded", recorded.extracted_content)
                            cart = [{"name": outcome["product"]["name"], "quantity": 10, "unit_price": 9.95}]
                            if scenario == "generic_changed":
                                outcome["product"]["match_reason"] = "Unrecorded final judgment"
                            outcomes = [outcome]
                        elif scenario == "satisfied":
                            await perform({"begin_item": {"item_name": item["name"]}})
                            recorded = await perform({"record_item_selection": {"outcome": outcome}})
                            self.assertIn("Selection evidence recorded", recorded.extracted_content)
                            cart = [{"name": outcome["product"]["name"], "quantity": 2, "unit_price": 11.5}]
                            outcomes = [outcome]
                        elif scenario in {"aggregate", "aggregate_limit"}:
                            await perform({"begin_item": {"item_name": item["name"]}})
                            added = await perform({"add_product_plus": {"outcome": outcome, "observed_quantity": 0,
                                                                       "observed_item_quantity": 1}})
                            self.assertIn("Clicked product + once", added.extracted_content)
                            if scenario == "aggregate_limit":
                                rejected = await perform({"add_product_plus": {"outcome": outcome, "observed_quantity": 1,
                                                                              "observed_item_quantity": 2}})
                                self.assertIn("quantity_unverified", rejected.extracted_content)
                            cart = [{"name": item["preferred_name"], "quantity": 1, "unit_price": 10},
                                    {"name": outcome["product"]["name"], "quantity": 1, "unit_price": 11.5}]
                            outcomes = [outcome]
                        elif scenario in {"svg", "svg_ancestor"}:
                            await perform({"begin_item": {"item_name": item["name"]}})
                            svg = SimpleNamespace(get_meaningful_text_for_llm=lambda: "", tag_name="svg",
                                                  attributes={"class": "Counter__StyledAddToCart-live"}, parent_node=None)
                            if scenario == "svg_ancestor":
                                svg = SimpleNamespace(get_meaningful_text_for_llm=lambda: "", tag_name="svg",
                                                      attributes={}, parent_node=SimpleNamespace(
                                                          attributes={"class": "Counter__wrapper"}, parent_node=None))
                            await perform({"click": {"index": 7}}, url=SITE + "product/milk", controls={7: svg})
                            self.assertNotIn({"click": {"index": 7}}, executed)
                        elif scenario in {"scoped_item", "scoped_page"}:
                            await perform({"begin_item": {"item_name": item["name"]}})
                            await perform({"click": {"index": 7}}, error="product link failed",
                                          url=SITE + "product/milk", controls={7: node})
                            if scenario == "scoped_item":
                                await perform({"begin_item": {"item_name": "bread"}}, url=SITE + "product/milk")
                            else:
                                await perform({"navigate": {"url": SITE + "product/bread"}}, url=SITE + "product/milk")
                            await perform({"click": {"index": 7}}, controls={7: node},
                                          url=SITE + ("product/milk" if scenario == "scoped_item" else "product/bread"))
                            self.assertEqual(calls[-1], {"click": {"index": 7}})
                            self.assertEqual(executed.count({"click": {"index": 7}}), 2)
                        elif scenario in {"checkout", "checkout_path", "slot_path", "account_path",
                                          "proceed_checkout", "checkout_destination", "no_active"}:
                            if scenario != "no_active":
                                await perform({"begin_item": {"item_name": item["name"]}})
                            if scenario == "checkout":
                                await perform({"finish_items": {}})
                            error_url = SITE + {"checkout_path": "checkout", "slot_path": "slot",
                                                "account_path": "account", "proceed_checkout": "cart",
                                                "checkout_destination": "product/milk"}.get(scenario, "cart")
                            checkout_node = (SimpleNamespace(get_meaningful_text_for_llm=lambda: "Proceed to Checkout",
                                                             attributes={}, parent_node=None)
                                             if scenario == "proceed_checkout" else node)
                            await perform({"navigate": {"url": SITE + "checkout"}} if scenario == "checkout_destination"
                                          else {"click": {"index": 7}}, error="checkout failed",
                                          url=error_url, controls={7: checkout_node})
                            await perform({"navigate": {"url": SITE + "cart"}}, url=error_url)
                            self.assertNotIn({"navigate": {"url": SITE + "cart"}}, executed)
                        elif scenario == "raw_unresolved":
                            cart = [{"name": item["preferred_name"], "quantity": 2, "unit_price": 10}]
                            outcomes = [{"item_name": item["name"], "selection_mode": "exact",
                                         "product": {"name": item["preferred_name"], "sku": item["sku"], "unit_price": 10}}]
                        else:
                            await perform({"begin_item": {"item_name": item["name"]}})
                            if scenario == "rejected":
                                outcome["product"]["unit_price"] = 11.51
                            if scenario != "fabricated":
                                await perform({"add_product_plus": {"outcome": outcome, "observed_quantity": 0,
                                                                     "observed_item_quantity": 0}})
                                if scenario == "accepted":
                                    await perform({"add_product_plus": {"outcome": outcome, "observed_quantity": 1,
                                                                         "observed_item_quantity": 1}})
                            cart = [{"name": outcome["product"]["name"], "quantity": 2, "unit_price": outcome["product"]["unit_price"]}]
                            outcomes = [outcome]
                        summary = kwargs["output_model_schema"](cart=cart, item_outcomes=outcomes,
                                                                 slot="Tomorrow", stage="slot_selected",
                                                                 unresolved=["Model unresolved: Bearer private-secret https://example.com/?account=private-account",
                                                                             item["name"].upper(), item["name"]]
                                                                 if scenario == "raw_unresolved" else [])
                        return SimpleNamespace(structured_output=summary, is_successful=lambda: True,
                                               is_done=lambda: True, errors=lambda: [], history=[])
                    agent.run = run
                    return agent

                module = SimpleNamespace(ActionResult=lambda **kwargs: SimpleNamespace(**{"error": None, **kwargs}),
                                         Agent=MagicMock(side_effect=make_agent), Tools=lambda: tools)
                path = Path(directory) / "result.json"
                with patch.dict("sys.modules", {"browser_use": module}), patch("shopping.create_llm"), \
                        patch("shopping.create_browser", return_value=browser), patch("shopping.start_browser", new_callable=AsyncMock), \
                        patch("shopping.check_readiness", new_callable=AsyncMock, return_value={"success": True}), \
                        patch("shopping.verify_product_page", new_callable=AsyncMock), \
                        patch("shopping.click_product_plus", new_callable=AsyncMock) as click, \
                        patch("sys.stdin.isatty", return_value=False), redirect_stdout(StringIO()) as output:
                    self.assertEqual(asyncio.run(shop(SITE, items, db, result_file=path)), scenario in {
                        "accepted", "aggregate", "satisfied", "generic_satisfied", "generic_missing"})
                self.assertEqual(click.await_count, {"recovery": 2, "accepted": 2, "quantity": 1,
                                                     "aggregate": 1, "aggregate_limit": 1, "generic_missing": 1}.get(scenario, 0))
                stopped = scenario in {"svg", "svg_ancestor", "checkout", "checkout_path", "slot_path",
                                       "account_path", "proceed_checkout", "checkout_destination", "no_active"}
                if stopped:
                    agent.stop.assert_called_once()
                else:
                    agent.stop.assert_not_called()
                data = json.loads(path.read_text())
                self.assertEqual(len(data["summary"]["item_outcomes"]), len(items))
                missing = [] if scenario in {"accepted", "aggregate", "satisfied", "raw_unresolved", "generic_satisfied", "generic_missing"} else [requested["name"] for requested in items]
                if scenario == "recovery":
                    missing = [item["name"]]
                self.assertEqual(data["summary"]["missing_or_over_cap"], missing)
                if stopped:
                    self.assertEqual(data["error_code"], "guard_blocked_action" if scenario.startswith("svg") else "agent_failure")
                    self.assertIs(data["success"], False)
                if scenario == "raw_unresolved":
                    self.assertEqual(data["summary"]["unresolved"], [item["name"]])
                    self.assertNotIn("Model unresolved", json.dumps(data["summary"]))
                    self.assertNotIn("Model unresolved", output.getvalue())
                    self.assertNotIn("Model unresolved", json.dumps({key: value for key, value in data.items()
                                                                     if key != "diagnostic"}))
                    self.assertEqual(data["error_code"], "agent_failure")
                    errors = json.loads(data["diagnostic"]["message"])["errors"]
                    self.assertEqual(len(errors), 1)
                    self.assertTrue(errors[0].startswith("Model unresolved: Bearer [REDACTED]"))
                    self.assertIn("[URL REDACTED]", errors[0])
                    for secret in ("private-secret", "private-account", "https://example.com"):
                        self.assertNotIn(secret, json.dumps(data))
                    saved = json.loads(db.execute("SELECT summary FROM attempts").fetchone()[0])
                    self.assertNotIn("Model unresolved", json.dumps(saved))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM alternatives").fetchone()[0], 0)
                if scenario == "accepted":
                    confirm_purchase(db, data["attempt"])
                    self.assertEqual(read_items(db)[0]["alternatives"], [{"name": "Nadec milk 1 L", "sku": "123"}])
                if scenario.startswith("generic_"):
                    confirm_purchase(db, data["attempt"])
                    self.assertEqual(read_items(db)[0]["quantity"], 10)
                    self.assertEqual(read_items(db)[0]["alternatives"], [] if scenario == "generic_changed" else
                                     [{"name": outcome["product"]["name"], "sku": None}])
                browser.kill.assert_awaited_once()

    def test_cart_guard_parse_failures_do_not_impersonate_or_hide_action_failures(self):
        from browser_use import ActionResult
        from browser_use.agent.views import AgentHistory

        for scenario in ("first_step", "after_add", "failed_click", "failed_account", "failed_checkout"):
            with self.subTest(scenario=scenario):
                item, outcome = substitution()
                agent = SimpleNamespace(history=SimpleNamespace(history=[]), stop=MagicMock())
                browser = SimpleNamespace(kill=AsyncMock(), browser_profile=SimpleNamespace(headless=True))

                def make_agent(**kwargs):
                    self.assertEqual(kwargs["max_failures"], 5)
                    self.assertEqual(kwargs["max_actions_per_step"], 1)

                    async def run(max_steps):
                        tools = kwargs["tools"]
                        guard = kwargs["register_new_step_callback"]
                        model = tools.registry.create_action_model()
                        state = SimpleNamespace(url=SITE + "product/milk", dom_state=SimpleNamespace(
                            selector_map={7: SimpleNamespace(get_meaningful_text_for_llm=lambda: "Milk details",
                                                             attributes={}, parent_node=None)}))

                        def output(action):
                            return SimpleNamespace(action=[model.model_validate(action)])

                        if scenario != "first_step":
                            await tools.begin_item(item_name=item["name"], browser_session=None)
                            if scenario == "after_add":
                                prior = output({"add_product_plus": {"outcome": outcome,
                                                "observed_quantity": 0, "observed_item_quantity": 0}})
                            else:
                                state.url = SITE + {"failed_click": "product/milk", "failed_account": "account",
                                                    "failed_checkout": "checkout"}[scenario]
                                prior = output({"click": {"index": 7}})
                            await guard(state, prior, None)
                            result = (await tools.act(prior.action[0], browser_session=None) if scenario == "after_add"
                                      else ActionResult(error="Action failed; outcome uncertain"))
                            agent.history.history.append(AgentHistory.model_construct(model_output=prior, result=[result]))

                        # Parsing fails before the callback or action dispatch; multiple such steps may accumulate.
                        for _ in range(2):
                            agent.history.history.append(AgentHistory.model_construct(
                                model_output=None, result=[ActionResult(error="AgentOutput Invalid JSON: trailing characters")]))
                        next_output = output({"click": {"index": 7}} if scenario in {"failed_click", "after_add"}
                                             else {"navigate": {"url": SITE + "cart"}})
                        expected = next_output.action[0].model_dump(exclude_none=True)
                        await guard(state, next_output, None)
                        if scenario in {"failed_account", "failed_checkout"}:
                            agent.stop.assert_called_once()
                        else:
                            agent.stop.assert_not_called()
                            self.assertEqual(next_output.action[0].model_dump(exclude_none=True),
                                             {"item_unresolved": {"item_name": item["name"], "reason": "recovery_exhausted"}}
                                             if scenario == "failed_click" else expected)
                        if scenario == "after_add":
                            # A parse failure must neither replay a successful add nor discard quantity tracking.
                            rejected = await tools.act(prior.action[0], browser_session=None)
                            self.assertIn("quantity_unverified", rejected.extracted_content)
                        return SimpleNamespace(structured_output=None, final_result=lambda: None,
                                               is_done=lambda: False, is_successful=lambda: False, errors=lambda: [], history=[])
                    agent.run = run
                    return agent

                with patch("browser_use.Agent", side_effect=make_agent), patch("shopping.create_llm"), \
                        patch("shopping.create_browser", return_value=browser), \
                        patch("shopping.start_browser", new_callable=AsyncMock), \
                        patch("shopping.check_readiness", new_callable=AsyncMock, return_value={"success": True}), \
                        patch("shopping.verify_product_page", new_callable=AsyncMock), \
                        patch("shopping.click_product_plus", new_callable=AsyncMock) as click, \
                        patch("sys.stdin.isatty", return_value=False), redirect_stdout(StringIO()):
                    self.assertFalse(asyncio.run(shop(SITE, [item], None)))
                self.assertEqual(click.await_count, 1 if scenario == "after_add" else 0)
                browser.kill.assert_awaited_once()

    def test_real_tools_register_and_dispatch_nested_selection_evidence(self):
        from browser_use import ActionResult, Tools

        for scenario in ("valid", "rejected", "url_mismatch", "missing_url", "http_url", "foreign_url",
                         "credential_url", "recovery_exhausted"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                item, outcome = substitution()
                product_url = SITE + "product/milk"
                outcome["product"]["product_url"] = product_url
                if scenario == "rejected":
                    outcome["product"]["unit_price"] = 11.51
                elif scenario == "missing_url":
                    del outcome["product"]["product_url"]
                elif scenario == "http_url":
                    outcome["product"]["product_url"] = product_url.replace("https:", "http:")
                elif scenario == "foreign_url":
                    outcome["product"]["product_url"] = "https://example.com/product/milk"
                elif scenario == "credential_url":
                    outcome["product"]["product_url"] = product_url.replace("https://", "https://user@")
                control = SimpleNamespace(click=AsyncMock())
                page = SimpleNamespace(get_elements_by_css_selector=AsyncMock(return_value=[control]))
                browser = SimpleNamespace(
                    get_current_page_url=AsyncMock(return_value=(SITE + "product/bread" if scenario == "url_mismatch"
                                                               else outcome["product"].get("product_url", product_url))),
                    get_current_page=AsyncMock(return_value=page), kill=AsyncMock(),
                    browser_profile=SimpleNamespace(headless=True))
                db = open_db(":memory:")
                self.addCleanup(db.close)
                agent = SimpleNamespace(history=SimpleNamespace(history=[]), stop=MagicMock())

                def make_agent(**kwargs):
                    async def run(max_steps):
                        tools = kwargs["tools"]
                        self.assertIsInstance(tools, Tools)
                        model = tools.registry.create_action_model(include_actions=["add_product_plus"])
                        model.model_json_schema()  # Resolve the local ItemOutcome/Product definitions.
                        action = model.model_validate({"add_product_plus": {
                            "outcome": outcome, "observed_quantity": 0, "observed_item_quantity": 0}})
                        self.assertEqual(action.add_product_plus.outcome.product.name, outcome["product"]["name"])
                        if scenario == "missing_url":
                            self.assertIsNone(action.add_product_plus.outcome.product.product_url)
                        begun = await tools.begin_item(item_name=item["name"], browser_session=None)
                        self.assertIsInstance(begun, ActionResult)
                        self.assertIsNone(begun.error)
                        self.assertIn(item["name"], begun.extracted_content)
                        if scenario == "recovery_exhausted":
                            reported = await tools.report_item_failure(item_name=item["name"], browser_session=None)
                            self.assertIsNone(reported.error)
                            guard = kwargs["register_new_step_callback"]
                            state = SimpleNamespace(url=product_url, dom_state=SimpleNamespace(selector_map={}))
                            recovery_model = tools.registry.create_action_model(include_actions=["input", "navigate"])
                            for term in ("milk", "fresh milk", "full fat milk"):
                                output = SimpleNamespace(action=[recovery_model.model_validate({
                                    "input": {"index": 1, "text": term}})])
                                await guard(state, output, None)
                                self.assertIn("input", output.action[0].model_dump(exclude_none=True))
                            output = SimpleNamespace(action=[recovery_model.model_validate({"navigate": {"url": SITE}})])
                            with patch.object(tools.registry, "create_action_model",
                                              wraps=tools.registry.create_action_model) as create_model:
                                await guard(state, output, None)
                            create_model.assert_called_once_with(include_actions=["item_unresolved"])
                            self.assertEqual(set(type(output.action[0]).model_fields), {"item_unresolved"})
                            self.assertEqual(output.action[0].model_dump(exclude_none=True), {
                                "item_unresolved": {"item_name": item["name"], "reason": "recovery_exhausted"}})
                            unresolved = await tools.act(output.action[0], browser_session=None)
                            self.assertIsInstance(unresolved, ActionResult)
                            self.assertIsNone(unresolved.error)
                            self.assertIn("Item unresolved", unresolved.extracted_content)
                            retry = await tools.begin_item(item_name=item["name"], browser_session=None)
                            self.assertIn("Item unresolved", retry.extracted_content)
                        else:
                            returned = await tools.add_product_plus(
                                outcome=outcome, observed_quantity=0, observed_item_quantity=0, browser_session=None)
                            self.assertIsInstance(returned, ActionResult)
                            self.assertIsNone(returned.error, returned.error)
                            if scenario == "valid":
                                self.assertIn("Clicked product + once", returned.extracted_content)
                                returned = await tools.add_product_plus(
                                    outcome=outcome, observed_quantity=1, observed_item_quantity=1, browser_session=None)
                                self.assertIsNone(returned.error, returned.error)
                                self.assertIn("Clicked product + once", returned.extracted_content)
                            else:
                                self.assertIn("No click performed", returned.extracted_content)
                                self.assertIn("price_cap" if scenario == "rejected" else "current product page",
                                              returned.extracted_content)
                        summary = kwargs["output_model_schema"](
                            cart=[], item_outcomes=[], slot=None, stage="cart", unresolved=[])
                        return SimpleNamespace(structured_output=summary, is_successful=lambda: False,
                                               is_done=lambda: True, errors=lambda: [], history=[])
                    agent.run = run
                    return agent

                path = Path(directory) / "result.json"
                with patch("browser_use.Agent", side_effect=make_agent), patch("shopping.create_llm"), \
                        patch("shopping.create_browser", return_value=browser), \
                        patch("shopping.start_browser", new_callable=AsyncMock) as start, \
                        patch("shopping.check_readiness", new_callable=AsyncMock, return_value={"success": True}), \
                        patch("sys.stdin.isatty", return_value=False), redirect_stdout(StringIO()):
                    self.assertFalse(asyncio.run(shop(SITE, [item], db, result_file=path)))
                start.assert_awaited_once_with(browser, SITE)
                self.assertEqual(control.click.await_count, 2 if scenario == "valid" else 0)
                if scenario != "valid":
                    page.get_elements_by_css_selector.assert_not_awaited()
                agent.stop.assert_not_called()
                if scenario == "recovery_exhausted":
                    saved = json.loads(path.read_text())["summary"]
                    self.assertEqual(saved["item_outcomes"][0]["unresolved_reason"], "recovery_exhausted")
                browser.kill.assert_awaited_once()

    def test_product_plus_exact_selector(self):
        self.assertEqual(PRODUCT_PLUS_SELECTOR,
                         '[class*="ProductDetails__ImgAndCarouselDiv"] svg[class*="Counter__StyledAddToCart"]')
        control = SimpleNamespace(click=AsyncMock())
        page = SimpleNamespace(get_elements_by_css_selector=AsyncMock(return_value=[control]))
        browser = SimpleNamespace(get_current_page_url=AsyncMock(return_value=SITE + "product/milk"),
                                  get_current_page=AsyncMock(return_value=page))
        asyncio.run(click_product_plus(browser))
        page.get_elements_by_css_selector.assert_awaited_once_with(PRODUCT_PLUS_SELECTOR)
        control.click.assert_awaited_once_with()
        for matches in ([], [control, control]):
            control.click.reset_mock()
            page.get_elements_by_css_selector.reset_mock()
            page.get_elements_by_css_selector.return_value = matches
            with self.assertRaisesRegex(ValueError, rf"missing or ambiguous \(match count: {len(matches)}\)"):
                asyncio.run(click_product_plus(browser))
            page.get_elements_by_css_selector.assert_awaited_once_with(PRODUCT_PLUS_SELECTOR)
            control.click.assert_not_awaited()
        browser.get_current_page_url.return_value = "https://example.com/"
        with self.assertRaisesRegex(ValueError, "only supported on Tamimi"):
            asyncio.run(click_product_plus(browser))

    def test_manual_setup_profile_and_cleanup(self):
        browser = SimpleNamespace(
            start=AsyncMock(), navigate_to=AsyncMock(), kill=AsyncMock(),
            browser_profile=SimpleNamespace(user_data_dir=None),
        )
        module = SimpleNamespace(Browser=MagicMock(return_value=browser))
        with tempfile.TemporaryDirectory() as directory, \
                patch("shopping.PROFILE", (Path(directory) / "profile").resolve()) as profile, \
                patch.dict("sys.modules", {"browser_use": module}), patch("sys.stdin.isatty", return_value=True), \
                patch("builtins.input", return_value="") as prompt, redirect_stdout(StringIO()):
            browser.browser_profile.user_data_dir = profile
            asyncio.run(setup())
            module.Browser.assert_called_once_with(user_data_dir=profile, headless=False, keep_alive=True,
                                                  storage_state=profile / "storage-state.json")
            self.assertEqual(profile.stat().st_mode & 0o777, 0o700)
            browser.navigate_to.assert_awaited_once_with(SITE)
            browser.kill.assert_awaited_once()
            prompt.assert_called_once()
            browser.browser_profile.user_data_dir = profile.parent / "temporary-profile"
            browser.navigate_to.reset_mock()
            browser.kill.reset_mock()
            with self.assertRaisesRegex(ValueError, "Shared browser profile unavailable"):
                asyncio.run(setup())
            browser.navigate_to.assert_not_awaited()
            browser.kill.assert_awaited_once()

    def test_shop_requires_verified_session_before_shopping(self):
        for signed_in, fulfillment, matches, expected_agents in (
                (False, "Home", True, 1), (True, None, False, 1),
                (True, "Home", False, 1), (True, "Home", True, 2)):
            with self.subTest(signed_in=signed_in, fulfillment=fulfillment, matches=matches):
                browser = SimpleNamespace(
                    start=AsyncMock(), navigate_to=AsyncMock(), kill=AsyncMock(),
                    browser_profile=SimpleNamespace(user_data_dir=None),
                )
                readiness = SimpleNamespace(signed_in=signed_in, fulfillment=fulfillment, location_matches=matches)
                checked = MagicMock()
                checked.structured_output = readiness
                checked.is_successful.return_value = True
                result = MagicMock()
                result.structured_output = None
                agents = [SimpleNamespace(run=AsyncMock(return_value=checked)),
                          SimpleNamespace(run=AsyncMock(return_value=result))]
                tools = MagicMock()
                tools.registry.registry.actions = {"done": None, "extract": None, "click": None,
                                                    "navigate": None, "input": None, "evaluate": None}
                module = SimpleNamespace(Browser=MagicMock(return_value=browser),
                                         Agent=MagicMock(side_effect=agents),
                                          Tools=MagicMock(return_value=tools), ChatOpenAI=MagicMock(),
                                         ActionResult=MagicMock())
                with tempfile.TemporaryDirectory() as directory, \
                        patch("shopping.PROFILE", (Path(directory) / "profile").resolve()) as profile, \
                         patch.dict("sys.modules", {"browser_use": module}), \
                         patch.dict("os.environ", {"OPENAI_API_KEY": "test-key", "OPENAI_MODEL": "local-model",
                                                   "OPENAI_BASE_URL": "https://llm.example.com/v1"}), \
                         patch("dotenv.load_dotenv"), \
                         patch.dict("os.environ", {"BROWSER_USE_HEADLESS": "false"}), \
                         patch("sys.stdin.isatty", return_value=False), redirect_stdout(StringIO()):
                    browser.browser_profile.user_data_dir = profile
                    self.assertFalse(asyncio.run(shop(SITE, [], None, "Home")))
                self.assertEqual(module.Agent.call_count, expected_agents)
                module.ChatOpenAI.assert_called_once_with(model="local-model", base_url="https://llm.example.com/v1")
                for call in module.Agent.call_args_list:
                    self.assertIs(call.kwargs["llm"], module.ChatOpenAI.return_value)
                    self.assertIs(call.kwargs["enable_signal_handler"], False)
                module.Browser.assert_called_once_with(user_data_dir=profile, headless=False, keep_alive=True,
                                                      storage_state=profile / "storage-state.json")
                self.assertEqual([call.args[0] for call in tools.exclude_action.call_args_list],
                                 ["navigate", "input", "evaluate"])
                browser.kill.assert_awaited_once()

    def test_shop_requires_openai_key_before_browser_start(self):
        with patch.dict("os.environ", {}, clear=True), patch("dotenv.load_dotenv"), \
                patch("shopping.create_browser") as browser:
            with self.assertRaisesRegex(ValueError, "Set OPENAI_API_KEY"):
                asyncio.run(shop(SITE, [], None))
            browser.assert_not_called()

    def test_headless_environment_parsing(self):
        module = SimpleNamespace(Browser=MagicMock())
        with tempfile.TemporaryDirectory() as directory, \
                patch("shopping.PROFILE", Path(directory) / "profile"), \
                patch.dict("sys.modules", {"browser_use": module}), \
                patch.dict("os.environ", {}, clear=True):
            create_browser()
            self.assertFalse(module.Browser.call_args.kwargs["headless"])
            for value, expected in (("true", True), (" TRUE ", True), ("false", False)):
                with patch.dict("os.environ", {"BROWSER_USE_HEADLESS": value}):
                    create_browser()
                    self.assertIs(module.Browser.call_args.kwargs["headless"], expected)
            for value in ("", "0", "yes", "invalid"):
                with patch.dict("os.environ", {"BROWSER_USE_HEADLESS": value}):
                    module.Browser.reset_mock()
                    with self.assertRaisesRegex(ValueError, "must be true or false"):
                        create_browser()
                    module.Browser.assert_not_called()
            with patch.dict("os.environ", {"BROWSER_USE_HEADLESS": "true"}):
                create_browser(headless=False)
                self.assertFalse(module.Browser.call_args.kwargs["headless"])

    def test_readiness_reasons_and_cart_guard(self):
        for observed, successful, reasons in (
                (None, True, ["no_readiness_summary"]),
                (SimpleNamespace(signed_in=False, fulfillment=" ", location_matches=False), False,
                 ["agent_unsuccessful", "not_signed_in", "missing_fulfillment", "location_mismatch"]),
                (SimpleNamespace(signed_in=True, fulfillment="Home", location_matches=True), True, [])):
            with self.subTest(reasons=reasons):
                checked = SimpleNamespace(structured_output=observed, is_successful=lambda: successful)
                agent = SimpleNamespace(run=AsyncMock(return_value=checked), stop=MagicMock())
                tools = MagicMock()
                tools.registry.registry.actions = {name: None for name in
                                                   ("done", "click", "navigate", "input", "evaluate", "add_product_plus")}
                module = SimpleNamespace(Agent=MagicMock(return_value=agent), Tools=MagicMock(return_value=tools))
                with patch.dict("sys.modules", {"browser_use": module}):
                    data = asyncio.run(check_readiness(object(), object(), "Home"))
                self.assertEqual(data["reasons"], reasons)
                self.assertEqual(data["success"], not reasons)
                self.assertEqual(data["status"], "readiness_failed" if reasons else "ready")
                self.assertEqual([call.args[0] for call in tools.exclude_action.call_args_list],
                                 ["navigate", "input", "evaluate", "add_product_plus"])
                guard = module.Agent.call_args.kwargs["register_new_step_callback"]
                node = SimpleNamespace(get_meaningful_text_for_llm=lambda: "Add to cart")
                state = SimpleNamespace(url=SITE, dom_state=SimpleNamespace(selector_map={1: node}))
                action = SimpleNamespace(model_dump=lambda **kwargs: {"click": {"index": 1}})
                asyncio.run(guard(state, SimpleNamespace(action=[action]), 0))
                agent.stop.assert_called_once()
                agent.run.assert_awaited_once_with(max_steps=15)
                self.assertIs(module.Agent.call_args.kwargs["enable_signal_handler"], False)

    def test_readiness_cli_json_without_database_or_shopping(self):
        for success in (False, True):
            data = {"status": "ready" if success else "readiness_failed", "success": success,
                    "reasons": [] if success else ["not_signed_in"], "signed_in": success,
                    "fulfillment": "Home", "location_matches": True}
            browser = SimpleNamespace(kill=AsyncMock())
            output = StringIO()
            with tempfile.TemporaryDirectory() as directory, \
                    patch("shopping.create_llm", return_value="llm"), \
                    patch("shopping.create_browser", return_value=browser), \
                    patch("shopping.start_browser", new_callable=AsyncMock) as start, \
                    patch("shopping.check_readiness", new_callable=AsyncMock, return_value=data) as check, \
                    patch("shopping.open_db") as db, patch("shopping.shop") as shopping:
                path = Path(directory) / "result.json"
                with patch("sys.argv", ["shopping.py", "readiness", "--location", "Home",
                                        "--result-file", str(path)]), redirect_stdout(output):
                    self.assertEqual(main(), 0 if success else 1)
                self.assertEqual(json.loads(output.getvalue()), data)
                self.assertEqual(json.loads(path.read_text()), data)
                start.assert_awaited_once_with(browser, SITE)
                check.assert_awaited_once_with(browser, "llm", "Home")
                browser.kill.assert_awaited_once()
                db.assert_not_called()
                shopping.assert_not_called()

    def test_readiness_cleanup_on_error(self):
        browser = SimpleNamespace(kill=AsyncMock())
        with patch("shopping.create_llm"), patch("shopping.create_browser", return_value=browser), \
                patch("shopping.start_browser", new_callable=AsyncMock, side_effect=ValueError("start failed")):
            with self.assertRaisesRegex(ValueError, "start failed"):
                asyncio.run(readiness())
            browser.kill.assert_awaited_once()

    @unittest.skipUnless(os.name == "posix", "SIGTERM cancellation uses POSIX loop handlers")
    def test_cli_sigterm_waits_for_browser_cleanup(self):
        for command in ("readiness", "shop"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                db_path = Path(directory) / "shopping.db"
                db = open_db(db_path)
                with db:
                    db.execute("INSERT INTO items(name, quantity) VALUES ('milk', 2)")
                db.close()
                loop = SimpleNamespace(add_signal_handler=MagicMock(), remove_signal_handler=MagicMock())
                cleaned = []

                async def interrupted_check(*args):
                    loop.add_signal_handler.call_args.args[1]()
                    await asyncio.sleep(0)
                    self.fail("SIGTERM did not cancel readiness")

                async def kill():
                    # A repeated SIGTERM must not interrupt snapshot-saving cleanup.
                    loop.add_signal_handler.call_args.args[1]()
                    await asyncio.sleep(0)
                    cleaned.append(True)

                browser = SimpleNamespace(kill=AsyncMock(side_effect=kill),
                                          browser_profile=SimpleNamespace(headless=False))
                module = SimpleNamespace(ActionResult=MagicMock(), Agent=MagicMock(), Tools=MagicMock())
                with patch.dict("sys.modules", {"browser_use": module}), \
                        patch("shopping.create_llm"), patch("shopping.create_browser", return_value=browser), \
                        patch("shopping.start_browser", new_callable=AsyncMock), \
                        patch("shopping.check_readiness", side_effect=interrupted_check), \
                        patch("shopping.asyncio.get_running_loop", return_value=loop), \
                        patch("shopping.signal.getsignal", return_value=signal.SIG_DFL), \
                        patch("shopping.signal.signal") as restore, \
                        patch("sys.stdin.isatty", return_value=True), patch("builtins.input") as prompt, \
                        patch("sys.argv", ["shopping.py", "--db", str(db_path), command]):
                    self.assertEqual(main(), 143)
                    self.assertEqual(cleaned, [True])
                    browser.kill.assert_awaited_once()
                    prompt.assert_not_called()
                    module.Agent.assert_not_called()
                    self.assertEqual(loop.add_signal_handler.call_args.args[0], signal.SIGTERM)
                    loop.remove_signal_handler.assert_called_once_with(signal.SIGTERM)
                    restore.assert_called_once_with(signal.SIGTERM, signal.SIG_DFL)

    def test_shop_result_files_and_headless_completion(self):
        item = {"name": "milk", "preferred_name": "milk", "quantity": 2,
                "max_price": 8, "alternatives": []}
        ready = {"status": "ready", "success": True, "reasons": [], "signed_in": True,
                 "fulfillment": "Home", "location_matches": True}
        for scenario in ("readiness_failed", "no_summary", "complete", "missing", "over_cap",
                         "no_slot", "wrong_stage", "agent_failed", "headed", "noninteractive"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                db = open_db(":memory:")
                self.addCleanup(db.close)
                browser = SimpleNamespace(kill=AsyncMock(), browser_profile=SimpleNamespace(
                    headless=scenario not in {"headed", "noninteractive"}))
                result = MagicMock()
                result.is_successful.return_value = scenario != "agent_failed"
                result.final_result.return_value = "No summary"
                result.is_done.return_value = False
                result.errors.return_value = []
                result.history = []
                cart = [{"name": "milk", "quantity": 1 if scenario == "missing" else 2,
                         "unit_price": 9 if scenario == "over_cap" else 7}]
                summary = {"cart": cart, "slot": None if scenario == "no_slot" else "Tomorrow 9am",
                           "stage": "cart" if scenario == "wrong_stage" else "slot_selected", "unresolved": [],
                           "item_outcomes": [{"item_name": "milk", "selection_mode": "exact",
                                              "product": {"name": "milk", "unit_price": cart[0]["unit_price"]}}]}
                result.structured_output = (None if scenario == "no_summary" else
                                            SimpleNamespace(model_dump=lambda: dict(summary),
                                                            cart=[SimpleNamespace(**row) for row in cart],
                                                            slot=summary["slot"], stage=summary["stage"]))
                agent = SimpleNamespace(run=AsyncMock(return_value=result))
                module = SimpleNamespace(ActionResult=MagicMock(), Agent=MagicMock(return_value=agent), Tools=MagicMock())
                observed = ({**ready, "status": "readiness_failed", "success": False,
                             "signed_in": False, "reasons": ["not_signed_in"]}
                            if scenario == "readiness_failed" else ready)
                path = Path(directory) / "result.json"
                output = StringIO()
                with patch.dict("sys.modules", {"browser_use": module}), \
                        patch("shopping.create_llm"), patch("shopping.create_browser", return_value=browser), \
                        patch("shopping.start_browser", new_callable=AsyncMock), \
                        patch("shopping.check_readiness", new_callable=AsyncMock, return_value=observed), \
                        patch("sys.stdin.isatty", return_value=scenario != "noninteractive"), \
                        patch("builtins.input") as prompt, \
                        redirect_stdout(output):
                    self.assertEqual(asyncio.run(shop(SITE, [item], db, result_file=path)),
                                     scenario in {"complete", "headed", "noninteractive"})
                data = json.loads(path.read_text())
                self.assertEqual(data["success"], scenario in {"complete", "headed", "noninteractive"})
                browser.kill.assert_awaited_once()
                if scenario == "headed":
                    prompt.assert_called_once()
                else:
                    prompt.assert_not_called()
                count = db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
                if scenario == "readiness_failed":
                    self.assertEqual(data, observed)
                    module.Agent.assert_not_called()
                    self.assertEqual(count, 0)
                elif scenario == "no_summary":
                    self.assertEqual(data["status"], "no_summary")
                    self.assertEqual(data["phase"], "cart_agent")
                    self.assertEqual(data["error_type"], "RuntimeError")
                    self.assertEqual(data["error_code"], "no_final_output")
                    self.assertEqual(data["diagnostic"]["frames"], [])
                    self.assertEqual(json.loads(data["diagnostic"]["message"])["errors"], [])
                    self.assertEqual(count, 0)
                    self.assertIn("No summary", output.getvalue())
                else:
                    self.assertEqual(data["status"], "attempt_saved")
                    self.assertIs(module.Agent.call_args.kwargs["enable_signal_handler"], False)
                    agent.run.assert_awaited_once_with(max_steps=30)
                    self.assertEqual(count, 1)
                    saved = db.execute("SELECT summary, purchased_at FROM attempts WHERE id = ?",
                                       (data["attempt"],)).fetchone()
                    self.assertEqual(json.loads(saved["summary"]), data["summary"])
                    self.assertIsNone(saved["purchased_at"])
                    self.assertEqual(db.execute("SELECT unit_price FROM prices").fetchone()[0], cart[0]["unit_price"])
                    if scenario == "headed":
                        self.assertIn("review the open browser", output.getvalue())
                    else:
                        self.assertIn("reopen Tamimi", output.getvalue())
                        self.assertNotIn("open browser", output.getvalue())

    def test_no_summary_termination_evidence(self):
        item = {"name": "milk", "preferred_name": "milk", "quantity": 1,
                "max_price": 8, "alternatives": []}
        for scenario in ("guard", "plus", "failure", "limit", "stopped", "missing", "broken"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                db = open_db(":memory:")
                self.addCleanup(db.close)
                browser = SimpleNamespace(kill=AsyncMock(), browser_profile=SimpleNamespace(headless=True))
                private_error = "private-key Bearer bearer-secret api_key=unknown-secret https://example.com/?account=private-account"
                errors = [None, private_error, private_error, "last failure"] if scenario in {"failure", "limit"} else []
                result = SimpleNamespace(structured_output=None, history=[None] * (30 if scenario == "limit" else 0),
                                         is_done=lambda: False, is_successful=lambda: False,
                                         errors=lambda: errors, final_result=lambda: None)
                state = SimpleNamespace(stopped=scenario == "stopped", consecutive_failures=0,
                                        n_steps=30 if scenario == "limit" else 0,
                                        last_result=[SimpleNamespace(error=private_error), SimpleNamespace(error=None)]
                                        if errors else [])
                agent = SimpleNamespace(state=state)
                def stop():
                    state.stopped = True
                agent.stop = MagicMock(side_effect=stop)
                registered = {}
                tools = SimpleNamespace(action=lambda description: lambda fn: registered.setdefault(fn.__name__, fn))
                action_executed = []
                async def run(max_steps):
                    self.assertEqual(max_steps, 30)
                    if scenario == "guard":
                        guard = module.Agent.call_args.kwargs["register_new_step_callback"]
                        action = SimpleNamespace(model_dump=lambda **kwargs: {"click": {"index": 7}})
                        node = SimpleNamespace(get_meaningful_text_for_llm=lambda: "Pay private-account")
                        await guard(SimpleNamespace(url=SITE + "/cart?account=private-account",
                                                    dom_state=SimpleNamespace(selector_map={7: node})),
                                    SimpleNamespace(action=[action]), None)
                        if not state.stopped:
                            action_executed.append(action)
                    elif scenario == "plus":
                        await registered["begin_item"]("milk")
                        outcome_model = registered["add_product_plus"].__annotations__["outcome"]
                        returned = await registered["add_product_plus"](outcome_model(
                            item_name="milk", selection_mode="exact", product={"name": "milk", "unit_price": 7}), 0, 0)
                        self.assertIn("missing", returned.error)
                    return result
                agent.run = run
                module = SimpleNamespace(ActionResult=lambda **kwargs: SimpleNamespace(**kwargs),
                                         Agent=MagicMock(return_value=agent), Tools=lambda: tools)
                if scenario == "broken":
                    def broken():
                        raise RuntimeError("unavailable")
                    result.errors = result.is_done = result.is_successful = broken
                path = Path(directory) / "result.json"
                with patch.dict("sys.modules", {"browser_use": module}), \
                        patch.dict("os.environ", {"OPENAI_API_KEY": "private-key"}), \
                        patch("shopping.create_llm"), patch("shopping.create_browser", return_value=browser), \
                        patch("shopping.start_browser", new_callable=AsyncMock), \
                         patch("shopping.check_readiness", new_callable=AsyncMock, return_value={"success": True}), \
                         patch("shopping.verify_product_page", new_callable=AsyncMock), \
                         patch("shopping.click_product_plus", new_callable=AsyncMock,
                              side_effect=ValueError("Product + control missing; no click performed")), \
                        redirect_stdout(StringIO()):
                    self.assertFalse(asyncio.run(shop(SITE, [item], db, result_file=path)))
                data = json.loads(path.read_text())
                self.assertEqual(set(data), {"status", "success", "phase", "error_type", "error_code", "diagnostic"})
                self.assertEqual(data["status"], "no_summary")
                self.assertIs(data["success"], False)
                self.assertEqual(data["phase"], "cart_agent")
                self.assertEqual(data["error_type"], "RuntimeError")
                self.assertEqual(data["diagnostic"]["frames"], [])
                expected = {"guard": "guard_blocked_action", "plus": "product_plus_unavailable",
                            "failure": "agent_failure", "limit": "step_limit", "stopped": "agent_stopped",
                            "missing": "no_final_output", "broken": "no_final_output"}
                self.assertEqual(data["error_code"], expected[scenario])
                message = data["diagnostic"]["message"]
                self.assertLessEqual(len(message), 2000)
                evidence = json.loads(message)
                for secret in ("private-key", "bearer-secret", "unknown-secret", "private-account"):
                    self.assertNotIn(secret, message)
                if scenario == "guard":
                    self.assertEqual(evidence["stop_reason"], {"code": "guard_blocked_action", "actions": ["click"], "index": 7})
                    self.assertEqual(evidence["history_steps"], 0)
                    self.assertEqual(evidence["errors"], [])
                    self.assertEqual(action_executed, [])
                if scenario in {"guard", "plus"}:
                    agent.stop.assert_called_once()
                if errors:
                    self.assertEqual(len(evidence["errors"]), 2)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)
                browser.kill.assert_awaited_once()

    def test_no_summary_bounds_last_result_errors(self):
        from shopping import no_summary_result

        def unavailable():
            raise RuntimeError("history errors unavailable")

        result = SimpleNamespace(history=[], is_done=lambda: False, is_successful=lambda: False,
                                 errors=unavailable)
        state = SimpleNamespace(stopped=False, consecutive_failures=2, n_steps=3,
                                last_result=[SimpleNamespace(error=None)] + [
                                    SimpleNamespace(error=f"failure {index}: " + "\"\\\n" * 1000 + "secret-value")
                                    for index in range(4)])
        with patch.dict("os.environ", {"OPENAI_API_KEY": "secret-value"}):
            data = no_summary_result(result, SimpleNamespace(state=state), 30, None)
        self.assertEqual(data["error_code"], "agent_failure")
        message = data["diagnostic"]["message"]
        self.assertLessEqual(len(message), 2000)
        self.assertNotIn("secret-value", message)
        evidence = json.loads(message)
        self.assertTrue(evidence["errors"])
        self.assertLessEqual(len(evidence["errors"]), 3)
        self.assertTrue(all(len(error) <= 250 for error in evidence["errors"]))
        self.assertEqual(evidence["consecutive_failures"], 2)

    def test_shop_cli_result_file_and_exit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "shopping.db"
            db = open_db(db_path)
            with db:
                db.execute("INSERT INTO items(name, quantity) VALUES ('milk', 2)")
            db.close()
            path = Path(directory) / "result.json"
            for success in (False, True):
                with patch("shopping.shop", new_callable=AsyncMock, return_value=success) as run, \
                        patch("sys.argv", ["shopping.py", "--db", str(db_path), "shop",
                                           "--location", "Home", "--result-file", str(path)]):
                    self.assertEqual(main(), 0 if success else 1)
                    args = run.await_args.args
                    self.assertEqual(args[0], SITE)
                    self.assertEqual(args[1][0]["name"], "milk")
                    self.assertEqual(args[3:], ("Home", path))

    def test_shop_cli_prebrowser_errors(self):
        for failure, phase in (("empty", "list_read"), ("db", "db_open"),
                               ("config", "config"), ("list", "list_read")):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                db = open_db(":memory:")
                self.addCleanup(db.close)
                path = Path(directory) / "result.json"
                output = StringIO()
                with patch("shopping.open_db", return_value=db,
                           side_effect=RuntimeError("database failed") if failure == "db" else None), \
                        patch("shopping.read_items", return_value=[] if failure == "empty" else [{}],
                              side_effect=RuntimeError("list failed") if failure == "list" else None), \
                        patch("shopping.create_llm", side_effect=RuntimeError("config failed")), \
                        patch("shopping.create_browser") as browser, \
                        patch("sys.argv", ["shopping.py", "shop", "--result-file", str(path)]), \
                        redirect_stderr(output), redirect_stdout(output):
                    with self.assertRaises(SystemExit) as exited:
                        main()
                    self.assertEqual(exited.exception.code, 2)
                    browser.assert_not_called()
                data = json.loads(path.read_text())
                self.assertEqual(data["status"], "error")
                self.assertIs(data["success"], False)
                self.assertEqual(data["phase"], phase)
                self.assertEqual(data["error_type"], "ValueError" if failure == "empty" else "RuntimeError")
                self.assertTrue(data["diagnostic"]["frames"])

    def test_shop_cli_errors_across_workflow(self):
        from pydantic import BaseModel, ValidationError

        class Output(BaseModel):
            quantity: int

        for phase in ("browser_startup", "readiness", "readiness_output", "cart_planning",
                      "cart_tools", "cart_agent", "cart_output", "assessment", "persistence",
                      "result_output", "cleanup"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                db = open_db(":memory:")
                self.addCleanup(db.close)
                with db:
                    db.execute("INSERT INTO items(name, quantity) VALUES ('milk', 1)")
                requested_items = read_items(db)
                path = Path(directory) / "result.json"
                output = StringIO()
                failure = RuntimeError("private-openai private-telegram private-typesafe Bearer unknown-bearer api_key=unknown-key sk-unknownsecret")
                browser = SimpleNamespace(kill=AsyncMock(side_effect=failure if phase == "cleanup" else None),
                                          browser_profile=SimpleNamespace(headless=True))
                checked = MagicMock()
                checked.structured_output = SimpleNamespace(signed_in=True, fulfillment="Home", location_matches=True)
                checked.is_successful.return_value = True
                result = MagicMock()
                row = SimpleNamespace(name="milk", quantity=1, unit_price=7)
                summary = {"cart": [vars(row)], "slot": "Tomorrow", "stage": "slot_selected", "unresolved": []}
                result.structured_output = SimpleNamespace(model_dump=lambda: dict(summary), cart=[row],
                                                          slot="Tomorrow", stage="slot_selected")
                result.is_successful.return_value = True
                if phase in {"readiness_output", "cart_output"}:
                    try:
                        Output(quantity="malformed")
                    except ValidationError as exc:
                        malformed = exc
                    type(checked if phase == "readiness_output" else result).structured_output = PropertyMock(side_effect=malformed)
                check = SimpleNamespace(run=AsyncMock(return_value=checked,
                                                     side_effect=failure if phase == "readiness" else None))
                agent = SimpleNamespace(run=AsyncMock(return_value=result,
                                                     side_effect=failure if phase == "cart_agent" else None))
                tools = MagicMock()
                tools.registry.registry.actions = {}
                module = SimpleNamespace(ActionResult=MagicMock(),
                                         Agent=MagicMock(side_effect=[check, agent]),
                                         Tools=MagicMock(side_effect=[tools, failure if phase == "cart_tools" else tools]))
                from shopping import write_result

                def writer(destination, data):
                    if phase == "result_output" and data["status"] != "error":
                        raise failure
                    write_result(destination, data)

                if phase == "persistence":
                    db.execute("DROP TABLE attempts")
                with patch.dict("sys.modules", {"browser_use": module}), \
                        patch.dict("os.environ", {"OPENAI_API_KEY": "private-openai",
                                                  "TELEGRAM_BOT_TOKEN": "private-telegram",
                                                  "TYPESAFE_API_KEY": "private-typesafe"}, clear=True), \
                        patch("shopping.open_db", return_value=db), patch("shopping.create_llm"), \
                        patch("shopping.create_browser", return_value=browser), \
                        patch("shopping.start_browser", new_callable=AsyncMock,
                              side_effect=failure if phase == "browser_startup" else None), \
                        patch("shopping.rank_alternatives", return_value=[],
                              side_effect=failure if phase == "cart_planning" else None), \
                        patch("shopping.assess", return_value=[],
                              side_effect=failure if phase == "assessment" else None), \
                        patch("shopping.write_result", side_effect=writer), \
                        patch("sys.stdin.isatty", return_value=False), \
                        patch("sys.argv", ["shopping.py", "shop", "--result-file", str(path)]), \
                        redirect_stderr(output), redirect_stdout(output):
                    with self.assertRaises(SystemExit) as exited:
                        main()
                    self.assertEqual(exited.exception.code, 2)
                data = json.loads(path.read_text())
                self.assertEqual(data["phase"], phase)
                self.assertEqual(data["status"], "error")
                self.assertIs(data["success"], False)
                if phase in {"readiness_output", "cart_output"}:
                    self.assertEqual(data["error_type"], "ValidationError")
                if phase in {"result_output", "cleanup"}:
                    self.assertEqual(data["attempt"], 1)
                    self.assertEqual(data["summary"], {**summary, "requested_items": requested_items, "missing_or_over_cap": []})
                for secret in ("private-openai", "private-telegram", "private-typesafe",
                               "unknown-bearer", "unknown-key", "sk-unknownsecret"):
                    self.assertNotIn(secret, json.dumps(data))
                    self.assertNotIn(secret, output.getvalue())
                browser.kill.assert_awaited_once()

    def test_error_result_bounds_and_unwritable_file(self):
        try:
            raise RuntimeError("x" * 3000)
        except RuntimeError as exc:
            data = error_result(exc, "cart_agent", "agent_failed")
        self.assertEqual(len(data["diagnostic"]["message"]), 2000)
        self.assertEqual(data["error_code"], "agent_failed")
        frame = data["diagnostic"]["frames"][-1]
        self.assertEqual(frame["file"], "test_shopping.py")
        self.assertIsInstance(frame["line"], int)
        self.assertEqual(frame["function"], "test_error_result_bounds_and_unwritable_file")
        with tempfile.TemporaryDirectory() as directory, \
                patch("shopping.open_db", side_effect=RuntimeError("private diagnostic")), \
                patch("sys.argv", ["shopping.py", "shop", "--result-file", directory]), \
                redirect_stderr(StringIO()) as output:
            with self.assertRaises(SystemExit) as exited:
                main()
            self.assertEqual(exited.exception.code, 2)
            self.assertNotIn("private diagnostic", output.getvalue())

    def test_shop_cli_does_not_catch_keyboard_interrupt_or_cancellation(self):
        for interruption in (KeyboardInterrupt(), asyncio.CancelledError()):
            with self.subTest(interruption=type(interruption).__name__), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "result.json"
                with patch("shopping.open_db", side_effect=interruption), \
                        patch("sys.argv", ["shopping.py", "shop", "--result-file", str(path)]):
                    with self.assertRaises(type(interruption)):
                        main()
                self.assertFalse(path.exists())

    def test_shop_cleanup_failure_does_not_replace_cancellation(self):
        browser = SimpleNamespace(kill=AsyncMock(side_effect=RuntimeError("cleanup failed")),
                                  browser_profile=SimpleNamespace(headless=True))
        with patch("shopping.create_llm"), patch("shopping.create_browser", return_value=browser), \
                patch("shopping.start_browser", new_callable=AsyncMock, side_effect=asyncio.CancelledError()), \
                patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(shop(SITE, [], None))
        browser.kill.assert_awaited_once()

    def test_list_and_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            db = open_db(Path(directory) / "shopping.db")
            with db:
                db.execute("INSERT INTO items VALUES (?, ?, ?, ?, ?, ?)",
                           ("milk 1 L", 2, "Almarai Fresh Milk Full Fat - 1L", "1000971", None, 9.0))
                db.execute("INSERT INTO alternatives VALUES (?, ?, ?)",
                           ("milk 1 L", "Nadec Fresh Milk Full Fat - 1L", "123"))
            selected = read_items(db)
            self.assertEqual(rank_alternatives(selected[0]), selected[0]["alternatives"])
            prompt = shopping_task("https://shop.tamimimarkets.com/", selected)
            self.assertIn("Shoppingsteps.json", prompt)
            self.assertIn("NEVER press that button", prompt)
            self.assertIn(PRODUCT_PLUS_SELECTOR, prompt)
            self.assertIn("clicks the plus SVG only with exactly one match", prompt)
            self.assertIn("Do NOT\nclick the header CHECKOUT control to add an item", prompt)
            self.assertIn("Nadec Fresh Milk", prompt)
            self.assertNotIn("Shoppingsteps.json", shopping_task("https://example.com", selected))
            cart = {"cart": [{"name": "Almarai Fresh Milk Full Fat - 1L", "quantity": 1, "unit_price": 7.0},
                             {"name": "Nadec Fresh Milk Full Fat - 1L", "quantity": 1, "unit_price": 8.0},
                              {"name": "unrelated", "quantity": 3, "unit_price": 4.0}]}
            cart["item_outcomes"] = [{"item_name": "milk 1 L", "selection_mode": "exact",
                                      "product": {"name": "Almarai Fresh Milk Full Fat - 1L", "sku": "1000971", "unit_price": 7.0}}]
            self.assertEqual(assess(selected, cart), [])
            cart["cart"][1]["unit_price"] = 10.0
            self.assertEqual(assess(selected, cart), ["milk 1 L"])
            cart["cart"][1]["name"] = "unapproved brand"
            self.assertEqual(assess(selected, cart), ["milk 1 L"])
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO alternatives VALUES (?, ?, ?)", ("missing", "anything", None))
            db.close()
            with self.assertRaises(ValueError):
                shopping_task("http://example.com", selected)

    def test_checkout_gate_and_cli(self):
        class Node:
            def __init__(self, text):
                self.text = text

            def get_meaningful_text_for_llm(self):
                return self.text

        controls = {1: Node("Proceed to Checkout"), 2: Node("09:00 AM - 10:00 AM"),
                    3: Node("Continue to payment"), 4: Node("CHECKOUT"), 5: Node("+")}
        self.assertFalse(blocked_action({"click": {"index": 1}},
                                        "https://shop.tamimimarkets.com/cart", controls))
        self.assertFalse(blocked_action({"click": {"index": 2}},
                                        "https://shop.tamimimarkets.com/slot", controls))
        self.assertTrue(blocked_action({"click": {"index": 3}},
                                        "https://shop.tamimimarkets.com/slot", controls))
        self.assertTrue(blocked_action({"click": {"index": 4}},
                                       "https://shop.tamimimarkets.com/product/milk", controls))
        self.assertFalse(blocked_action({"click": {"index": 5}},
                                        "https://shop.tamimimarkets.com/product/milk", controls))
        self.assertTrue(blocked_action({"click": {"coordinate_x": 5, "coordinate_y": 10}},
                                       "https://shop.tamimimarkets.com/slot", controls))
        self.assertTrue(blocked_action({"navigate": {"url": "https://shop.tamimimarkets.com/payment"}},
                                        "https://shop.tamimimarkets.com/slot", controls))
        setup_controls = {1: Node("Store Pickup or Home Delivery"), 2: Node("My Account"),
                          3: Node("Confirm address"), 4: Node("Add to cart"),
                          5: Node("Logout"), 6: Node("Proceed to Checkout")}
        for index in (1, 2, 3):
            self.assertFalse(blocked_setup_action({"click": {"index": index}}, SITE, setup_controls))
        for index in (4, 5, 6):
            self.assertTrue(blocked_setup_action({"click": {"index": index}}, SITE, setup_controls))
        self.assertTrue(blocked_setup_action({"click": {"index": 3}},
                                             SITE + "checkout", setup_controls))

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.db"

            def run(*args):
                with patch("sys.argv", ["shopping.py", "--db", str(db_path), *args]), redirect_stdout(StringIO()):
                    return main()

            self.assertEqual(run("add", "milk 1 L", "2"), 0)
            self.assertEqual(run("add", "milk 1 L", "3"), 0)
            self.assertEqual(run("prefer", "milk 1 L", "Almarai Milk - 1L", "--sku", "1000971",
                                 "--brand", "Almarai", "--max-price", "8"), 0)
            self.assertEqual(run("allow", "milk 1 L", "Nadec Milk - 1L"), 0)
            with open_db(db_path) as db:
                item = read_items(db)[0]
                self.assertEqual((item["quantity"], item["sku"], item["max_price"]), (3, "1000971", 8.0))
                self.assertEqual(item["alternatives"][0]["name"], "Nadec Milk - 1L")
                with db:
                    db.execute("INSERT INTO attempts(summary) VALUES ('{}')")
                    db.execute("INSERT INTO prices VALUES (?, ?, ?)", (1, "Almarai Milk - 1L", 7.0))
            self.assertEqual(run("confirm-purchase", "1"), 0)
            self.assertEqual(run("add", "bread", "2"), 0)
            self.assertEqual(run("allow", "bread", "Wholemeal Bread"), 0)
            with open_db(db_path) as db:
                self.assertIsNotNone(db.execute("SELECT purchased_at FROM attempts WHERE id = 1").fetchone()[0])
                self.assertEqual(len(read_items(db)), 2)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM alternatives").fetchone()[0], 2)
                attempts = [tuple(row) for row in db.execute("SELECT * FROM attempts ORDER BY id")]
                prices = [tuple(row) for row in db.execute("SELECT * FROM prices ORDER BY attempt_id, product_name")]
            for _ in range(2):
                self.assertEqual(run("clear"), 0)
                with open_db(db_path) as db:
                    self.assertEqual(read_items(db), [])
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM alternatives").fetchone()[0], 0)
                    self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM attempts ORDER BY id")], attempts)
                    self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM prices ORDER BY attempt_id, product_name")], prices)

    def test_jev_only_ranks_approved_alternatives(self):
        item = {"name": "milk", "preferred_name": "Almarai 1L", "brand": "Almarai",
                "alternatives": [{"name": "approved A", "sku": "1"},
                                 {"name": "approved B", "sku": "2"}]}
        response = {"answers": {"best": {"choice": "item_1", "confidence": 0.92}}}
        with patch("shopping.urlopen", return_value=BytesIO(json.dumps(response).encode())):
            self.assertEqual(rank_alternatives(item, "test-key")[0]["name"], "approved B")
        response["answers"]["best"]["choice"] = "item_7"
        with patch("shopping.urlopen", return_value=BytesIO(json.dumps(response).encode())):
            self.assertEqual(rank_alternatives(item, "test-key"), [])


if __name__ == "__main__":
    unittest.main()
