CATEGORIES = [
    # OJO: la clave es `db_brand_id` -> parametro `db_brands` de la API.
    # Con `brands` la API ignora el filtro y devuelve la categoria completa,
    # asi que ambas entradas escaneaban los mismos 160 equipos.
    {"id": 6, "name": "iPhone", "db_brand_id": 890},
    {"id": 6, "name": "Samsung", "db_brand_id": 996},
]

# Tiendas de la CAPA RAPIDA (run_capas.py). Ver la nota larga en hardware.py:
# esta lista es "donde podemos leer el precio del HTML", no "donde esta el
# catalogo". Paris queda fuera por renderizar en cliente; run.py la sigue cubriendo.
#
# Medido el 04-08-2026 sobre la categoria 6: 924 entidades en 15 tiendas
# parseables, contra las 2 utiles que daba STORE_IDS.
#
# OJO con Reuse (2471): vende REACONDICIONADOS. No se excluye porque el detector
# compara cada entidad contra SU PROPIA historia, asi que una tienda
# estructuralmente mas barata no genera falso positivo -- su baseline ya es bajo.
# Y el nombre de la entidad trae "Reacondicionado", asi que la alerta se
# autoetiqueta. Si igual molesta en Telegram, se saca de aca y listo.
STORE_IDS_RAPIDA = {
    9: "Falabella",
    18: "Ripley",
    8147: "Falabella Marketplace",
    264: "Todoclick",
    23: "MacOnline",
    45: "Winpy",
    3165: "Tecnomas.cl",
    7652: "Digitek",
    6101: "BackOnline",
    2471: "Reuse",
}

STORE_IDS = {
    9: "Falabella",
    11: "Paris",
    18: "Ripley",
}

MAX_PAGES_PER_CATEGORY = 10
PAGE_SIZE = 100
REQUEST_DELAY_SECONDS = 0.3

MIN_PRICE_CLP = 50000

# Dispersion medida entre tiendas: 14% mediana / 33% p90 / 37% maximo. El
# umbral del 50% deja margen sobre ese maximo, que es lo que importa porque
# el precio que seguimos es el minimo entre tiendas y un quiebre de stock
# lo mueve dentro de ese rango.
ALERT_MAX_RATIO = 0.50

MAX_ALERTS_PER_RUN = 5
MAX_ALERTS_PER_DAY = 20
ALERT_COOLDOWN_HOURS = 24
REALERT_ON_EXTRA_DROP = 0.10
HISTORY_WINDOW_DAYS = 30
KEEP_HISTORY_DAYS = 90
