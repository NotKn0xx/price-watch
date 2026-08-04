"""Orquestador de las dos capas.

    capa lenta   Solotodo /entities/ + /pricing_history/   cada ~12h
                 -> catalogo, historia, baseline, volatilidad, watchlist

    capa rapida  la tienda directo, sobre la watchlist       cada 10 min
                 -> precio de ahora, contra la referencia que dejo la lenta

La division no es por gusto: Solotodo remuestrea cada ~4h (mediana 4,06h / p90
13,2h) y el Worker de cloudflare-cron ya dispara cada 10 min. Consultar a Solotodo
cada 10 minutos es pedir 24 veces lo mismo. La capa lenta aporta lo que solo ella
tiene -- 393 dias de historia por entidad y 142 tiendas de cobertura -- y la
rapida aporta lo unico que Solotodo no puede dar: latencia.

Uso:
    PROFILE=perfumes python run_capas.py            # decide sola que capa toca
    PROFILE=perfumes MODO=lenta python run_capas.py # fuerza refresco de watchlist
    PROFILE=perfumes DRY_RUN=1 python run_capas.py
"""

import importlib
import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import requests
from dotenv import load_dotenv

load_dotenv()

import capa_lenta
import capa_rapida
import estado_rapido
import extractores
import vigilancia
from db import (
    count_alerts_since,
    get_conn,
    init_db,
    load_recent_alerts,
    record_alert,
)
from notifier import send_alert

PROFILE = os.environ.get("PROFILE", "perfumes")
MODO = os.environ.get("MODO", "").lower()
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
profile = importlib.import_module(f"profiles.{PROFILE}")

# Cada cuanto se refresca la watchlist. 12h es holgado contra las ~4h de remuestreo
# de Solotodo: pedirla mas seguido no trae datos nuevos.
HORAS_WATCHLIST = 12

# Tope de entidades a las que se les pide historia por pasada. Cada pricing_history
# cuesta ~0,37s medidos; sin tope una categoria grande se comeria la corrida entera.
# Las que no entran esperan a la siguiente pasada (ver la rotacion mas abajo).
MAX_HISTORIAS = 400

# Hilos para pedir historia. Solotodo no mostro rate limit en 12 llamadas seguidas,
# pero eso no es licencia para abrir 50 conexiones: es una API gratuita ajena.
HILOS_HISTORIA = 6


def _cfg(nombre, defecto):
    return getattr(profile, nombre, defecto)


# --------------------------------------------------------------------------
# Capa lenta
# --------------------------------------------------------------------------

def _candidatas(category_id, store_ids, min_precio, db_brand_id=None):
    """Entidades que vale la pena mirar antes de gastar una peticion de historia.

    El filtro es deliberadamente barato: tienda parseable, disponible y sobre el
    piso de precio. Todo lo demas necesita historia, y la historia es lo caro.

    `db_brand_id` importa en celulares, cuyo perfil declara dos entradas sobre la
    MISMA categoria 6 (iPhone y Samsung) diferenciadas solo por marca. Sin pasarlo,
    las dos entradas enumeran la categoria completa y se escanean 25.846 entidades
    dos veces en vez de 4.065 una vez por marca.
    """
    for e in capa_lenta.entidades(category_id, store_ids, db_brand_id=db_brand_id):
        if not e["precio"] or not e["disponible"] or not e["url"]:
            continue
        if e["precio"] < min_precio:
            continue
        if not extractores.es_parseable(e["store_id"]):
            continue
        e["category_id"] = category_id
        yield e


def _rotar(entidades, disparo, tope=MAX_HISTORIAS):
    """Recorta a `tope` rotando el punto de partida entre pasadas.

    Cortar siempre por el principio dejaria la cola de la categoria sin historia
    para siempre: esas entidades nunca entrarian a la watchlist y no seria visible
    que faltan. Rotando, la cobertura completa se alcanza en varias pasadas.
    """
    if len(entidades) <= tope:
        return entidades
    inicio = (disparo * tope) % len(entidades)
    doble = entidades + entidades
    return doble[inicio : inicio + tope]


def refrescar_watchlist(disparo):
    """Corre la capa lenta y deja la watchlist lista para la rapida.

    Usa STORE_IDS_RAPIDA y no STORE_IDS, y la distincion importa: las dos listas
    responden a preguntas distintas.

    STORE_IDS lo elige run.py por cobertura de catalogo -- donde esta el producto.
    Aca hace falta lo otro: donde podemos LEER el precio del HTML. Medido, la
    diferencia era brutal en hardware, que apuntaba a las grandes de componentes
    y de sus 6 tiendas solo Winpy sirve; las otras 5 renderizan en cliente. Con
    una sola tienda no hay con que corroborar, que es la senal que da valor a
    todo esto, asi que la capa rapida de hardware nacia ciega.
    """
    store_ids = list(_cfg("STORE_IDS_RAPIDA", profile.STORE_IDS).keys())
    min_precio = _cfg("MIN_PRICE_CLP", vigilancia.MIN_PRECIO_CLP)
    categorias = [c["id"] for c in profile.CATEGORIES]
    ventana = _cfg("VENTANA_REFERENCIA_DIAS", capa_lenta.VENTANA_DIAS)

    entidades = []
    vistas = set()
    for target in profile.CATEGORIES:
        try:
            for e in _candidatas(
                target["id"], store_ids, min_precio, target.get("db_brand_id")
            ):
                # Celulares declara dos entradas sobre la categoria 6; si algun dia
                # se agrega una sin marca, esto evita duplicar la entidad.
                if e["entity_id"] in vistas:
                    continue
                vistas.add(e["entity_id"])
                entidades.append(e)
        except requests.RequestException as exc:
            print(f"  error enumerando '{target['name']}': {exc}")

    seleccion = _rotar(entidades, disparo)
    print(f"  {len(entidades)} candidatas, {len(seleccion)} con historia esta pasada")

    def con_historia(e):
        try:
            filas = capa_lenta.historial(e["entity_id"], dias=ventana)
        except requests.RequestException:
            return None
        resumen = capa_lenta.resumen(filas, e["precio"], window_days=ventana)
        if not resumen:
            return None
        return {**e, "resumen": resumen, "parseable": True}

    with ThreadPoolExecutor(max_workers=HILOS_HISTORIA) as pool:
        con_datos = [r for r in pool.map(con_historia, seleccion) if r]

    watchlist = vigilancia.construir_watchlist(con_datos)

    entradas = []
    for c in watchlist:
        r = c["resumen"]
        entradas.append(
            {
                "entity_id": c["entity_id"],
                "store_id": c["store_id"],
                "product_id": c["product_id"],
                "category_id": c["category_id"],
                "nombre": c["nombre"],
                "url": c["url"],
                "metodo": extractores.metodo_de(c["store_id"]),
                "nivel": c["nivel"],
                "puntaje": c["puntaje"],
                "precio": r["precio"],
                "baseline": int(r["baseline"]),
                "p10": int(r["p10"]),
                "p90": int(r["p90"]),
                "minimo": int(r["minimo"]),
                "percentil": r["percentil"],
                "volatilidad": r["volatilidad"],
            }
        )

    conteo, peticiones = vigilancia.resumen_reparto(watchlist)
    print(
        f"  watchlist: {len(entradas)} entidades {conteo} "
        f"| ~{peticiones} peticiones/dia"
    )
    return entradas, conteo, peticiones


# --------------------------------------------------------------------------
# Capa rapida
# --------------------------------------------------------------------------

def _evaluar(fila, precio):
    """Decide si el precio recien leido es un hallazgo. Devuelve dict o None.

    La referencia es la de la capa lenta (baseline sobre 90 dias con el tiempo sin
    stock ya descontado), no una calculada aqui: la capa rapida no tiene historia
    propia y no debe inventarsela.

    DOS PUERTAS, y la segunda es la que importa.

    Solo con el ratio contra la mediana, un producto que oscila entre dos precios
    de forma recurrente alerta cada vez que baja. Caso real de la primera corrida
    de hardware: AMD Ryzen 5 4500 a $91.900 contra mediana de $159.900, o sea -43%,
    que cruza cualquier umbral de ratio. Pero su historia dice otra cosa:

        61 muestras -> $159.900 (34), $91.900 (17), $96.500 (10)
        percentil del precio "de oferta": 0,257

    Ha estado asi de barato una cuarta parte del tiempo. Es un ciclo promocional,
    no un hallazgo. Alertarlo es exactamente el fallo que hundio a Ratonean2:
    confundir el ciclo estacional con un descuento real.

    La segunda puerta exige rareza, y cual sirve depende del perfil. Medido con
    backtest.py sobre 89 dias de pricing_history (ver los umbrales de cada
    profiles/*.py): en perfumeria basta `precio < p10`, pero en hardware no
    discrimina nada porque el ciclo baja tan seguido que su propio p10 queda
    dentro del ciclo, y hay que exigir `precio < minimo`.

    ALERT_MAX_RATIO_RAPIDA es aparte de ALERT_MAX_RATIO a proposito: run.py mira
    la capa normalizada, donde el emparejamiento de Solotodo mete ruido (el Chanel
    de 50/100/150ml) y necesita un umbral duro. Aca la entidad es un SKU de una
    tienda y ademas hay puerta de rareza, asi que se puede aflojar el ratio sin
    perder precision. Medido en perfumeria: 0,50 daba 0,1 alertas/dia y 0,70 da
    1,1 con 85% de utiles.
    """
    base = fila.get("baseline")
    if not base or not precio:
        return None

    max_ratio = _cfg("ALERT_MAX_RATIO_RAPIDA", _cfg("ALERT_MAX_RATIO", 0.50))
    ratio = precio / base
    if ratio > max_ratio:
        return None

    p10 = fila.get("p10")
    minimo = fila.get("minimo")
    bajo_p10 = bool(p10 and precio < p10)
    bajo_minimo = bool(minimo and precio < minimo)

    puerta = _cfg("PUERTA_RAREZA", "p10")
    if puerta == "p10":
        if p10 is None or not bajo_p10:
            return None
    elif puerta == "minimo":
        if minimo is None or not bajo_minimo:
            return None
    elif puerta != "ninguna":
        raise ValueError(f"PUERTA_RAREZA desconocida: {puerta!r}")

    return {
        "ratio": ratio,
        "caida": round((1 - ratio) * 100),
        "bajo_p10": bajo_p10,
        "bajo_minimo": bajo_minimo,
        "baseline": base,
        "p10": p10,
        "puerta": puerta,
        "ventana": _cfg("VENTANA_REFERENCIA_DIAS", capa_lenta.VENTANA_DIAS),
    }


def _corroborar(fila, hallazgo, watchlist_por_producto):
    """Contrasta contra las otras tiendas que venden el mismo producto.

    Es la senal que la capa normalizada de Solotodo no permite calcular, porque ahi
    el precio ya viene colapsado a un minimo entre tiendas:

        aislada  -> una cae y el resto sostiene   -> error o liquidacion puntual
        campana  -> caen todas                    -> promocion coordinada
    """
    hermanas = [
        f for f in watchlist_por_producto.get(fila.get("product_id"), [])
        if f["entity_id"] != fila["entity_id"]
    ]
    if not hermanas:
        return "sin_comparacion", 0, 0

    resumenes = {f["store_id"]: {"percentil": f.get("percentil")} for f in hermanas}
    resumenes[fila["store_id"]] = {"percentil": 0.0}
    return capa_lenta.corroborar(resumenes, fila["store_id"])


def _mensaje(fila, precio, hallazgo, clase, cayeron, comparadas):
    tienda = extractores.nombre_de(fila["store_id"])
    fmt = lambda n: f"${n:,.0f}".replace(",", ".")

    lineas = [
        f"[{PROFILE}] {fila.get('nombre') or 'producto'}",
        "",
        f"{fmt(precio)} en {tienda}",
        f"Cayo {hallazgo['caida']}% vs su mediana de {hallazgo['ventana']}d "
        f"({fmt(hallazgo['baseline'])})",
    ]
    if hallazgo["bajo_minimo"]:
        lineas.append(f"Es su precio mas bajo registrado en {hallazgo['ventana']}d")
    elif hallazgo["bajo_p10"]:
        lineas.append("Por debajo de su p10 historico")

    if clase == "aislada":
        lineas.append(f"Solo esta tienda bajo ({comparadas} comparadas) - posible error")
    elif clase == "campana":
        lineas.append(f"Bajaron {cayeron}/{comparadas} tiendas - es campana, no error")
    elif clase == "parcial":
        lineas.append(f"Bajaron {cayeron}/{comparadas} tiendas")

    lineas += ["", f"Comprar: {fila['url']}"]
    return "\n".join(lineas)


def correr_rapida(conn, disparo, cache, watchlist):
    """Un disparo de la capa rapida. Devuelve (stats, alertas, cache nuevo)."""
    if not watchlist:
        return {"consultadas": 0}, 0, cache

    por_producto = {}
    for f in watchlist:
        por_producto.setdefault(f.get("product_id"), []).append(f)

    lote = vigilancia.lote_del_disparo(watchlist, disparo)
    for item in lote:
        guardado = cache.get(item["entity_id"]) or {}
        item["cache"] = {
            "etag": guardado.get("etag"),
            "last_modified": guardado.get("last_modified"),
        }
        item["huella"] = guardado.get("huella")

    resultados = capa_rapida.revisar_lote(lote)
    stats = capa_rapida.estadisticas(resultados)

    por_id = {f["entity_id"]: f for f in watchlist}
    candidatos = []

    for r in resultados:
        guardado = cache.setdefault(r["entity_id"], {})
        guardado.update(
            {k: v for k, v in (r.get("cache") or {}).items() if v},
        )
        if r.get("huella"):
            guardado["huella"] = r["huella"]

        if r.get("error"):
            continue
        if not r.get("cambio") or not r.get("precio"):
            continue  # nada nuevo: no se evalua ni se escribe

        fila = por_id.get(r["entity_id"])
        if not fila:
            continue

        # El ultimo precio visto queda en la watchlist en memoria; se persiste
        # con el sidecar al final de la corrida.
        fila["precio"] = r["precio"]

        hallazgo = _evaluar(fila, r["precio"])
        if hallazgo:
            candidatos.append((fila, r["precio"], hallazgo))

    enviadas, motivos = _alertar(conn, candidatos, por_producto)
    stats["candidatos"] = len(candidatos)
    stats["suprimidas"] = motivos
    return stats, enviadas, cache


def _alertar(conn, candidatos, por_producto):
    """Envia hasta el cupo. Devuelve (enviadas, motivos de supresion).

    Los motivos se cuentan para poder DECIDIR con datos si algun dia hay que
    separar el presupuesto de run.py del de esta capa. Hoy no hace falta -- medido
    sobre 11 dias reales, el pico fue 3 alertas/dia contra un presupuesto de 20,
    o sea 15% -- pero sin instrumentar no habria como notar que eso cambio.
    """
    motivos = {"presupuesto": 0, "cooldown": 0, "envio": 0}
    if not candidatos:
        return 0, motivos

    candidatos.sort(key=lambda c: c[2]["ratio"])
    presupuesto = _cfg("MAX_ALERTS_PER_DAY", 20) - count_alerts_since(conn, 24)
    cupo = min(_cfg("MAX_ALERTS_PER_RUN", 5), max(presupuesto, 0))
    if cupo <= 0:
        motivos["presupuesto"] = len(candidatos)
        print(f"  presupuesto diario agotado: {len(candidatos)} candidatos sin enviar")
        return 0, motivos

    recientes = load_recent_alerts(
        conn,
        [f.get("product_id") for f, _, _ in candidatos if f.get("product_id")],
        within_hours=_cfg("ALERT_COOLDOWN_HOURS", 24),
    )
    redrop = _cfg("REALERT_ON_EXTRA_DROP", 0.10)
    enviadas = 0

    for fila, precio, hallazgo in candidatos:
        if enviadas >= cupo:
            motivos["presupuesto"] += 1
            continue

        ya = recientes.get(fila.get("product_id"))
        # Se reabre solo si siguio bajando de forma relevante; un cooldown fijo se
        # comeria justamente la mejor caida.
        if ya is not None and precio > ya * (1 - redrop):
            motivos["cooldown"] += 1
            continue

        clase, cayeron, comparadas = _corroborar(fila, hallazgo, por_producto)
        mensaje = _mensaje(fila, precio, hallazgo, clase, cayeron, comparadas)

        if DRY_RUN:
            print("  [DRY RUN]", mensaje.replace("\n", " | "))
            enviadas += 1
            continue

        if send_alert(mensaje):
            record_alert(
                conn, fila.get("product_id"), precio,
                f"capa rapida: -{hallazgo['caida']}% ({clase})",
                fila["store_id"], fila["url"],
            )
            conn.commit()
            enviadas += 1
            print("  ALERTA:", (fila.get("nombre") or "")[:60])
        else:
            motivos["envio"] += 1
            print("  (no se pudo enviar, se reintenta en el proximo disparo)")

    return enviadas, motivos


# --------------------------------------------------------------------------

def run_once():
    init_db()
    inicio = time.time()

    estado = estado_rapido.cargar(PROFILE)
    disparo = estado["disparo"] + 1
    cache = estado["cache"]
    watchlist = estado["watchlist"]
    watchlist_ts = estado["watchlist_ts"]

    lenta = {"corrio": False}
    stats = {"consultadas": 0}
    enviadas = 0

    toca_lenta = MODO == "lenta" or estado_rapido.vencida(
        watchlist, watchlist_ts, HORAS_WATCHLIST
    )
    if MODO == "rapida":
        toca_lenta = False

    if toca_lenta:
        print(f"Capa lenta ({PROFILE})...")
        try:
            nueva, conteo, peticiones = refrescar_watchlist(disparo)
            if nueva:
                watchlist, watchlist_ts = nueva, estado_rapido.sello()
            lenta = {"corrio": True, "entidades": len(nueva), "conteo": conteo,
                     "peticiones": peticiones}
        except Exception as exc:
            # Si la capa lenta falla se sigue con la watchlist anterior: vieja es
            # mejor que ninguna, y `vencida()` hara que se reintente igual.
            print(f"  error en capa lenta: {exc}")
            traceback.print_exc()

    with get_conn() as conn:
        print(f"Capa rapida (disparo {disparo})...")
        try:
            stats, enviadas, cache = correr_rapida(conn, disparo, cache, watchlist)
            print(
                f"  {stats['consultadas']} consultadas | {stats.get('sin_cambio',0)} sin cambio "
                f"| {stats.get('con_cambio',0)} con cambio | {stats.get('errores',0)} errores"
            )
            if stats.get("metodo_cambiado"):
                print(
                    f"  OJO: {stats['metodo_cambiado']} tiendas cambiaron de metodo "
                    f"-- revisar extractores.TIENDAS"
                )
        except Exception as exc:
            print(f"  error en capa rapida: {exc}")
            traceback.print_exc()

    cache = estado_rapido.podar(cache, [f["entity_id"] for f in watchlist])
    estado_rapido.guardar(PROFILE, disparo, cache, watchlist, watchlist_ts)

    duracion = time.time() - inicio
    print(f"Listo en {duracion:.1f}s | {enviadas} alertas")
    _resumen(lenta, stats, enviadas, disparo, duracion)
    return enviadas


def _resumen(lenta, stats, enviadas, disparo, duracion):
    """Panel de la corrida en GitHub Actions.

    Sin esto un disparo sin cambios de precio no deja ninguna huella (justamente
    porque no commitea) y se vuelve indistinguible de un workflow que no corrio.
    """
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return

    lineas = [f"## {PROFILE} - disparo {disparo}", ""]
    if lenta.get("corrio"):
        lineas += [
            f"**Capa lenta:** {lenta['entidades']} entidades en watchlist "
            f"{lenta['conteo']} (~{lenta['peticiones']} peticiones/dia)",
            "",
        ]
        # La capa lenta deberia correr 2 veces al dia, no en cada disparo. Si
        # aparece seguido es que el sidecar no esta sobreviviendo entre corridas
        # (actions/cache), y entonces se estan pidiendo 400 pricing_history por
        # perfil cada 10 minutos contra una API gratuita ajena.
        if disparo > 3:
            lineas += [
                "> La capa lenta corrio en un disparo tardio. Deberia hacerlo "
                "cada ~12h. Si se repite, revisar que el paso *Estado de la capa "
                "rapida* este restaurando el sidecar.",
                "",
            ]
    sup = stats.get("suprimidas") or {}
    lineas += [
        "| Consultadas | Sin cambio | Con cambio | Errores | Ahorro parseo |",
        "|--:|--:|--:|--:|--:|",
        f"| {stats.get('consultadas',0)} | {stats.get('sin_cambio',0)} "
        f"| {stats.get('con_cambio',0)} | {stats.get('errores',0)} "
        f"| {stats.get('ahorro_parseo',0)} |",
        "",
        "| Candidatos | Enviadas | Sin cupo | En cooldown | Fallo de envio |",
        "|--:|--:|--:|--:|--:|",
        f"| {stats.get('candidatos',0)} | {enviadas} | {sup.get('presupuesto',0)} "
        f"| {sup.get('cooldown',0)} | {sup.get('envio',0)} |",
        "",
        f"Terminado en {duracion:.1f}s.",
    ]
    # El presupuesto se comparte con run.py. Hoy no topa (pico medido: 3/dia de
    # 20), pero si empieza a topar hay que verlo aca antes que en Telegram.
    if sup.get("presupuesto"):
        lineas.append(
            f"\n> Se quedaron {sup['presupuesto']} candidatos sin cupo. Si se "
            f"repite, separar el presupuesto de run.py."
        )
    if stats.get("metodo_cambiado"):
        lineas.append(
            f"\n> {stats['metodo_cambiado']} tiendas cambiaron de metodo de "
            f"extraccion. Revisar `extractores.TIENDAS`."
        )
    lineas.append("")

    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lineas) + "\n")
    except OSError as exc:
        print(f"  (no se pudo escribir el resumen: {exc})")


if __name__ == "__main__":
    run_once()
