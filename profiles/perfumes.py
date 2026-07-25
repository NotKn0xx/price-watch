CATEGORIES = [
    {"id": 780, "name": "Perfumes, Colonias y Fragancias"},
]

STORE_IDS = {
    9: "Falabella",
    11: "Paris",
    18: "Ripley",
}

# La categoria tiene ~2550 productos en estas 3 tiendas; con 3 paginas se
# miraba el 12% del catalogo. 40 paginas cubren todo con holgura.
MAX_PAGES_PER_CATEGORY = 40
PAGE_SIZE = 100
REQUEST_DELAY_SECONDS = 0.3

# Bajo este precio son mayoritariamente miniaturas y set de muestras: una
# caida del 60% ahi no es una falla de precio aprovechable.
MIN_PRICE_CLP = 20000

MAX_ALERTS_PER_RUN = 10
ALERT_COOLDOWN_HOURS = 24
REALERT_ON_EXTRA_DROP = 0.10
HISTORY_WINDOW_DAYS = 30
KEEP_HISTORY_DAYS = 90
