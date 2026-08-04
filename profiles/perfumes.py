CATEGORIES = [
    {"id": 780, "name": "Perfumes, Colonias y Fragancias"},
]

STORE_IDS = {
    9: "Falabella",
    11: "Paris",
    18: "Ripley",
}

# Tiendas de la CAPA RAPIDA (run_capas.py). Distinta de STORE_IDS a proposito:
# esta lista no responde "donde esta el catalogo" sino "donde podemos leer el
# precio del HTML sin navegador".
#
# Medido el 04-08-2026 sobre la categoria 780: de las 30 tiendas parseables del
# registro, solo estas 3 venden perfumeria. Paris queda fuera porque renderiza en
# cliente -- sigue cubierta por run.py, que no necesita leer su HTML.
#
# Falabella Marketplace (8147) es la novedad: es el ex-Linio, y SERNAC lo ubico
# primero en reclamos tras el CyberDay 2025 (7,04%), con "cancelaciones
# unilaterales por error en el precio" como queja principal. O sea que es, por
# medicion ajena, donde mas se equivocan los precios.
STORE_IDS_RAPIDA = {
    9: "Falabella",
    18: "Ripley",
    8147: "Falabella Marketplace",
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

# --- Capa rapida: umbrales calibrados con backtest.py --------------------
#
# Medido el 04-08-2026 sobre 60 entidades y 89 dias de pricing_history, con el
# baseline calculado de forma causal (solo pasado en cada punto):
#
#   ratio  puerta   total  raras  dudosas  ciclos  %util  al dia
#   0.50   p10          2      1        1       0   100%     0.1
#   0.50   ninguna      4      1        1       2    50%     0.2
#   0.60   p10          6      2        4       0   100%     0.3
#   0.70   p10         20      4       13       3    85%     1.1
#   0.75   p10         26      5       13       8    69%     1.5
#
# El 0.50 heredado de run.py daba 0,1 alertas/dia: una cada diez dias. La puerta
# p10 hace el trabajo fino -- al 0.60 elimina los 4 ciclos y deja 100% de utiles --
# asi que el ratio puede aflojarse sin ensuciar. 0.70 es donde el volumen se
# vuelve util (1,1/dia) sin que la precision caiga: 85%.
#
# En 0.75 la precision cae a 69%, asi que ahi esta el limite.
ALERT_MAX_RATIO_RAPIDA = 0.70
PUERTA_RAREZA = "p10"
