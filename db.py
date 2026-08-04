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

-- Historial POR TIENDA, solo para la watchlist (capa francotirador).
--
-- `price_history` guarda un unico precio por producto: el mas barato entre las
-- tiendas del perfil. Sirve para alertar, pero no permite responder "subio en
-- una tienda o en todas?", que es la pregunta que separa una decision de esa
-- tienda de un alza de mercado (proveedor, dolar). Sin esa distincion no se
-- puede afirmar nada sobre alzas previas sin arriesgar acusar en falso.
--
-- No se puede tener para todo el catalogo: /products/browse/ NO devuelve
-- tiendas (verificado), asi que el detalle exige /products/{id}/entities/, una
-- peticion por producto. Aplicarlo a los 4.281 productos serian ~11.000
-- peticiones diarias extra contra la API gratuita de Solotodo. Por eso solo la
-- watchlist: 100-300 productos elegidos, que es de sobra para 3-4 videos por
-- semana.
--
-- Mismo formato de tramos comprimidos que price_history: se escribe solo
-- cuando el precio cambia, o el .db (que va en git) creceria sin control.
CREATE TABLE IF NOT EXISTS store_prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    store_id INTEGER NOT NULL,
    price INTEGER NOT NULL,
    normal_price INTEGER,
    url TEXT,
    first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_price_history_product ON price_history(product_id, first_seen);
CREATE INDEX IF NOT EXISTS idx_alerts_product ON alerts(product_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_alerts_sent ON alerts(sent_at);
CREATE INDEX IF NOT EXISTS idx_store_prices ON store_prices(product_id, store_id, first_seen);
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
        _soltar_watchlist(conn)


def _soltar_watchlist(conn):
    """Elimina la tabla `watchlist`, que estuvo brevemente en el .db.

    Se saco por un fallo real: el .db se commitea, y ante un push rechazado el
    workflow hace `git reset --hard` y reconstruye con reaplicar.py, que solo
    sabe de precios. La watchlist se perdia entera en cada conflicto y quedaba
    commiteada vacia, sin ruido. Ahora vive en el sidecar (estado_rapido.py),
    que es estado efimero y no versionado.

    Se dropea en vez de ignorarse para que un .db viejo no arrastre una tabla
    muerta que confunda a quien la vea.
    """
    conn.execute("DROP TABLE IF EXISTS watchlist")


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
    # Una sola observacion por producto, la mas barata. Hoy browse_category ya
    # deduplica con su set `emitted`, pero el invariante -- un solo tramo
    # vigente por producto -- es de la TABLA, no del llamador: si entraran dos
    # filas del mismo producto se abririan dos tramos vigentes y
    # open_segment_from() tomaria uno arbitrario. Mismo criterio que
    # flush_store_prices.
    unicas = {}
    for product_id, price in observations:
        previa = unicas.get(product_id)
        if previa is None or price < previa:
            unicas[product_id] = price

    unchanged, changed = 0, []
    for product_id, price in unicas.items():
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


def load_store_segments(conn, product_ids):
    """Tramos por (producto, tienda). {(product_id, store_id): [tramos...]}.

    Mismo criterio que load_segments: no filtra por fecha, porque el tramo
    vigente no reescribe `last_seen` y un filtro temporal descartaria justo las
    referencias mas estables. El recorte lo hace quien interpreta la serie.
    """
    out = {}
    ids = list(product_ids)
    if not ids:
        return out
    for chunk in _chunks(ids, _CHUNK):
        marcas = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""
            SELECT id, product_id, store_id, price, normal_price, url,
                   first_seen, last_seen
            FROM store_prices
            WHERE product_id IN ({marcas})
            ORDER BY product_id, store_id, first_seen
            """,
            tuple(chunk),
        ).fetchall()
        for r in rows:
            out.setdefault((r["product_id"], r["store_id"]), []).append(dict(r))
    return out


def flush_store_prices(conn, segments_by_key, observations):
    """Aplica observaciones por tienda. Cada una: (product_id, store_id, price,
    normal_price, url).

    Solo escribe cuando el precio de ESA tienda cambia. Una corrida sin cambios
    deja el archivo intacto byte a byte y no genera commit.

    La url se refresca al abrir tramo: las tiendas rotan sus URLs de producto y
    una url vieja es un link roto -- que en un video publicado es peor que no
    tener link.
    """
    # Una sola observacion por (producto, tienda): la mas barata. Dos filas de
    # la misma clave abririan dos tramos vigentes y romperian el invariante de
    # la tabla -- `load_store_segments` tomaria uno arbitrario y la serie
    # quedaria mezclando fichas distintas. observaciones_de() ya colapsa, pero
    # el invariante se defiende aca porque es de la tabla, no del llamador.
    unicas = {}
    for product_id, store_id, price, normal_price, url in observations:
        clave = (product_id, store_id)
        previa = unicas.get(clave)
        if previa is None or price < previa[0]:
            unicas[clave] = (price, normal_price, url)

    sin_cambio, cambiados = 0, []
    for (product_id, store_id), (price, normal_price, url) in unicas.items():
        tramos = segments_by_key.get((product_id, store_id))
        vigente = tramos[-1] if tramos else None
        if vigente and vigente["price"] == price:
            sin_cambio += 1
        else:
            cambiados.append(
                (product_id, store_id, price, normal_price, url,
                 vigente["id"] if vigente else None)
            )

    cerrar = [(c[5],) for c in cambiados if c[5] is not None]
    if cerrar:
        conn.executemany(
            "UPDATE store_prices SET last_seen = CURRENT_TIMESTAMP WHERE id = ?",
            cerrar,
        )
    if cambiados:
        conn.executemany(
            """INSERT INTO store_prices (product_id, store_id, price, normal_price, url)
               VALUES (?, ?, ?, ?, ?)""",
            [c[:5] for c in cambiados],
        )
    return sin_cambio, len(cambiados)


def prune_store_prices(conn, keep_days=200):
    """Retencion mas larga que price_history, a proposito.

    El detector de alzas previas necesita mirar 60-90 dias atras; con los 90 de
    price_history el margen seria cero y se estaria leyendo justo el borde de lo
    que ya se borro. Como la watchlist son cientos de productos y no miles, la
    retencion larga no infla el repo.

    Protege el tramo vigente de cada (producto, tienda) por la misma razon que
    prune_history: su last_seen es antiguo por diseno.
    """
    conn.execute(
        """
        DELETE FROM store_prices
        WHERE last_seen < datetime('now', ?)
          AND id NOT IN (SELECT MAX(id) FROM store_prices GROUP BY product_id, store_id)
        """,
        (f"-{keep_days} days",),
    )


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


def purge_foreign_categories(conn, category_ids):
    """Borra lo que quedo de categorias que el perfil ya no vigila.

    El perfil es la fuente de verdad sobre que se sigue. Si un .db se reutiliza
    tras redefinir los perfiles, arrastra productos de categorias ajenas que
    nunca vuelven a recibir precios: no generan alertas, pero engordan el
    archivo (que se commitea en cada cambio) y ensucian cualquier analisis.

    `prune_history` no los alcanza: protege siempre el tramo vigente de cada
    producto, y estos tienen exactamente uno.

    Devuelve cuantos productos se descartaron.
    """
    ids = sorted(set(category_ids))
    if not ids:
        return 0

    placeholders = ",".join("?" * len(ids))
    sobrantes = [
        r["product_id"]
        for r in conn.execute(
            f"SELECT product_id FROM products WHERE category_id NOT IN ({placeholders})",
            tuple(ids),
        )
    ]
    if not sobrantes:
        return 0

    for chunk in _chunks(sobrantes, _CHUNK):
        marcas = ",".join("?" * len(chunk))
        conn.execute(f"DELETE FROM price_history WHERE product_id IN ({marcas})", tuple(chunk))
        conn.execute(f"DELETE FROM alerts WHERE product_id IN ({marcas})", tuple(chunk))
        conn.execute(f"DELETE FROM products WHERE product_id IN ({marcas})", tuple(chunk))
    return len(sobrantes)


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
