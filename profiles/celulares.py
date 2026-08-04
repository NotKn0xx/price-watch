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

# --- Capa rapida: umbrales calibrados con backtest.py --------------------
#
# Medido el 04-08-2026 sobre 75 entidades y 89 dias:
#
#   ratio  puerta   total  raras  dudosas  ciclos  %util  al dia
#   0.50   p10          0      -        -       -      -     0.0
#   0.60   p10          1      0        0       1     0%     0.0
#   0.70   p10          4      1        2       1    75%     0.2
#   0.75   p10          7      3        3       1    86%     0.3
#   0.80   minimo       9      5        3       1    89%     0.4
#   0.90   minimo      42     15      18        9    79%     1.9
#
# Esto explica por que celulares nunca alerto: con ALERT_MAX_RATIO=0.50 dispara
# CERO veces en 89 dias. No estaba roto, estaba fuera de rango.
#
# Se elige 0.80 + minimo (89% de utiles) antes que 0.90 (79% pero 1,9/dia): el
# problema que este proyecto tiene que evitar es el ruido, no la escasez.
# Ventana de referencia larga (ver hardware.py). Remedido sobre 399 dias:
#
#   0.70+p10   19 eventos  74% util  0.2/dia
#   0.75+p10   53 eventos  75% util  0.6/dia   <- elegida
#   0.80+min   68 eventos  60% util  0.7/dia
#
# Se prefiere 0.75+p10 sobre el 0.80+minimo de la calibracion anterior: aquel
# media 89% pero sobre 9 eventos, muestra demasiado chica para confiar en el
# porcentaje. 53 eventos al 75% es una medicion mas solida y mas volumen.
VENTANA_REFERENCIA_DIAS = 270

ALERT_MAX_RATIO_RAPIDA = 0.75
PUERTA_RAREZA = "p10"
