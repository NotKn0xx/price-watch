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

from extractores import extraer_detalle, metodo_de, nombre_de
from http_tienda import descargar

# UA usado en la medicion original. Hay bloqueos 403 en produccion (septiembre
# 2026); se registran y se aplica espera, sin intentar evadirlos.
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

    try:
        return descargar(url, cabeceras, timeout, sesion)
    except requests.RequestException as exc:
        return ERROR, None, {"error": type(exc).__name__}
    except ValueError as exc:
        # Solo codigos internos, nunca URLs ni credenciales de las excepciones.
        return ERROR, None, {"error": str(exc) if str(exc) in {
            "url_no_permitida", "redireccion_otro_host", "destino_no_publico",
            "destino_invalido", "redireccion_invalida", "redireccion_insegura",
            "contenido_no_html", "respuesta_demasiado_grande", "tiempo_total_excedido",
        } else "respuesta_invalida"}



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
        "reintentar_en": cabeceras.get("retry_after", 0),
    }

    if estado == SIN_CAMBIO:
        # La tienda confirmo que no hay novedad. Ni descarga ni parseo.
        base["precio"] = referencia
        base["huella"] = item.get("huella")
        base["disponible"] = item.get("disponible")
        base["moneda"] = item.get("moneda")
        return base

    if estado == ERROR:
        base["error"] = cabeceras.get("error")
        return base

    lectura = extraer_detalle(html, url=cabeceras.get("url_final") or item["url"])
    precio, metodo = lectura["precio"], lectura["metodo"]
    cambio_metodo = bool(metodo and metodo_de(store_id) and metodo != metodo_de(store_id))
    base.update(disponible=lectura["disponible"], moneda=lectura["moneda"])
    if precio is None:
        base["error"] = lectura["error"] or "sin_precio"
        base["estado"] = ERROR
        base["cambio_metodo"] = cambio_metodo
        return base
    nueva = huella(precio, lectura["disponible"])

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

    import threading
    from urllib.parse import urlsplit
    locales = threading.local()
    sesiones = []
    # Una consulta simultanea por dominio, incluso si tiene varios store_id.
    locks = {urlsplit(i["url"]).netloc: threading.Lock() for i in lote}

    def con_sesion(item):
        sesion = getattr(locales, "s", None)
        if sesion is None:
            sesion = requests.Session()
            sesion.headers.update(CABECERAS)
            locales.s = sesion
            sesiones.append(sesion)
        with locks[urlsplit(item["url"]).netloc]:
            return revisar_una(item, sesion=sesion)

    try:
        with ThreadPoolExecutor(max_workers=concurrencia) as pool:
            return list(pool.map(con_sesion, lote))
    finally:
        for sesion in sesiones:
            sesion.close()


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
        "sin_cambio": sum(1 for r in resultados if r["estado"] == SIN_CAMBIO),
        "con_cambio": sum(1 for r in resultados if r.get("cambio")),
        "errores": sum(1 for r in resultados if r["estado"] == ERROR or r.get("error")),
        "metodo_cambiado": sum(1 for r in resultados if r.get("cambio_metodo")),
        "ahorro_parseo": sum(
            1 for r in resultados if r["estado"] == SIN_CAMBIO or not r.get("cambio")
        ),
    }
