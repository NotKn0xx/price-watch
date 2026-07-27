"""Entrada de la capa francotirador: historial por tienda de la watchlist.

Se ejecuta aparte de `run.py`. `run.py` hace el censo amplio (todo el catalogo,
un precio agregado por producto); esto sigue de cerca unos pocos productos pero
guardando el precio de CADA tienda, que es lo unico que permite despues
distinguir un alza de una tienda de un alza de mercado.

Uso:
    PROFILE=perfumes DB_PATH=price_watch_perfumes.db python vigilar.py

La watchlist no se escribe a mano. Se deriva de la base: los productos con mas
movimiento de precio son justamente los que dan contenido, y la lista se
actualiza sola a medida que el catalogo cambia. WATCHLIST_SIZE la acota para no
pasarse del presupuesto de peticiones.
"""

import importlib
import os
import sys

from db import get_conn, init_db, prune_store_prices
from francotirador import vigilar


def _cfg(profile, nombre, defecto):
    valor = os.environ.get(nombre)
    if valor is not None:
        return type(defecto)(valor)
    return getattr(profile, nombre, defecto)


def elegir_watchlist(conn, limite, amplitud_minima=0.08):
    """Elige que productos seguir por tienda. Heuristica de arranque.

    NO ordenar por cantidad de tramos. `price_history` guarda el precio mas
    barato ENTRE tiendas, asi que cuando la tienda barata se queda sin stock el
    precio "sube" y al reponer "baja", sin que nadie haya cambiado nada. Medido
    en la base real: un perfume alterno entre $102.990 y $111.990 cuatro veces
    en dos dias y medio. Contar tramos elige justo esos productos: los mas
    ruidosos por stock, no los que tienen algo que contar.

    Se usa en cambio la AMPLITUD (max/min) y la cantidad de precios DISTINTOS.
    Un producto que rebota entre dos valores tiene muchos tramos pero solo dos
    precios distintos; uno que se movio de verdad recorre varios niveles.

    `amplitud_minima` descarta el ruido de centavos: bajo ~8% entre maximo y
    minimo no hay nada publicable.

    Es una heuristica de arranque, y hay que reemplazarla en cuanto store_prices
    tenga historia: entonces la watchlist se elige sobre series por tienda, que
    son estables, en vez de sobre la agregada, que no puede serlo.
    """
    filas = conn.execute(
        """
        SELECT product_id,
               COUNT(DISTINCT price) AS niveles,
               MIN(price) AS mn,
               MAX(price) AS mx
        FROM price_history
        GROUP BY product_id
        HAVING niveles > 1 AND mn > 0 AND (mx - mn) * 1.0 / mn >= ?
        ORDER BY niveles DESC, (mx - mn) * 1.0 / mn DESC, product_id
        LIMIT ?
        """,
        (amplitud_minima, limite),
    ).fetchall()
    return [r["product_id"] for r in filas]


def main():
    nombre = os.environ.get("PROFILE", "perfumes")
    profile = importlib.import_module(f"profiles.{nombre}")

    limite = _cfg(profile, "WATCHLIST_SIZE", 120)
    demora = _cfg(profile, "REQUEST_DELAY_SECONDS", 0.35)
    retencion = _cfg(profile, "KEEP_STORE_DAYS", 200)
    store_ids = set(profile.STORE_IDS)

    init_db()
    with get_conn() as conn:
        ids = elegir_watchlist(conn, limite)
        if not ids:
            print(f"[{nombre}] sin productos con movimiento todavia; nada que vigilar")
            return 0

        fallos = []
        sin_cambio, cambiados, fallidos = vigilar(
            conn, ids, store_ids=store_ids, demora=demora,
            al_fallar=lambda pid, exc: fallos.append((pid, str(exc)[:80])),
        )
        prune_store_prices(conn, keep_days=retencion)

        tiendas = conn.execute(
            "SELECT COUNT(DISTINCT store_id) FROM store_prices"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM store_prices").fetchone()[0]

    print(
        f"[{nombre}] watchlist {len(ids)} · cambios {cambiados} · sin cambio {sin_cambio} "
        f"· fallidos {fallidos} · acumulado {total} tramos en {tiendas} tiendas"
    )
    for pid, err in fallos[:5]:
        print(f"    fallo {pid}: {err}")

    # Que fallen algunos productos es normal (5xx puntuales). Que fallen todos
    # significa que la API cambio o nos bloquearon, y eso si debe romper la
    # corrida para que GitHub avise por correo.
    if ids and fallidos == len(ids):
        print("ERROR: fallaron TODAS las consultas. API caida, cambiada o bloqueo.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
