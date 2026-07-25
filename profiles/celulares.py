CATEGORIES = [
    # OJO: la clave es `db_brand_id` -> parametro `db_brands` de la API.
    # Con `brands` la API ignora el filtro y devuelve la categoria completa,
    # asi que ambas entradas escaneaban los mismos 160 equipos.
    {"id": 6, "name": "iPhone", "db_brand_id": 890},
    {"id": 6, "name": "Samsung", "db_brand_id": 996},
]

STORE_IDS = {
    9: "Falabella",
    11: "Paris",
    18: "Ripley",
}

MAX_PAGES_PER_CATEGORY = 10
PAGE_SIZE = 100
REQUEST_DELAY_SECONDS = 0.3

MIN_PRICE_CLP = 50000

MAX_ALERTS_PER_RUN = 10
ALERT_COOLDOWN_HOURS = 24
REALERT_ON_EXTRA_DROP = 0.10
HISTORY_WINDOW_DAYS = 30
KEEP_HISTORY_DAYS = 90
