import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "price_watch.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    product_id INTEGER PRIMARY KEY,
    name TEXT,
    category_id INTEGER,
    first_seen TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    price INTEGER,
    checked_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    price INTEGER,
    reason TEXT,
    sent_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_price_history_product ON price_history(product_id, checked_at);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def upsert_product(conn, product: dict):
    conn.execute(
        """
        INSERT INTO products (product_id, name, category_id)
        VALUES (:product_id, :name, :category_id)
        ON CONFLICT(product_id) DO UPDATE SET
            name = excluded.name,
            category_id = excluded.category_id
        """,
        product,
    )


def insert_price(conn, product: dict):
    conn.execute(
        "INSERT INTO price_history (product_id, price) VALUES (:product_id, :price)",
        product,
    )


def get_price_history(conn, product_id: int, limit: int = 30):
    rows = conn.execute(
        "SELECT price FROM price_history WHERE product_id = ? ORDER BY checked_at DESC LIMIT ?",
        (product_id, limit),
    ).fetchall()
    return [r["price"] for r in rows if r["price"] is not None]


def was_recently_alerted(conn, product_id: int, within_hours: int = 24) -> bool:
    row = conn.execute(
        "SELECT 1 FROM alerts WHERE product_id = ? AND sent_at >= datetime('now', ?) LIMIT 1",
        (product_id, f"-{within_hours} hours"),
    ).fetchone()
    return row is not None


def record_alert(conn, product_id: int, price: int, reason: str):
    conn.execute(
        "INSERT INTO alerts (product_id, price, reason) VALUES (?, ?, ?)",
        (product_id, price, reason),
    )
