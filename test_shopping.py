import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from shopping import PRODUCT_PLUS_SELECTOR, SITE, assess, blocked_action, blocked_setup_action, click_product_plus, main, open_db, rank_alternatives, read_items, setup, shop, shopping_task


class ShoppingTest(unittest.TestCase):
    def test_product_plus_exact_selector(self):
        control = SimpleNamespace(click=AsyncMock())
        page = SimpleNamespace(get_elements_by_css_selector=AsyncMock(return_value=[control]))
        browser = SimpleNamespace(get_current_page_url=AsyncMock(return_value=SITE + "product/milk"),
                                  get_current_page=AsyncMock(return_value=page))
        asyncio.run(click_product_plus(browser))
        page.get_elements_by_css_selector.assert_awaited_once_with(PRODUCT_PLUS_SELECTOR)
        control.click.assert_awaited_once_with()
        for matches in ([], [control, control]):
            control.click.reset_mock()
            page.get_elements_by_css_selector.return_value = matches
            with self.assertRaisesRegex(ValueError, "missing or ambiguous"):
                asyncio.run(click_product_plus(browser))
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
                        patch("sys.stdin.isatty", return_value=False), redirect_stdout(StringIO()):
                    browser.browser_profile.user_data_dir = profile
                    self.assertFalse(asyncio.run(shop(SITE, [], None, "Home")))
                self.assertEqual(module.Agent.call_count, expected_agents)
                module.ChatOpenAI.assert_called_once_with(model="local-model", base_url="https://llm.example.com/v1")
                for call in module.Agent.call_args_list:
                    self.assertIs(call.kwargs["llm"], module.ChatOpenAI.return_value)
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

    def test_list_and_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            db = open_db(Path(directory) / "shopping.db")
            with db:
                db.execute("INSERT INTO items VALUES (?, ?, ?, ?, ?, ?)",
                           ("milk 1 L", 2, "Almarai Fresh Milk Full Fat - 1L", "1000971", "Almarai", 9.0))
                db.execute("INSERT INTO alternatives VALUES (?, ?, ?)",
                           ("milk 1 L", "Nadec Fresh Milk Full Fat - 1L", "123"))
            selected = read_items(db)
            self.assertEqual(rank_alternatives(selected[0]), selected[0]["alternatives"])
            prompt = shopping_task("https://shop.tamimimarkets.com/", selected)
            self.assertIn("Shoppingsteps.json", prompt)
            self.assertIn("NEVER press that button", prompt)
            self.assertIn("svg > g > circle", prompt)
            self.assertIn("Do NOT\nclick the header CHECKOUT control to add an item", prompt)
            self.assertIn("Nadec Fresh Milk", prompt)
            self.assertNotIn("Shoppingsteps.json", shopping_task("https://example.com", selected))
            cart = {"cart": [{"name": "Almarai Fresh Milk Full Fat - 1L", "quantity": 1, "unit_price": 7.0},
                             {"name": "Nadec Fresh Milk Full Fat - 1L", "quantity": 1, "unit_price": 8.0},
                             {"name": "unrelated", "quantity": 3, "unit_price": 4.0}]}
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
