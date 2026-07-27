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
# Como seguimos el minimo entre tiendas, un quiebre de stock en la tienda barata
# "sube" el precio y al reponer lo "baja" de golpe; el umbral existe para dejar
# ese ruido por debajo. Ademas el 9,3% del catalogo esta con >=20% de descuento
# de campana en cualquier momento, contra 0% sobre 50%.
#
# CORRECCION (27-07-2026): la dispersion entre tiendas que estaba anotada aqui
# (20% mediana / 40% p90 / 45% maximo, ninguno sobre 50%) se midio sobre datos
# agregados. Con precios reales POR TIENDA da mediana 3%, p90 50%, maximo 260%,
# y el 10% supera el 50% -- la cola es mucho peor de lo que decia.
#
# El 0.50 se mantiene igual: de 45 productos solo 1 podia cruzarlo por quiebre
# de stock, y ese ya quedaba fuera por MIN_PRICE_CLP. Riesgo real ~2%.
# Si se baja MIN_PRICE_CLP, rehacer la medicion antes.
ALERT_MAX_RATIO = 0.50

MAX_ALERTS_PER_RUN = 5
MAX_ALERTS_PER_DAY = 20
ALERT_COOLDOWN_HOURS = 24
REALERT_ON_EXTRA_DROP = 0.10
HISTORY_WINDOW_DAYS = 30
KEEP_HISTORY_DAYS = 90
