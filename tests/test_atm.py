import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend.app import create_app


class AtmIntegrationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.database = Path(folder.name) / "atm.sqlite3"
        self.app = create_app(self.database)
        self.app.testing = True
        self.client = self.app.test_client()

    def account(self, account_id=1, client=None):
        response = (client or self.client).get(f"/api/accounts/{account_id}")
        self.assertEqual(response.status_code, 200)
        return response.json["account"]

    def start(self, account_id=1):
        response = self.client.post(f"/api/accounts/{account_id}/sessions", json={})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["session"]["state"], "CARD_INSERTED")
        return self.action(response.json, "advance")

    def action(self, snapshot, action, expected=200, client=None, **data):
        session = snapshot["session"]
        response = (client or self.client).post(
            f"/api/sessions/{session['token']}/actions",
            json={"action": action, "revision": session["revision"], **data},
        )
        self.assertEqual(response.status_code, expected, response.get_data(as_text=True))
        return response.json

    def authenticate(self, account_id=1):
        return self.action(self.start(account_id), "submit_pin", pin="1234")

    def test_serves_the_app_and_rejects_invalid_requests(self):
        with self.client.get("/") as homepage, self.client.get("/app.js") as script:
            self.assertEqual(homepage.status_code, 200)
            self.assertIn(b'src="app.js"', homepage.data)
            self.assertEqual(script.status_code, 200)
            self.assertIn(b"/api/accounts/", script.data)
        self.assertEqual(self.client.get("/api/health").json, {"app": "toa-atm-simulator"})
        self.assertEqual(self.client.get("/api/accounts/6").status_code, 404)
        self.assertEqual(self.client.post("/api/accounts/1/reset").status_code, 415)
        pin_screen = self.start()
        self.action(pin_screen, "submit_amount", amount="100", expected=409)
        self.assertEqual(self.account()["balance_cents"], 100_000)

    def test_withdrawal_survives_restart_and_duplicate_submission(self):
        withdrawal = self.action(self.authenticate(), "withdrawal")
        for invalid in ("15", "510", "NaN", "100.01", "1e2"):
            withdrawal = self.action(withdrawal, "submit_amount", amount=invalid)
            self.assertEqual(withdrawal["transition"], "t6")
            self.assertEqual(withdrawal["account"]["balance_cents"], 100_000)

        result = self.action(withdrawal, "submit_amount", amount="100.00")
        self.assertEqual(result["session"]["state"], "DISPENSE")
        self.assertEqual(result["account"]["balance_cents"], 90_000)
        self.action(withdrawal, "submit_amount", amount="100.00", expected=409)
        restarted = create_app(self.database).test_client()
        self.assertEqual(self.account(client=restarted)["balance_cents"], 90_000)
        self.assertEqual([entry["type"] for entry in self.account(client=restarted)["history"]], ["withdrawal"])
        self.assertEqual(self.account(2, restarted)["balance_cents"], 100_000)

    def test_inquiry_cancellation_and_fee(self):
        confirmation = self.action(self.authenticate(), "balance_inquiry")
        menu = self.action(confirmation, "cancel_inquiry")
        self.assertEqual(menu["account"]["balance_cents"], 100_000)
        self.assertEqual(menu["account"]["history"], [])
        confirmation = self.action(menu, "balance_inquiry")
        inquiry = self.action(confirmation, "confirm_inquiry")
        self.assertEqual(inquiry["session"]["state"], "BALANCE_INQUIRY")
        self.assertEqual(inquiry["account"]["balance_cents"], 99_900)
        self.action(confirmation, "confirm_inquiry", expected=409)
        self.assertEqual(self.account()["history"][0]["amount_cents"], 100)

    def test_card_retention_preserves_funds_until_explicit_reset(self):
        withdrawal = self.action(self.authenticate(), "withdrawal")
        self.action(withdrawal, "submit_amount", amount="100")
        failed = self.start()
        for attempt in range(1, 4):
            failed = self.action(failed, "submit_pin", pin="1111")
            self.assertEqual(failed["session"]["pin_attempts"], attempt)
        self.assertEqual(failed["session"]["state"], "CARD_RETAINED")
        self.assertEqual(failed["transition"], "t10")
        self.action(failed, "submit_pin", pin="1234", expected=409)
        idle = self.action(failed, "advance")
        self.assertEqual(idle["session"]["state"], "IDLE")
        self.assertEqual(idle["account"]["balance_cents"], 90_000)
        self.assertEqual(idle["account"]["history"][-1]["type"], "withdrawal")

        reset = self.client.post("/api/accounts/1/reset", json={})
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(reset.json["account"], {"id": 1, "balance_cents": 100_000, "history": []})
        self.action(idle, "advance", expected=404)

    def test_concurrent_cards_cannot_overdraw_the_same_account(self):
        fee = self.action(self.authenticate(), "balance_inquiry")
        self.action(fee, "confirm_inquiry")
        first = self.action(self.authenticate(), "withdrawal")
        second = self.action(self.authenticate(), "withdrawal")

        def withdraw(snapshot):
            return self.action(snapshot, "submit_amount", amount="500", client=self.app.test_client())

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(withdraw, [first, second]))

        self.assertCountEqual([result["session"]["state"] for result in results], ["DISPENSE", "ENTER_AMOUNT"])
        self.assertTrue(any(result.get("error") == "Insufficient funds." for result in results))
        self.assertEqual(self.account()["balance_cents"], 49_900)
        self.assertEqual([entry["type"] for entry in self.account()["history"]], ["withdrawal", "balance_inquiry"])

    def test_locked_database_allows_safe_retry(self):
        self.app.config["SQLITE_TIMEOUT"] = 0.05
        withdrawal = self.action(self.authenticate(), "withdrawal")
        blocker = sqlite3.connect(self.database)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            session = withdrawal["session"]
            locked = self.client.post(
                f"/api/sessions/{session['token']}/actions",
                json={"action": "submit_amount", "revision": session["revision"], "amount": "100"},
            )
            self.assertEqual(locked.status_code, 503)
            self.assertEqual(locked.headers["Retry-After"], "1")
            self.assertIn("busy", locked.json["error"])
        finally:
            blocker.close()

        self.assertEqual(self.account()["balance_cents"], 100_000)
        retry = self.action(withdrawal, "submit_amount", amount="100")
        self.assertEqual(retry["account"]["balance_cents"], 90_000)
        self.assertEqual(len(retry["account"]["history"]), 1)


if __name__ == "__main__":
    unittest.main()
