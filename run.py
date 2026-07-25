import importlib
import os
import time
import traceback

from dotenv import load_dotenv

load_dotenv()

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

PROFILE = os.environ.get("PROFILE", "perfumes")
profile = importlib.import_module(f"profiles.{PROFILE}")


def run_once():
    init_db()
    with get_conn() as conn:
        for target in profile.CATEGORIES:
            category_id = target["id"]
            category_name = target["name"]
            brand_id = target.get("brand_id")
            print(f"Escaneando categoria '{category_name}'...")
            try:
                for product in browse_category(
                    category_id,
                    profile.STORE_IDS.keys(),
                    brand_id=brand_id,
                    page_size=profile.PAGE_SIZE,
                    max_pages=profile.MAX_PAGES_PER_CATEGORY,
                ):
                    if product["price"] is None:
                        continue

                    history = get_price_history(conn, product["product_id"])
                    reason = check_anomaly(product, history)

                    upsert_product(conn, product)
                    insert_price(conn, product)

                    if reason and not was_recently_alerted(conn, product["product_id"]):
                        entity = best_entity_for_alert(product["product_id"], profile.STORE_IDS.keys())
                        if entity is None:
                            # Sin registro de precio activo en las tiendas filtradas: no es
                            # una oferta comprable ahora mismo, no vale la pena alertar.
                            continue
                        store_name = profile.STORE_IDS.get(entity["store_id"], "?")
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
            time.sleep(profile.REQUEST_DELAY_SECONDS)


if __name__ == "__main__":
    run_once()
