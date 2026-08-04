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

# Tiendas de la CAPA RAPIDA (run_capas.py). No es un ajuste menor de STORE_IDS:
# es una lista completamente distinta, y aca esta la razon medida.
#
# De las 6 tiendas de STORE_IDS solo Winpy entrega el precio en el HTML. PC
# Factory, Wei, UG Store, MegaBytes y Spider Store renderizan en cliente. Con una
# sola tienda no hay contra que corroborar, y la corroboracion cruzada -- una cae
# y el resto sostiene vs. caen todas -- es la senal que separa un error de precio
# de una campana. La capa rapida de hardware nacia ciega.
#
# Medido el 04-08-2026 sobre las 9 categorias del perfil: 6.745 entidades
# repartidas en 30 tiendas parseables. Estas son las de mayor catalogo.
#
# De paso resuelve algo que las grandes no dan: estas tiendas son justamente
# donde mas sobreviven los errores de precio, porque nadie las vigila. Falabella
# tiene equipo de pricing; Todoclick y Eylstore no.
#
# STORE_IDS se deja intacto: run.py sigue barriendo el catalogo por donde
# corresponde, que es otra pregunta.
STORE_IDS_RAPIDA = {
    3165: "Tecnomas.cl",
    45: "Winpy",
    264: "Todoclick",
    5903: "CTMAN",
    86: "SP Digital",
    8147: "Falabella Marketplace",
    3263: "Notebooksya!",
    257: "Nice One",
    6398: "Eylstore",
    56: "Infor-Ingen",
    128: "AllTec",
    955: "Tecno Master",
    5639: "CSByte",
    398: "CCLink",
    2438: "TecTec",
}

MAX_PAGES_PER_CATEGORY = 10
PAGE_SIZE = 100
REQUEST_DELAY_SECONDS = 0.3

# Mouse y teclados de $5.000 generan puro ruido.
MIN_PRICE_CLP = 20000

# Umbral mas sensible que en perfumes, y con razon medida: estas tiendas
# tienen mucha menos dispersion entre si (SSD 10% mediana / 21% p90 / 31%
# maximo; monitores 5% / 12% / 13%) y practicamente no usan el truco del
# "precio normal" inflado: 0% del catalogo aparece con >=20% de descuento
# declarado. Una caida del 40% aca ya es anomala de verdad.
ALERT_MAX_RATIO = 0.60

MAX_ALERTS_PER_RUN = 5
MAX_ALERTS_PER_DAY = 20
ALERT_COOLDOWN_HOURS = 24
REALERT_ON_EXTRA_DROP = 0.10
HISTORY_WINDOW_DAYS = 30
KEEP_HISTORY_DAYS = 90

# --- Capa rapida: umbrales calibrados con backtest.py --------------------
#
# Medido el 04-08-2026 sobre 90 entidades y 89 dias:
#
#   ratio  puerta   total  raras  dudosas  ciclos  %util  al dia
#   0.50   p10          2      0        0       2     0%     0.1
#   0.60   p10          2      0        0       2     0%     0.1
#   0.70   p10          5      0        0       5     0%     0.2
#   0.75   p10         12      2        4       6    50%     0.4
#   0.80   minimo      11      2        2       7    36%     0.4
#   0.90   minimo      23      7        6      10    57%     0.9
#
# ADVERTENCIA: hardware es el perfil con peor senal de los tres, y no por
# configuracion. Su precision topa en ~57% haga lo que se haga, porque los
# precios de componentes son ciclicos: bajan y suben de forma regular, asi que
# "barato contra su propia historia" no aisla anomalias.
#
# Se probo la hipotesis de que la tendencia a la baja (una GPU se deprecia)
# ensuciaba la referencia, recalculando con ventana de 30 dias en vez de 90. NO
# la mejoro: 57% en ambas. La hipotesis queda descartada, no confirmada.
#
# Se deja 0.90 + minimo porque a 57% igual entrega ~0,9 utiles/dia, contra 0% en
# cualquier umbral mas estricto. Pero cuenta con que ~4 de cada 10 alertas de
# hardware seran ciclos promocionales, y si molesta, subir a 0.75/p10 baja el
# volumen a 0,4/dia sin ganar precision.
ALERT_MAX_RATIO_RAPIDA = 0.90
PUERTA_RAREZA = "minimo"
