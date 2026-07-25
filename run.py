import importlib
import os
import time
import traceback

from dotenv import load_dotenv

load_dotenv()

from db import (
    flush_prices,
    get_conn,
    init_db,
    load_open_segments,
    load_recent_alerts,
    load_segments,
    prune_history,
    record_alert,
    upsert_products,
)
from detector import check_anomaly
from notifier import send_alert
from solotodo import best_entity_for_alert, browse_category

PROFILE = os.environ.get("PROFILE", "perfumes")
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
profile = importlib.import_module(f"profiles.{PROFILE}")


def _cfg(name, default):
    return getattr(profile, name, default)


def scan_category(conn, target, store_ids):
    category_id = target["id"]
    category_name = target["name"]

    products = list(
        browse_category(
            category_id,
            store_ids,
            db_brand_id=target.get("db_brand_id"),
            page_size=_cfg("PAGE_SIZE", 100),
            max_pages=_cfg("MAX_PAGES_PER_CATEGORY", 50),
            page_delay=_cfg("REQUEST_DELAY_SECONDS", 0.3),
        )
    )
    if not products:
        print(f"  sin resultados en '{category_name}'")
        return []

    ids = [p["product_id"] for p in products]

    # Todo el estado de la categoria en 3 queries, en vez de una por producto.
    segments = load_segments(conn, ids, window_days=_cfg("HISTORY_WINDOW_DAYS", 30))
    open_segments = load_open_segments(conn, ids)
    recent_alerts = load_recent_alerts(conn, ids, within_hours=_cfg("ALERT_COOLDOWN_HOURS", 24))

    min_price = _cfg("MIN_PRICE_CLP", 0)
    redrop = _cfg("REALERT_ON_EXTRA_DROP", 0.10)
    candidates = []

    for product in products:
        finding = check_anomaly(product, segments.get(product["product_id"], []), min_price)
        if not finding:
            continue
        already = recent_alerts.get(product["product_id"])
        # Reabrimos la alerta si el precio siguio bajando de forma relevante;
        # antes un cooldown de 24h fijas se comia justamente la mejor caida.
        if already is not None and product["price"] > already * (1 - redrop):
            continue
        candidates.append((finding["ratio"], product, finding))

    upsert_products(conn, products)
    extended, inserted = flush_prices(
        conn, open_segments, [(p["product_id"], p["price"]) for p in products]
    )
    print(
        f"  {len(products)} productos | tramos: {extended} sin cambio, "
        f"{inserted} nuevos | {len(candidates)} candidatos"
    )

    candidates.sort(key=lambda c: c[0])  # ratio mas bajo = caida mas fuerte
    return [(p, f, category_name) for _, p, f in candidates]


def run_once():
    init_db()
    store_ids = list(profile.STORE_IDS.keys())
    max_alerts = _cfg("MAX_ALERTS_PER_RUN", 10)
    started = time.time()
    all_candidates = []

    with get_conn() as conn:
        for target in profile.CATEGORIES:
            print(f"Escaneando categoria '{target['name']}'...")
            try:
                all_candidates += scan_category(conn, target, store_ids)
            except Exception:
                print(f"  Error en categoria '{target['name']}':")
                traceback.print_exc()

        all_candidates.sort(key=lambda c: c[1]["ratio"])
        sent = 0

        for product, finding, category_name in all_candidates:
            if sent >= max_alerts:
                print(f"  (tope de {max_alerts} alertas por corrida alcanzado)")
                break

            entity = best_entity_for_alert(product["product_id"], store_ids)
            if entity is None:
                # Sin registro de precio activo y disponible en las tiendas
                # filtradas: no es una oferta comprable ahora mismo.
                continue

            store_name = profile.STORE_IDS.get(entity["store_id"], "?")
            url = entity["external_url"]
            msg = (
                f"Posible falla de precio [{category_name}]\n"
                f"{product['name']}\n"
                f"{finding['reason']}\n"
                f"Tienda: {store_name}\n"
                f"{url}"
            )

            if DRY_RUN:
                print("  [DRY RUN]", msg.replace("\n", " | "))
            else:
                send_alert(msg)
                record_alert(
                    conn,
                    product["product_id"],
                    product["price"],
                    finding["reason"],
                    entity["store_id"],
                    url,
                )
                print("  ALERTA:", product["name"])
            sent += 1

        if not DRY_RUN:
            prune_history(conn, keep_days=_cfg("KEEP_HISTORY_DAYS", 90))

    print(f"Listo en {time.time() - started:.1f}s | {sent if not DRY_RUN else 0} alertas enviadas")


if __name__ == "__main__":
    run_once()
