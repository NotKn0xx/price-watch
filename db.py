import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / os.environ.get("DB_PATH", "price_watch.db")

# Tope de variables por statement en SQLite.
_CHUNK = 900

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    product_id INTEGER PRIMARY KEY,
    name TEXT,
    category_id INTEGER,
    first_seen TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Serie de precios COMPRIMIDA: una fila por tramo de precio constante, no una
-- por corrida. Guardar una fila por producto por corrida agregaba decenas de
-- miles de filas al dia y, como el .db se commitea, hacia crecer el repo sin
-- control.
--
-- `last_seen` se escribe solo al CERRAR el tramo (cuando el precio cambia).
-- En el tramo vigente queda igual a `first_seen` y eso es correcto: la
-- duracion de cada tramo se deduce del inicio del siguiente, y la del ultimo
-- se extiende hasta ahora. Asi una corrida sin cambios de precio no escribe
-- nada y el archivo queda identico byte a byte.
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    price INTEGER NOT NULL,
    first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    price INTEGER,
    reason TEXT,
    store_id INTEGER,
    url TEXT,
    sent_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_price_history_product ON price_history(product_id, first_seen);
CREATE INDEX IF NOT EXISTS idx_alerts_product ON alerts(product_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_alerts_sent ON alerts(sent_at);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # El .db vive en git: journal_mode=DELETE evita dejar archivos -wal/-shm
    # sueltos que romperian el commit del historial.
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        _migrate_price_history(conn)
        conn.executescript(SCHEMA)


def _migrate_price_history(conn):
    """Convierte el esquema viejo (una fila por corrida) al comprimido."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='price_history'"
    ).fetchone()
    if not exists:
        return
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(price_history)")}
    if "last_seen" in cols:
        return  # ya migrado

    conn.execute("ALTER TABLE price_history RENAME TO price_history_old")
    conn.executescript(SCHEMA)
    # Colapsa corridas consecutivas con el mismo precio en un solo tramo.
    conn.execute(
        """
        INSERT INTO price_history (product_id, price, first_seen, last_seen)
        SELECT product_id, price, MIN(checked_at), MAX(checked_at)
        FROM (
            SELECT product_id, price, checked_at,
                   ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY checked_at)
                 - ROW_NUMBER() OVER (PARTITION BY product_id, price ORDER BY checked_at) AS grp
            FROM price_history_old
            WHERE price IS NOT NULL
        )
        GROUP BY product_id, price, grp
        """
    )
    conn.execute("DROP TABLE price_history_old")

    alert_cols = {r["name"] for r in conn.execute("PRAGMA table_info(alerts)")}
    for col in ("store_id INTEGER", "url TEXT"):
        if col.split()[0] not in alert_cols:
            conn.execute(f"ALTER TABLE alerts ADD COLUMN {col}")


def upsert_products(conn, products):
    # El WHERE evita reescribir filas identicas: sin el, cada corrida ensuciaba
    # todas las paginas del archivo y git commiteaba un blob nuevo aunque no
    # hubiera cambiado nada.
    conn.executemany(
        """
        INSERT INTO products (product_id, name, category_id)
        VALUES (:product_id, :name, :category_id)
        ON CONFLICT(product_id) DO UPDATE SET
            name = excluded.name,
            category_id = excluded.category_id
        WHERE products.name IS NOT excluded.name
           OR products.category_id IS NOT excluded.category_id
        """,
        products,
    )


def load_segments(conn, product_ids):
    """Tramos de precio por producto, en una sola query por chunk.

    Antes esto era un SELECT por producto (1634 queries por corrida en
    perfumes). Devuelve {product_id: [{price, first_seen, last_seen}]}.

    No filtra por ventana a proposito: como el tramo vigente no actualiza
    `last_seen`, un producto estable hace 40 dias tiene `last_seen` viejo y un
    filtro por fecha lo dejaria fuera -- perdiendo justo las referencias mas
    solidas. El recorte a la ventana lo hace el detector, que sabe interpretar
    la duracion de cada tramo. La tabla ya viene acotada por prune_history.
    """
    out = {}
    ids = list(product_ids)
    if not ids:
        return out
    for chunk in _chunks(ids, _CHUNK):
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""
            SELECT id, product_id, price, first_seen, last_seen
            FROM price_history
            WHERE product_id IN ({placeholders})
            ORDER BY product_id, first_seen
            """,
            tuple(chunk),
        ).fetchall()
        for r in rows:
            out.setdefault(r["product_id"], []).append(dict(r))
    return out


def open_segment_from(segments):
    """Ultimo tramo (el vigente) de una lista ya ordenada por first_seen."""
    return segments[-1] if segments else None


def flush_prices(conn, segments_by_product, observations):
    """Aplica las observaciones (product_id, price) de esta corrida.

    Solo escribe cuando el precio CAMBIA: cierra el tramo anterior y abre uno
    nuevo. Si el precio sigue igual no se toca nada, y por eso una corrida sin
    novedades deja el archivo intacto y no genera commit.
    """
    unchanged, changed = 0, []
    for product_id, price in observations:
        current = open_segment_from(segments_by_product.get(product_id))
        if current and current["price"] == price:
            unchanged += 1
        else:
            changed.append((product_id, price, current["id"] if current else None))

    to_close = [(c[2],) for c in changed if c[2] is not None]
    if to_close:
        conn.executemany(
            "UPDATE price_history SET last_seen = CURRENT_TIMESTAMP WHERE id = ?",
            to_close,
        )
    if changed:
        conn.executemany(
            "INSERT INTO price_history (product_id, price) VALUES (?, ?)",
            [(c[0], c[1]) for c in changed],
        )
    return unchanged, len(changed)


def prune_history(conn, keep_days=90):
    """Descarta tramos viejos, preservando siempre el vigente de cada producto.

    El tramo vigente queda protegido por el NOT IN: su `last_seen` es antiguo
    por diseno (no se reescribe mientras el precio no cambie) y borrarlo seria
    perder la referencia del producto.
    """
    conn.execute(
        """
        DELETE FROM price_history
        WHERE last_seen < datetime('now', ?)
          AND id NOT IN (SELECT MAX(id) FROM price_history GROUP BY product_id)
        """,
        (f"-{keep_days} days",),
    )
    conn.execute("DELETE FROM alerts WHERE sent_at < datetime('now', ?)", (f"-{keep_days} days",))


def load_recent_alerts(conn, product_ids, within_hours=24):
    """{product_id: menor precio ya alertado en la ventana}."""
    out = {}
    ids = list(product_ids)
    if not ids:
        return out
    for chunk in _chunks(ids, _CHUNK):
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""
            SELECT product_id, MIN(price) AS min_price
            FROM alerts
            WHERE product_id IN ({placeholders})
              AND sent_at >= datetime('now', ?)
              AND price IS NOT NULL
            GROUP BY product_id
            """,
            (*chunk, f"-{within_hours} hours"),
        ).fetchall()
        for r in rows:
            out[r["product_id"]] = r["min_price"]
    return out


def count_alerts_since(conn, hours=24) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM alerts WHERE sent_at >= datetime('now', ?)",
        (f"-{hours} hours",),
    ).fetchone()
    return row["n"] if row else 0


def record_alert(conn, product_id, price, reason, store_id=None, url=None):
    conn.execute(
        "INSERT INTO alerts (product_id, price, reason, store_id, url) VALUES (?, ?, ?, ?, ?)",
        (product_id, price, reason, store_id, url),
    )


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]
