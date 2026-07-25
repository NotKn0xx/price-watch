CATEGORIES = [
    {"id": 3, "name": "Procesadores"},
    {"id": 2, "name": "Tarjetas de video"},
    {"id": 7, "name": "Ram"},
    {"id": 8, "name": "Discos duros"},
    {"id": 39, "name": "Unidades de Estado Solido"},
    {"id": 4, "name": "Monitores"},
    {"id": 50, "name": "Audifonos y Headsets"},
    {"id": 40, "name": "Mouse"},
    {"id": 41, "name": "Teclados"},
]

# Falabella/Paris/Ripley casi no venden componentes de PC sueltos (procesadores,
# GPU, RAM); para hardware usamos tiendas especializadas.
STORE_IDS = {
    12: "PC Factory",
    183: "Spider Store",
    14: "Wei",
    45: "Winpy",
    392: "MegaBytes",
    280: "UG Store",
}

MAX_PAGES_PER_CATEGORY = 10
PAGE_SIZE = 100
REQUEST_DELAY_SECONDS = 0.3

# Mouse y teclados de $5.000 generan puro ruido.
MIN_PRICE_CLP = 20000

MAX_ALERTS_PER_RUN = 10
ALERT_COOLDOWN_HOURS = 24
REALERT_ON_EXTRA_DROP = 0.10
HISTORY_WINDOW_DAYS = 30
KEEP_HISTORY_DAYS = 90
