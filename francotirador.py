"""Capa francotirador: historial POR TIENDA de una watchlist acotada.

Por que existe, en una linea: `/products/browse/` no devuelve tiendas
(verificado el 27-07-2026 contra la API: cero menciones a store/entity/seller en
la respuesta), asi que `price_history` solo puede guardar el precio mas barato
agregado. Con eso se alerta, pero no se puede responder la pregunta que importa
para publicar: **subio en una tienda o en todas?**

Esa distincion no es un detalle. Si un precio sube en una sola tienda es una
decision de esa tienda; si sube en todas es costo, proveedor o dolar. Afirmar lo
primero cuando en realidad paso lo segundo es acusar en falso, que es el riesgo
mas serio del proyecto.

El detalle por tienda cuesta una peticion por producto. Para los 4.281 del
catalogo serian ~11.000 diarias extra contra la API gratuita de Solotodo -- el
README ya advierte que ese volumen invita a que bloqueen. Por eso: watchlist de
100-300 productos, que alcanza de sobra para 3-4 videos por semana.
"""

import time

from db import flush_store_prices, load_store_segments
from solotodo import get_entities

# Cortesia entre peticiones. La API es gratuita y es la unica fuente que queda
# (la de Mercado Libre esta cerrada: 403 con token de usuario valido). Ser
# educado es mas barato que perder el acceso.
DEMORA = 0.35


def _a_entero(valor):
    """Los precios vienen como '81990.00'. None si no hay dato utilizable."""
    if valor is None:
        return None
    try:
        return int(round(float(valor)))
    except (TypeError, ValueError):
        return None


def observaciones_de(entidades, store_ids=None):
    """Convierte la respuesta de /entities/ en filas para store_prices.

    Descarta lo que no sirve para publicar:
      - sin registro activo o no disponible: un precio de algo agotado no es
        un precio, y publicarlo manda gente a una pagina sin stock
      - precio no positivo o ilegible
      - tiendas fuera del perfil, si se pasa store_ids

    Funcion pura: no toca red ni base. Es la que conviene tener bien testeada,
    porque de aca sale lo que despues se afirma en publico.
    """
    filas = []
    for e in entidades or []:
        store_id = e.get("store_id")
        if store_id is None:
            continue
        if store_ids is not None and store_id not in store_ids:
            continue

        registro = e.get("active_registry") or {}
        if not registro.get("is_available"):
            continue

        precio = _a_entero(registro.get("offer_price"))
        if not precio or precio <= 0:
            continue

        filas.append((
            e.get("product_id") or (e.get("product") or {}).get("id"),
            store_id,
            precio,
            _a_entero(registro.get("normal_price")),
            e.get("external_url"),
        ))
    return filas


def vigilar(conn, product_ids, store_ids=None, demora=DEMORA, al_fallar=None):
    """Recorre la watchlist y persiste el precio de cada tienda.

    Falla por producto, no por corrida: si un producto revienta se registra y se
    sigue. Un 500 puntual de Solotodo no debe costar el ciclo entero ni dejar
    huecos en el resto de la watchlist.

    Devuelve (sin_cambio, cambiados, fallidos).
    """
    ids = list(product_ids)
    if not ids:
        return 0, 0, 0

    tramos = load_store_segments(conn, ids)
    observaciones, fallidos = [], 0

    for i, product_id in enumerate(ids):
        try:
            entidades = get_entities(product_id)
        except Exception as exc:  # red, 5xx, json invalido
            fallidos += 1
            if al_fallar:
                al_fallar(product_id, exc)
            continue

        for fila in observaciones_de(entidades, store_ids):
            # El product_id de la respuesta puede venir vacio; el que pedimos
            # siempre es correcto.
            observaciones.append((product_id,) + fila[1:])

        if demora and i < len(ids) - 1:
            time.sleep(demora)

    sin_cambio, cambiados = flush_store_prices(conn, tramos, observaciones)
    return sin_cambio, cambiados, fallidos
