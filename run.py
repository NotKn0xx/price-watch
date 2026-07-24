import time
import traceback

from dotenv import load_dotenv

load_dotenv()

from config import CATEGORIES, MAX_PAGES_PER_CATEGORY, PAGE_SIZE, REQUEST_DELAY_SECONDS, STORE_IDS
from db import (
    get_conn,
    get_price_history,
    init_db,
    insert_price,
    record_alert,
    upsert_product,
    was_recently_alerted,
)
from detector import check_anomaly
from notifier import send_alert
from solotodo import best_entity_for_alert, browse_category

STORE_URL_BASE = "https://www.solotodo.cl/products/"


def run_once():
    init_db()
    with get_conn() as conn:
        for category_id, category_name in CATEGORIES.items():
            print(f"Escaneando categoria '{category_name}'...")
            try:
                for product in browse_category(
                    category_id,
                    STORE_IDS.keys(),
                    page_size=PAGE_SIZE,
                    max_pages=MAX_PAGES_PER_CATEGORY,
                ):
                    if product["price"] is None:
                        continue

                    history = get_price_history(conn, product["product_id"])
                    reason = check_anomaly(product, history)

                    upsert_product(conn, product)
                    insert_price(conn, product)

                    if reason and not was_recently_alerted(conn, product["product_id"]):
                        entity = best_entity_for_alert(product["product_id"], STORE_IDS.keys())
                        store_name = STORE_IDS.get(entity["store_id"], "?") if entity else "?"
                        url = entity["external_url"] if entity else f"{STORE_URL_BASE}{product['product_id']}"
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
