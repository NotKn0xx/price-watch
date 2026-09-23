"""Cliente para la API publica de Solotodo (publicapi.solotodo.com).

Solotodo ya mantiene actualizado el catalogo y los precios de Falabella,
Paris, Ripley y otras tiendas chilenas en su propio backend, y expone esa
data via una API publica pensada para consumo externo. Consultamos esa API
en vez de scrapear cada tienda directamente.
"""

import requests
import threading
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://publicapi.solotodo.com"

# La API tope page_size en 100 para /products/browse/; pedir mas no falla,
# simplemente devuelve 100.
MAX_PAGE_SIZE = 100

# Una sola sesion para todo el proceso: reusa la conexion TCP/TLS. Sin esto
# cada request rehace el handshake (~0.4s vs ~0.15s medido contra la API).
_local = threading.local()


def get_session():
    if not getattr(_local, "session", None):
        s = requests.Session()
        retry = Retry(
            total=4,
            backoff_factor=1.5,  # 0s, 1.5s, 3s, 6s
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
        s.mount("https://", adapter)
        s.headers["User-Agent"] = "price-watch/1.0 (+github.com/NotKn0xx/price-watch)"
        _local.session = s
    return _local.session


def browse_category(
    category_id,
    store_ids,
    db_brand_id=None,
    page_size=MAX_PAGE_SIZE,
    max_pages=50,
    timeout=30,
    page_delay=0.0,
):
    """Itera productos de una categoria, restringidos a store_ids.

    El precio devuelto es el mas bajo en CLP entre las tiendas filtradas.
    Si se pasa db_brand_id, restringe ademas a esa marca (ej. Apple, Samsung).

    Pagina hasta agotar la categoria (`count`), con max_pages solo como tope
    de seguridad. Antes el tope real eran 3 paginas, lo que en perfumes
    significaba mirar 300 de 2550 productos.
    """
    page_size = min(page_size, MAX_PAGE_SIZE)
    session = get_session()
    page = 1
    seen = 0
    total = None
    emitted = set()

    while page <= max_pages:
        params = [("categories", category_id)]
        params += [("stores", s) for s in store_ids]
        if db_brand_id:
            # OJO: el parametro es `db_brands`, no `brands`. La API ignora
            # silenciosamente `brands` y devuelve la categoria completa, que
            # es como el perfil de celulares terminaba escaneando los mismos
            # 160 equipos una vez por "marca".
            params.append(("db_brands", db_brand_id))
        params += [("page", page), ("page_size", page_size)]

        resp = session.get(f"{BASE_URL}/products/browse/", params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()

        results = data.get("results", [])
        if not results:
            break
        if total is None:
            total = data.get("count", 0)

        for entry in results:
            for pe in entry.get("product_entries", []):
                product = pe["product"]
                product_id = product["id"]
                if product_id in emitted:
                    continue
                prices = _extract_clp_prices(pe.get("metadata", {}))
                if prices is None:
                    continue
                offer_price, normal_price = prices
                emitted.add(product_id)
                discount = None
                if normal_price and normal_price > offer_price:
                    discount = round((1 - offer_price / normal_price) * 100)
                yield {
                    "product_id": product_id,
                    "name": product["name"],
                    "category_id": category_id,
                    "price": offer_price,
                    "old_price": normal_price,
                    "discount": discount,
                }

        seen += len(results)
        if total and seen >= total:
            break
        page += 1
        if page_delay:
            import time

            time.sleep(page_delay)


def _extract_clp_prices(metadata):
    """Devuelve (offer_price, normal_price) en CLP, o None si no hay datos."""
    for entry in metadata.get("prices_per_currency", []):
        if entry.get("currency_id") == 1:  # CLP
            offer = entry.get("offer_price")
            normal = entry.get("normal_price")
            if offer is None:
                return None
            offer = int(float(offer))
            if offer <= 0:
                return None
            normal = int(float(normal)) if normal is not None else None
            # Algunas tiendas reportan normal_price < offer_price; en ese caso
            # no sirve como referencia de descuento.
            if normal is not None and normal < offer:
                normal = None
            return offer, normal
    return None


def get_entities(product_id, timeout=30):
    """Detalle por tienda (precio, disponibilidad, URL) para un producto."""
    resp = get_session().get(f"{BASE_URL}/products/{product_id}/entities/", timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def best_entity_for_alert(product_id, store_ids, timeout=30):
    """Encuentra la entidad (tienda) mas barata y disponible entre store_ids."""
    store_ids = set(store_ids)
    try:
        entities = get_entities(product_id, timeout=timeout)
    except requests.RequestException:
        return None

    candidates = []
    for e in entities:
        if e.get("store_id") not in store_ids:
            continue
        reg = e.get("active_registry")
        if not reg or not reg.get("is_available"):
            continue
        try:
            price = int(float(reg["offer_price"]))
        except (KeyError, TypeError, ValueError):
            continue
        if price <= 0:
            continue
        candidates.append((price, e))

    if not candidates:
        return None
    return min(candidates, key=lambda c: c[0])[1]
