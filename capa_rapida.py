"""Capa rapida: consulta la tienda directo, para lo que Solotodo no alcanza a ver.

Solotodo remuestrea cada ~4h (mediana 4,06h / p90 13,2h). El Worker de
`cloudflare-cron` ya dispara cada 10 minutos. O sea que hoy se consulta 24 veces
mas seguido que lo que la fuente cambia. Ese hueco es lo unico que esta capa
existe para cubrir.

DONDE CORRE. El Worker dispara, pero no scrapea: el plan gratuito de Workers da
10ms de CPU por invocacion, que no alcanza para parsear decenas de fichas HTML.
El Worker sigue haciendo repository_dispatch y el trabajo se hace en el runner,
igual que hoy.

DETECCION DE CAMBIO ANTES DE PARSEAR -- medido el 04-08-2026, y el resultado
obliga a corregir el plan original:

    tienda      ETag   Last-Modified   condicional   hash de cuerpo estable
    Falabella   no     si              304 OK        NO
    Ripley      no     no              200           NO
    Winpy       no     no              200           NO

Dos conclusiones:

  1. El hash del cuerpo completo NO sirve. Dos peticiones seguidas a la misma URL
     sin cambios devuelven cuerpos distintos: las fichas traen tokens de sesion y
     marcas de tiempo por peticion. Comparar cuerpos daria "cambio" siempre.
  2. Solo Falabella responde 304. Ahi la peticion condicional ahorra la descarga
     entera, que es lo caro. En el resto no hay nada que ahorrar en red.

Por eso el ahorro se aplica sobre el VALOR EXTRAIDO y no sobre el cuerpo: se
guarda el hash de (precio, disponible) y si no cambio, no se escribe ni se evalua
nada aguas abajo. En Falabella ademas se evita la descarga.
"""

import hashlib
from concurrent.futures import ThreadPoolExecutor

import requests

from extractores import extraer, metodo_de, nombre_de

# UA de navegador real. Las 9 tiendas grandes medidas devuelven 200 con esto y sin
# proxy: ninguna bloquea. No hace falta ninguna API de scraping para acceder.
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

CABECERAS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "es-CL,es;q=0.9",
}

TIMEOUT = 25

# Concurrencia. No la fija ningun limite de plataforma sino la cortesia: el salto
# de 6 peticiones/dia a miles es exactamente lo que despierta defensas que hoy no
# estan activas. Se sube por etapas y midiendo, no de una.
CONCURRENCIA = 8

SIN_CAMBIO = "sin_cambio"
NUEVO = "nuevo"
ERROR = "error"


def huella(precio, disponible):
    """Hash de lo que de verdad importa, no del HTML.

    Ver la medicion de arriba: el cuerpo cambia entre peticiones identicas, asi que
    la unica huella estable es la del dato ya extraido.
    """
    return hashlib.sha256(f"{precio}|{int(bool(disponible))}".encode()).hexdigest()[:16]


def consultar(url, cache=None, sesion=None, timeout=TIMEOUT):
    """Pide la ficha, usando peticion condicional si la tienda la soporta.

    `cache` es el dict guardado de la consulta anterior: last_modified, etag.
    Devuelve (estado, html, cabeceras). Con estado SIN_CAMBIO el html viene None
    porque la tienda confirmo que no hay nada nuevo.
    """
    cache = cache or {}
    cabeceras = dict(CABECERAS)
    if cache.get("etag"):
        cabeceras["If-None-Match"] = cache["etag"]
    if cache.get("last_modified"):
        cabeceras["If-Modified-Since"] = cache["last_modified"]

    pedir = (sesion or requests).get
    try:
        resp = pedir(url, headers=cabeceras, timeout=timeout)
    except requests.RequestException as exc:
        return ERROR, None, {"error": type(exc).__name__}

    nuevas = {
        "etag": resp.headers.get("ETag"),
        "last_modified": resp.headers.get("Last-Modified"),
        "status": resp.status_code,
    }

    if resp.status_code == 304:
        return SIN_CAMBIO, None, nuevas
    if resp.status_code != 200:
        nuevas["error"] = f"HTTP {resp.status_code}"
        return ERROR, None, nuevas

    return NUEVO, resp.text, nuevas


def revisar_una(item, sesion=None):
    """Consulta y extrae una entidad de la watchlist.

    `item` necesita entity_id, store_id, url, y opcionalmente precio (ultimo
    conocido), cache y huella previa.

    Devuelve un dict con el resultado. `cambio` en False significa que no hay nada
    que evaluar aguas abajo: ni escritura ni deteccion.
    """
    store_id = item.get("store_id")
    referencia = item.get("precio")

    estado, html, cabeceras = consultar(
        item["url"], cache=item.get("cache"), sesion=sesion
    )

    base = {
        "entity_id": item.get("entity_id"),
        "store_id": store_id,
        "tienda": nombre_de(store_id),
        "url": item.get("url"),
        "estado": estado,
        "cache": {k: cabeceras.get(k) for k in ("etag", "last_modified")},
        "cambio": False,
        "precio": None,
        "metodo": None,
    }

    if estado is SIN_CAMBIO:
        # La tienda confirmo que no hay novedad. Ni descarga ni parseo.
        base["precio"] = referencia
        base["huella"] = item.get("huella")
        return base

    if estado is ERROR:
        base["error"] = cabeceras.get("error")
        return base

    precio, metodo, cambio_metodo = extraer(
        html, metodo_esperado=metodo_de(store_id), referencia=referencia
    )

    if precio is None:
        # Habia HTML pero no se pudo leer un precio. Casi siempre significa que la
        # tienda cambio la maqueta. Se reporta explicito en vez de tratarlo como
        # "sin cambio", que lo dejaria en silencio.
        base["error"] = "sin_precio"
        base["cambio_metodo"] = cambio_metodo
        return base

    # `is_available` no viene en el HTML de forma uniforme entre tiendas. Que haya
    # precio legible se toma como disponible, y la capa lenta corrige con el
    # `is_available` real de Solotodo en su proxima pasada.
    nueva = huella(precio, True)

    base.update(
        precio=precio,
        metodo=metodo,
        cambio_metodo=cambio_metodo,
        huella=nueva,
        cambio=nueva != item.get("huella"),
    )
    return base


def revisar_lote(lote, concurrencia=CONCURRENCIA):
    """Revisa un lote de la watchlist en paralelo.

    Una sesion por hilo: `requests.Session` no es segura entre hilos, y compartir
    una sola aqui era la forma facil de mezclar cookies entre tiendas.
    """
    if not lote:
        return []

    locales = __import__("threading").local()

    def con_sesion(item):
        sesion = getattr(locales, "s", None)
        if sesion is None:
            sesion = requests.Session()
            sesion.headers.update(CABECERAS)
            locales.s = sesion
        return revisar_una(item, sesion=sesion)

    with ThreadPoolExecutor(max_workers=concurrencia) as pool:
        return list(pool.map(con_sesion, lote))


def estadisticas(resultados):
    """Resumen del lote, para el panel de la corrida.

    El desglose de errores POR TIPO y POR TIENDA no es adorno. Medido en
    produccion el 05-08-2026: hardware daba 24 errores de 43 consultas mientras
    en local daba 0. Con solo el conteo total no habia forma de saber si era
    timeout, bloqueo o maqueta cambiada, y son tres problemas distintos con tres
    soluciones distintas.
    """
    from collections import Counter

    tipos = Counter()
    tiendas = Counter()
    for r in resultados:
        e = r.get("error")
        if not e:
            continue
        tipos[e] += 1
        tiendas[r.get("tienda", "?")] += 1

    total = len(resultados)
    return {
        "por_error": dict(tipos.most_common(8)),
        "por_tienda_error": dict(tiendas.most_common(8)),
        "consultadas": total,
        "sin_cambio": sum(1 for r in resultados if r["estado"] is SIN_CAMBIO),
        "con_cambio": sum(1 for r in resultados if r.get("cambio")),
        "errores": sum(1 for r in resultados if r["estado"] is ERROR or r.get("error")),
        "metodo_cambiado": sum(1 for r in resultados if r.get("cambio_metodo")),
        "ahorro_parseo": sum(
            1 for r in resultados if r["estado"] is SIN_CAMBIO or not r.get("cambio")
        ),
    }
