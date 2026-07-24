import time
import traceback

from dotenv import load_dotenv

load_dotenv()

from config import (
    CATEGORIES,
    CATEGORY_OUTLIER_IDS,
    MAX_PAGES_PER_CATEGORY,
    PAGE_SIZE,
    REQUEST_DELAY_SECONDS,
    STORE_IDS,
)
from db import (
    get_conn,
    get_price_history,
    init_db,
    insert_price,
    record_alert,
    upsert_product,
    was_recently_alerted,
)
from detector import check_anomaly, check_category_outlier
from notifier import send_alert
from solotodo import best_entity_for_alert, browse_category


def run_once():
    init_db()
    with get_conn() as conn:
        for category_id, category_name in CATEGORIES.items():
            print(f"Escaneando categoria '{category_name}'...")
            try:
                products = list(
                    browse_category(
                        category_id,
                        STORE_IDS.keys(),
                        page_size=PAGE_SIZE,
                        max_pages=MAX_PAGES_PER_CATEGORY,
                    )
                )
                category_prices = (
                    sorted(p["price"] for p in products if p["price"])
                    if category_id in CATEGORY_OUTLIER_IDS
                    else []
                )

                for product in products:
                    if product["price"] is None:
                        continue

                    history = get_price_history(conn, product["product_id"])
                    reason = check_anomaly(product, history)
                    if not reason and category_id in CATEGORY_OUTLIER_IDS:
                        reason = check_category_outlier(product, category_prices)

                    upsert_product(conn, product)
                    insert_price(conn, product)

                    if reason and not was_recently_alerted(conn, product["product_id"]):
                        entity = best_entity_for_alert(product["product_id"], STORE_IDS.keys())
                        if entity is None:
                            # Sin registro de precio activo en las tiendas filtradas: no es
                            # una oferta comprable ahora mismo, no vale la pena alertar.
                            continue
                        store_name = STORE_IDS.get(entity["store_id"], "?")
                        url = entity["external_url"]
                        msg = (
                            f"Posible falla de precio [{category_name}]\n"
                            f"{product['name']}\n"
                            f"{reason}\n"
                            f"Tienda: {store_name}\n"
                            f"{url}"
                        )
                        send_alert(msg)
                        record_alert(conn, product["product_id"], product["price"], reason)
                        print("  ALERTA:", product["name"])
            except Exception:
                print(f"  Error en categoria '{category_name}':")
                traceback.print_exc()
            time.sleep(REQUEST_DELAY_SECONDS)


if __name__ == "__main__":
    run_once()
