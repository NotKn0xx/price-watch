CATEGORIES = [
    {"id": 780, "name": "Perfumes, Colonias y Fragancias"},
]

STORE_IDS = {
    9: "Falabella",
    11: "Paris",
    18: "Ripley",
}

# La categoria tiene ~2515 productos en estas 3 tiendas; con 3 paginas se
# miraba el 12% del catalogo. 40 paginas cubren todo con holgura.
MAX_PAGES_PER_CATEGORY = 40
PAGE_SIZE = 100
REQUEST_DELAY_SECONDS = 0.3

# Bajo este precio son mayoritariamente miniaturas y sets de muestras: una
# caida del 60% ahi no es una falla de precio aprovechable.
MIN_PRICE_CLP = 20000

# Gatillo de alerta: el precio debe caer a la mitad o menos de su referencia.
# Medido en esta categoria, la dispersion de precio entre tiendas del mismo
# producto es 20% mediana / 40% p90 / 45% maximo, y ningun producto pasa de
# 50%. Como seguimos el minimo entre tiendas, un quiebre de stock en la tienda
# barata "sube" el precio dentro de ese rango; exigir >50% deja todo ese ruido
# bajo el umbral. Ademas el 9,3% del catalogo esta con >=20% de descuento de
# campana en cualquier momento, contra 0% sobre 50%.
ALERT_MAX_RATIO = 0.50

MAX_ALERTS_PER_RUN = 5
MAX_ALERTS_PER_DAY = 20
ALERT_COOLDOWN_HOURS = 24
REALERT_ON_EXTRA_DROP = 0.10
HISTORY_WINDOW_DAYS = 30
KEEP_HISTORY_DAYS = 90
