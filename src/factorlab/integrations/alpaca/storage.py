"""Durable external-order identities and activity ledger, independent of local matching."""
from contextlib import contextmanager
import json
import sqlite3

from factorlab.trading.storage import dumps, decode


def reduce_alpaca_event(db, event):
    """Apply original adapter admission and its original submission window."""
    if event['kind'] != 'PORTFOLIO_ORDER_ATTEMPT_RECORDED':
        raise ValueError('unsupported Alpaca ledger event')
    dispatch = event['dispatch']
    if dispatch['domain'] != 'alpaca':
        raise ValueError('unsupported dispatch domain')
    db.execute('INSERT INTO alpaca_orders VALUES (?,?,NULL,1,?)',
               (dispatch['client_order_id'], dispatch['intent'], dispatch['reservation']))
    db.execute('INSERT OR REPLACE INTO alpaca_meta VALUES (?,?)',
               ('submissions', json.dumps(dispatch['submissions'])))


class AlpacaStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS alpaca_meta (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS alpaca_orders (
                client_id TEXT PRIMARY KEY, intent TEXT NOT NULL, payload TEXT,
                uncertain INTEGER NOT NULL, reservation TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS alpaca_activities (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        """)

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def get(self, key):
        row = self.db.execute("SELECT payload FROM alpaca_meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, payload):
        self.db.execute("INSERT OR REPLACE INTO alpaca_meta VALUES (?,?)", (key, json.dumps(payload)))

    def order(self, client_id):
        row = self.db.execute("SELECT intent,payload,uncertain,reservation FROM alpaca_orders WHERE client_id=?",
                              (client_id,)).fetchone()
        return (decode(json.loads(row[0])), json.loads(row[1]) if row[1] else None, bool(row[2]), row[3]) if row else None

    def orders(self):
        return [(key, self.order(key)) for key, in self.db.execute("SELECT client_id FROM alpaca_orders").fetchall()]

    def admit(self, intent, reservation):
        self.db.execute("INSERT INTO alpaca_orders VALUES (?,?,NULL,1,?)",
                        (intent.client_order_id, dumps(intent), str(reservation)))

    def save_order(self, client_id, payload, *, uncertain=False):
        self.db.execute("UPDATE alpaca_orders SET payload=?,uncertain=? WHERE client_id=?",
                        (json.dumps(payload), int(uncertain), client_id))

    def uncertain(self, client_id):
        self.db.execute("UPDATE alpaca_orders SET uncertain=1 WHERE client_id=?", (client_id,))

    def activity(self, row):
        old = self.db.execute("SELECT payload FROM alpaca_activities WHERE id=?", (row["id"],)).fetchone()
        payload = json.dumps(row, sort_keys=True)
        if old and old[0] != payload:
            raise ValueError("official activity identity changed")
        self.db.execute("INSERT OR IGNORE INTO alpaca_activities VALUES (?,?)", (row["id"], payload))

    def activities(self):
        return tuple(json.loads(row[0]) for row in self.db.execute("SELECT payload FROM alpaca_activities ORDER BY id"))

    def close(self):
        self.db.close()
