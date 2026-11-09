import argparse
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.serving import WSGIRequestHandler, make_server

ROOT = Path(__file__).resolve().parents[1]
STARTING_BALANCE = 100_000
INQUIRY_FEE = 100
MAX_ATTEMPTS = 3
SESSION_LIFETIME = 15 * 60
DEMO_PIN = "1234"
HISTORY_LIMIT = 50
APP_ID = "toa-atm-simulator"
SESSION_LOG_PATH = re.compile(r"(/api/sessions/)[A-Za-z0-9_-]+(?=/actions(?:[?\s]|$))")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    balance_cents INTEGER NOT NULL CHECK (balance_cents >= 0)
);
CREATE TABLE IF NOT EXISTS atm_sessions (
    token TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    state TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    pin_attempts INTEGER NOT NULL DEFAULT 0 CHECK (pin_attempts BETWEEN 0 AND 3),
    message TEXT NOT NULL,
    transition TEXT,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    type TEXT NOT NULL,
    message TEXT NOT NULL,
    amount_cents INTEGER,
    balance_cents INTEGER NOT NULL CHECK (balance_cents >= 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS transaction_history ON transactions(account_id, id DESC);
CREATE INDEX IF NOT EXISTS session_expiration ON atm_sessions(updated_at);
"""

TRANSITIONS = {
    ("CARD_INSERTED", "advance"): ("ENTER_PIN", "Enter your four-digit demo PIN.", "t1"),
    ("TRANSACTION_MENU", "withdrawal"): ("ENTER_AMOUNT", "Enter an amount from $10 to $500 in multiples of $10.", "t4"),
    ("TRANSACTION_MENU", "balance_inquiry"): (
        "BALANCE_INQUIRY_CONFIRM", "A balance inquiry costs $1.00. Confirm to continue or cancel to return to the menu.", "t8"
    ),
    ("BALANCE_INQUIRY_CONFIRM", "cancel_inquiry"): ("TRANSACTION_MENU", "Inquiry cancelled. Choose a transaction.", "t9"),
    ("BALANCE_INQUIRY_CONFIRM", "back_to_menu"): ("TRANSACTION_MENU", "Inquiry cancelled. Choose a transaction.", "t9"),
    ("BALANCE_INQUIRY", "back_to_menu"): ("TRANSACTION_MENU", "Choose a transaction.", "t9"),
    ("DISPENSE", "advance"): ("EJECT_CARD", "Cash dispensed. Please take your card.", "t7"),
    ("EJECT_CARD", "advance"): ("IDLE", "Insert a card to begin.", "t12"),
    ("CARD_RETAINED", "advance"): ("IDLE", "Card retained. Insert a new demo card to try again.", "t12"),
}
EJECTABLE_STATES = {
    "CARD_INSERTED", "ENTER_PIN", "TRANSACTION_MENU", "ENTER_AMOUNT",
    "DISPENSE", "BALANCE_INQUIRY_CONFIRM", "BALANCE_INQUIRY",
}


def currency(cents):
    dollars, remainder = divmod(cents, 100)
    return "$" + f"{dollars:,}.{remainder:02d}"


def connect(path, timeout=5.0):
    db = sqlite3.connect(path, timeout=timeout)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def account_snapshot(db, account_id):
    account = db.execute("SELECT id, balance_cents FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if account is None:
        return None

    history = db.execute(
        """SELECT type, message, created_at, amount_cents, balance_cents
           FROM transactions WHERE account_id = ? ORDER BY id DESC LIMIT ?""",
        (account_id, HISTORY_LIMIT),
    )
    return {**dict(account), "history": [dict(event) for event in history]}


def session_snapshot(db, session, error=None):
    result = {
        "account": account_snapshot(db, session["account_id"]),
        "session": {key: session[key] for key in ("token", "state", "revision", "pin_attempts")},
        "message": session["message"],
        "transition": session["transition"],
    }
    if error is not None:
        result["error"] = error
    return result


def record_event(db, account_id, event_type, message, amount_cents=None):
    db.execute(
        """INSERT INTO transactions (account_id, type, message, amount_cents, balance_cents)
           SELECT id, ?, ?, ?, balance_cents FROM accounts WHERE id = ?""",
        (event_type, message, amount_cents, account_id),
    )


def withdrawal_amount(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,2})?", value) is None:
        return None
    whole, _, fraction = value.partition(".")
    cents = int(whole) * 100 + int(fraction.ljust(2, "0"))
    if not 1_000 <= cents <= 50_000 or cents % 1_000:
        return None
    return cents


def process_action(db, session, action, data):
    state = session["state"]
    account_id = session["account_id"]
    attempts = session["pin_attempts"]
    message = session["message"]
    transition = None
    error = None

    if action == "submit_pin" and state == "ENTER_PIN":
        pin = data.get("pin")
        if not isinstance(pin, str) or re.fullmatch(r"[0-9]{4}", pin) is None:
            error = "Enter a four-digit PIN."
        elif secrets.compare_digest(pin, DEMO_PIN):
            state, message, transition = "TRANSACTION_MENU", "PIN accepted. Choose a transaction.", "t2"
            attempts = 0
        else:
            attempts += 1
            if attempts == MAX_ATTEMPTS:
                state, transition = "CARD_RETAINED", "t10"
                message = "Too many incorrect PIN attempts. The ATM has retained your demo card."
                record_event(db, account_id, "card_retained", "Card retained after three incorrect PIN attempts.")
            else:
                remaining = MAX_ATTEMPTS - attempts
                transition = "t3"
                error = f"Incorrect PIN. Attempts remaining: {remaining}."
                record_event(db, account_id, "pin_failure", error)

    elif (state, action) in {
        ("ENTER_AMOUNT", "submit_amount"),
        ("BALANCE_INQUIRY_CONFIRM", "confirm_inquiry"),
    }:
        is_withdrawal = action == "submit_amount"
        amount = withdrawal_amount(data.get("amount")) if is_withdrawal else INQUIRY_FEE
        if amount is None:
            error = "Enter an amount from $10 to $500 in multiples of $10."
        else:
            updated = db.execute(
                """UPDATE accounts SET balance_cents = balance_cents - ?
                   WHERE id = ? AND balance_cents >= ?""",
                (amount, account_id, amount),
            )
            if updated.rowcount == 0:
                error = "Insufficient funds." if is_withdrawal else "Insufficient funds to pay the $1.00 inquiry fee."
            else:
                balance = db.execute("SELECT balance_cents FROM accounts WHERE id = ?", (account_id,)).fetchone()[0]
                if is_withdrawal:
                    state, transition, event_type = "DISPENSE", "t5", "withdrawal"
                    message = f"Cash dispensed: {currency(amount)}.\nRemaining balance: {currency(balance)}."
                else:
                    state, transition, event_type = "BALANCE_INQUIRY", "t13", "balance_inquiry"
                    message = f"Inquiry fee charged: {currency(amount)}.\nCurrent balance: {currency(balance)}."
                record_event(db, account_id, event_type, message.replace("\n", " "), amount)

        if error and is_withdrawal:
            transition = "t6"

    elif action == "eject" and state in EJECTABLE_STATES:
        state, message, transition = "EJECT_CARD", "Please take your card.", "t11"
    elif (state, action) in TRANSITIONS:
        state, message, transition = TRANSITIONS[state, action]
        if state == "IDLE":
            attempts = 0
    else:
        return None

    db.execute(
        """UPDATE atm_sessions SET state = ?, message = ?, transition = ?, pin_attempts = ?,
           revision = revision + 1, updated_at = ? WHERE token = ?""",
        (state, message, transition, attempts, int(time.time()), session["token"]),
    )
    updated_session = db.execute("SELECT * FROM atm_sessions WHERE token = ?", (session["token"],)).fetchone()
    return session_snapshot(db, updated_session, error)


def create_app(database=None):
    app = Flask(__name__, static_folder=None)
    app.config.update(
        MAX_CONTENT_LENGTH=4096,
        SQLITE_TIMEOUT=5.0,
        TRUSTED_HOSTS=["127.0.0.1", "localhost", "[::1]"],
    )

    path = Path(database or os.environ.get("ATM_DB_PATH") or ROOT / "data" / "atm.sqlite3")
    path.parent.mkdir(parents=True, exist_ok=True)
    db = connect(path)
    try:
        db.executescript(SCHEMA)
        with db:
            db.executemany(
                "INSERT OR IGNORE INTO accounts (id, balance_cents) VALUES (?, ?)",
                ((account_id, STARTING_BALANCE) for account_id in range(1, 6)),
            )
    finally:
        db.close()

    def database_connection():
        if "db" not in g:
            g.db = connect(path, timeout=app.config["SQLITE_TIMEOUT"])
        return g.db

    @app.teardown_appcontext
    def close_database(_error):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.errorhandler(sqlite3.OperationalError)
    def handle_sqlite_error(error):
        code = getattr(error, "sqlite_errorcode", None)
        if code is not None:
            busy = (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
        else:
            # Python 3.10 SQLite exceptions expose the message only.
            busy = str(error).startswith(("database is locked", "database table is locked", "database schema is locked"))
        if not busy:
            raise error
        return jsonify(error="The account database is busy. Wait a moment and retry."), 503, {"Retry-After": "1"}

    @app.before_request
    def require_json():
        if request.path.startswith("/api/") and request.method == "POST":
            if not request.is_json:
                return jsonify(error="Send a JSON request."), 415
            if not isinstance(request.get_json(silent=True), dict):
                return jsonify(error="Send a valid JSON object."), 400

    @app.after_request
    def disable_api_cache(response):
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    @app.get("/project.html")
    def home():
        return send_from_directory(ROOT / "frontend", "project.html")

    @app.get("/app.js")
    def frontend_script():
        return send_from_directory(ROOT / "frontend", "app.js")

    @app.get("/api/health")
    def health():
        return jsonify(app=APP_ID)

    @app.get("/api/accounts/<int:account_id>")
    def get_account(account_id):
        db = database_connection()
        with db:
            db.execute("BEGIN")
            account = account_snapshot(db, account_id)
            if account is None:
                return jsonify(error="Demo account not found."), 404
            return jsonify(account=account)

    @app.post("/api/accounts/<int:account_id>/sessions")
    def insert_card(account_id):
        db = database_connection()
        with db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone() is None:
                return jsonify(error="Demo account not found."), 404

            now = int(time.time())
            db.execute("DELETE FROM atm_sessions WHERE updated_at <= ?", (now - SESSION_LIFETIME,))
            token = secrets.token_urlsafe(32)
            db.execute(
                """INSERT INTO atm_sessions (token, account_id, state, message, transition, updated_at)
                   VALUES (?, ?, 'CARD_INSERTED', 'Card inserted. Reading card…', 't0', ?)""",
                (token, account_id, now),
            )
            session = db.execute("SELECT * FROM atm_sessions WHERE token = ?", (token,)).fetchone()
            return jsonify(session_snapshot(db, session)), 201

    @app.post("/api/sessions/<token>/actions")
    def act(token):
        data = request.get_json()
        action, revision = data.get("action"), data.get("revision")
        if not isinstance(action, str) or type(revision) is not int or revision < 0:
            return jsonify(error="Supply an action and its nonnegative integer revision."), 400

        db = database_connection()
        with db:
            db.execute("BEGIN IMMEDIATE")
            session = db.execute("SELECT * FROM atm_sessions WHERE token = ?", (token,)).fetchone()
            if session is None or session["updated_at"] <= int(time.time()) - SESSION_LIFETIME:
                return jsonify(error="This card session expired or was reset. Insert a card to continue."), 404
            if revision != session["revision"]:
                return jsonify(session_snapshot(db, session, "The session changed. The latest state is shown.")), 409

            result = process_action(db, session, action, data)
            if result is None:
                return jsonify(session_snapshot(db, session, "This action is unavailable in the current state.")), 409
            return jsonify(result)

    @app.post("/api/accounts/<int:account_id>/reset")
    def reset_account(account_id):
        db = database_connection()
        with db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute("UPDATE accounts SET balance_cents = ? WHERE id = ?", (STARTING_BALANCE, account_id))
            if updated.rowcount == 0:
                return jsonify(error="Demo account not found."), 404

            db.execute("DELETE FROM atm_sessions WHERE account_id = ?", (account_id,))
            db.execute("DELETE FROM transactions WHERE account_id = ?", (account_id,))
            return jsonify(
                account=account_snapshot(db, account_id),
                message="Demo account reset. Balance restored to $1,000.00.",
            )

    return app


class AtmRequestHandler(WSGIRequestHandler):
    def log(self, log_type, message, *args):
        args = tuple(SESSION_LOG_PATH.sub(r"\1[redacted]", arg) if isinstance(arg, str) else arg for arg in args)
        super().log(log_type, message, *args)


def main():
    parser = argparse.ArgumentParser(description="Run the local ATM simulator.")
    parser.add_argument("--open-browser", action="store_true", help="Open the simulator in your default browser.")
    args = parser.parse_args()
    host = "127.0.0.1"
    port = int(os.environ.get("ATM_PORT", "8000"))
    url = f"http://{host}:{port}/"

    if args.open_browser:
        try:
            with urlopen(url + "api/health", timeout=1) as response:
                existing_app = json.load(response).get("app")
        except (URLError, OSError, ValueError, AttributeError):
            existing_app = None

        if existing_app == APP_ID:
            print(f"ATM simulator is already running at {url}", flush=True)
            webbrowser.open(url)
            return

    with make_server(host, port, create_app(), threaded=True, request_handler=AtmRequestHandler) as server:
        print(f"ATM simulator is ready at {url}. Press Ctrl+C to stop it.", flush=True)
        if args.open_browser:
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        server.serve_forever()


if __name__ == "__main__":
    main()
