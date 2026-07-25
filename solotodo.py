"""Cliente para la API publica de Solotodo (publicapi.solotodo.com).

Solotodo ya mantiene actualizado el catalogo y los precios de Falabella,
Paris, Ripley y otras tiendas chilenas en su propio backend, y expone esa
data via una API publica pensada para consumo externo. Consultamos esa API
en vez de scrapear cada tienda directamente.
"""

import requests

BASE_URL = "https://publicapi.solotodo.com"


def browse_category(category_id, store_ids, brand_id=None, page_size=100, max_pages=5, timeout=20):
    """Itera productos de una categoria, restringidos a store_ids.

    El precio devuelto es el mas bajo en CLP entre las tiendas filtradas.
    Si se pasa brand_id, restringe ademas a esa marca (ej. Apple, Samsung).
    """
    page = 1
    seen = 0
    while page <= max_pages:
        params = [("categories", category_id)]
        params += [("stores", s) for s in store_ids]
        if brand_id:
            params.append(("brands", brand_id))
        params += [("page", page), ("page_size", page_size)]
        resp = requests.get(f"{BASE_URL}/products/browse/", params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if not results:
            break
        for entry in results:
            for pe in entry.get("product_entries", []):
                product = pe["product"]
                prices = _extract_clp_prices(pe.get("metadata", {}))
                if prices is None:
                    continue
                offer_price, normal_price = prices
                discount = None
                if normal_price and normal_price > 0:
                    discount = round((1 - offer_price / normal_price) * 100)
                yield {
                    "product_id": product["id"],
                    "name": product["name"],
                    "category_id": category_id,
                    "price": offer_price,
                    "old_price": normal_price,
                    "discount": discount,
                }
        seen += len(results)
        if seen >= data.get("count", 0):
            break
        page += 1


def _extract_clp_prices(metadata):
    """Devuelve (offer_price, normal_price) en CLP, o None si no hay datos."""
    for entry in metadata.get("prices_per_currency", []):
        if entry.get("currency_id") == 1:  # CLP
            offer = entry.get("offer_price")
            normal = entry.get("normal_price")
            if offer is None:
                return None
            return (
                int(float(offer)),
                int(float(normal)) if normal is not None else None,
            )
    return None


def get_entities(product_id, timeout=20):
    """Detalle por tienda (precio, disponibilidad, URL) para un producto."""
    resp = requests.get(f"{BASE_URL}/products/{product_id}/entities/", timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def best_entity_for_alert(product_id, store_ids, timeout=20):
    """Encuentra la entidad (tienda) mas barata entre store_ids para un producto."""
    entities = get_entities(product_id, timeout=timeout)
    candidates = [
        e for e in entities
        if e.get("store_id") in store_ids and e.get("active_registry")
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda e: float(e["active_registry"]["offer_price"]))
