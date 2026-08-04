"""Reproduce la deteccion de la capa rapida sobre la historia ya existente.

Sirve para calibrar umbrales SIN esperar. La historia no hay que acumularla:
`/entities/{id}/pricing_history/` ya guarda 90-393 dias por entidad, con
`is_available` y `normal_price` por muestra. Basta con recorrerla.

CAUSALIDAD. Es lo unico que hace que el resultado signifique algo. En cada punto
de evaluacion el baseline se calcula usando SOLO las muestras anteriores a ese
punto (`resumen(pasado, ahora=t)`). Si se usara la ventana completa, el baseline
del dia 30 ya "sabria" lo que paso el dia 60 y todo saldria mejor de lo que sera
en produccion. Es el error clasico de backtest y aca esta cerrado por diseno.

Para EVALUAR lo disparado si se mira el futuro, y eso es correcto: la pregunta
no es "que sabiamos entonces" sino "resulto ser raro de verdad". Cada alerta se
etiqueta con el percentil de ese precio sobre la serie COMPLETA:

    rara    percentil <= 0.05   estuvo asi de barata casi nunca
    dudosa  0.05 - 0.15
    ciclo   percentil > 0.15    baja asi de seguido: es la promocion habitual

El caso que motivo esto: AMD Ryzen 5 4500 a -43% de su mediana, que cruzaba
cualquier umbral de ratio, pero ese precio aparecia en 17 de 61 muestras.

Uso:
    python backtest.py                      # los tres perfiles, 120 entidades c/u
    PERFILES=perfumes MUESTRA=250 python backtest.py
"""

import os
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

import capa_lenta
import extractores

# Cuantas entidades se muestrean por perfil. Cada pricing_history cuesta ~0,37s
# medidos, asi que esto define el tiempo de la corrida mas que ninguna otra cosa.
MUESTRA = int(os.environ.get("MUESTRA", "120"))

HILOS = 6

# Ventana de referencia a probar. Se expone porque no hay una sola correcta: en
# hardware los precios TIENDEN A LA BAJA (una GPU se deprecia), asi que sobre 90
# dias un "minimo historico" es lo normal y no discrimina nada. Una ventana corta
# compara contra el precio reciente y no contra el de hace tres meses.
VENTANA = int(os.environ.get("VENTANA", str(capa_lenta.VENTANA_DIAS)))

# Cuanta historia se BAJA, que puede ser mas que la ventana de referencia: para
# probar ventanas largas hay que tener las muestras antes de poder recortarlas.
HISTORIA = int(os.environ.get("HISTORIA", str(max(VENTANA, capa_lenta.VENTANA_DIAS))))

# Muestras minimas de pasado antes de empezar a evaluar. Con menos, el baseline
# se calcula sobre casi nada y dispara por ruido.
MIN_PASADO = 12

# Etiquetas de resultado, por percentil sobre la serie COMPLETA.
RARA = 0.05
CICLO = 0.15

# Combinaciones a comparar: (ratio maximo, puerta de rareza).
#
#   ninguno  solo el ratio contra la mediana
#   p10      ademas, mas barato que el 90% de su historia
#   minimo   ademas, mas barato que NUNCA antes en la ventana
#
# `minimo` existe por hardware: ahi los precios son ciclicos y la puerta p10 no
# alcanza, porque el ciclo baja tan seguido que su propio p10 queda dentro del
# ciclo. Ver la tabla que imprime este script.
SIN_PUERTA, P10, MINIMO = "ninguno", "p10", "minimo"

COMBOS = [
    (0.50, P10),
    (0.50, SIN_PUERTA),
    (0.60, P10),
    (0.60, SIN_PUERTA),
    (0.70, P10),
    (0.75, P10),
    (0.70, MINIMO),
    (0.80, MINIMO),
    (0.90, MINIMO),
]


def _percentil_total(filas, precio):
    """Percentil de un precio sobre la serie completa, ponderado por tiempo."""
    tramos = capa_lenta._tramos_disponibles(filas)
    return capa_lenta._percentil_ponderado(tramos, precio)


def backtest_entidad(filas):
    """Recorre la serie hacia adelante. Devuelve [(combo, precio, etiqueta)].

    Se evalua un punto solo cuando el precio CAMBIA respecto del anterior: en
    produccion la capa rapida tampoco evalua nada si la huella no cambio, asi que
    contar cada muestra inflaria el conteo con repeticiones del mismo hallazgo.
    """
    disparos = []
    anterior = None

    for i in range(MIN_PASADO, len(filas)):
        actual = filas[i]
        if not actual["disponible"]:
            continue
        if anterior is not None and actual["precio"] == anterior:
            continue
        anterior = actual["precio"]

        pasado = filas[:i]
        r = capa_lenta.resumen(pasado, actual["precio"], ahora=actual["ts"],
                               window_days=VENTANA)
        if not r or not r.get("baseline") or r.get("p10") is None:
            continue

        ratio = actual["precio"] / r["baseline"]
        puertas = {
            SIN_PUERTA: True,
            P10: actual["precio"] < r["p10"],
            MINIMO: actual["precio"] < r["minimo"],
        }

        for max_ratio, puerta in COMBOS:
            if ratio > max_ratio:
                continue
            if not puertas[puerta]:
                continue

            pct = _percentil_total(filas, actual["precio"])
            if pct is None:
                etiqueta = "?"
            elif pct <= RARA:
                etiqueta = "rara"
            elif pct <= CICLO:
                etiqueta = "dudosa"
            else:
                etiqueta = "ciclo"

            disparos.append(((max_ratio, puerta), actual["precio"], etiqueta))

    return disparos


def entidades_de(perfil, tope):
    """Muestra de entidades del perfil, repartida entre sus tiendas rapidas."""
    import importlib

    mod = importlib.import_module(f"profiles.{perfil}")
    tiendas = list(getattr(mod, "STORE_IDS_RAPIDA", mod.STORE_IDS).keys())
    min_precio = getattr(mod, "MIN_PRICE_CLP", 20000)

    por_tienda = max(tope // max(len(tiendas), 1), 4)
    salida = []

    for target in mod.CATEGORIES:
        for store_id in tiendas:
            if not extractores.es_parseable(store_id):
                continue
            try:
                lote = list(
                    capa_lenta._entidades_de_tienda(
                        target["id"], store_id, target.get("db_brand_id"),
                        page_size=100, max_pages=1, timeout=40,
                    )
                )
            except requests.RequestException:
                continue
            utiles = [
                e for e in lote
                if e["precio"] and e["disponible"] and e["precio"] >= min_precio
            ]
            salida += utiles[:por_tienda]
            if len(salida) >= tope:
                return salida[:tope]

    return salida[:tope]


def correr(perfil, tope=MUESTRA):
    ents = entidades_de(perfil, tope)
    print(f"\n{'='*66}\n{perfil}: {len(ents)} entidades muestreadas")
    if not ents:
        return None

    def traer(e):
        try:
            return capa_lenta.historial(e["entity_id"], dias=HISTORIA)
        except requests.RequestException:
            return []

    with ThreadPoolExecutor(max_workers=HILOS) as pool:
        series = [s for s in pool.map(traer, ents) if len(s) > MIN_PASADO]

    if not series:
        print("  sin historia suficiente")
        return None

    dias = max(
        (s[-1]["ts"] - s[0]["ts"]).days for s in series if len(s) > 1
    ) or 1
    print(f"  {len(series)} con historia | ventana observada {dias} dias")

    resultados = {}
    for serie in series:
        for combo, _, etiqueta in backtest_entidad(serie):
            r = resultados.setdefault(combo, {"rara": 0, "dudosa": 0, "ciclo": 0, "?": 0})
            r[etiqueta] += 1

    print(f"\n  {'ratio':<7}{'puerta':<9}{'total':>7}{'raras':>7}{'dudosas':>9}"
          f"{'ciclos':>8}{'%util':>7}{'al dia':>8}")
    print("  " + "-" * 65)

    filas = []
    for combo in COMBOS:
        r = resultados.get(combo)
        if not r:
            print(f"  {combo[0]:<7}{combo[1]:<9}{0:>7}")
            filas.append((combo, 0, 0, 0.0, 0.0))
            continue
        total = sum(r.values())
        util = r["rara"] + r["dudosa"]
        pct = (util / total * 100) if total else 0.0
        # Extrapolado a la watchlist real: la muestra cubre `len(series)` entidades
        # y la watchlist ronda las 300, sobre `dias` dias observados.
        al_dia = total / dias * (300 / len(series))
        print(f"  {combo[0]:<7}{combo[1]:<9}{total:>7}"
              f"{r['rara']:>7}{r['dudosa']:>9}{r['ciclo']:>8}{pct:>6.0f}%{al_dia:>8.1f}")
        filas.append((combo, total, util, pct, al_dia))

    return filas


def main(argv):
    perfiles = os.environ.get("PERFILES", "perfumes,hardware,celulares").split(",")
    for p in perfiles:
        try:
            correr(p.strip())
        except Exception as exc:
            print(f"  error en {p}: {exc}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
