"""Lectura conservadora: oferta del producto, nunca cercania al precio anterior.

No ejecuta JavaScript. Una lectura ambigua queda sin precio; moneda y stock
desconocidos quedan explicitos para que el consumidor no invente confirmacion.
"""

import json
import math
import re
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit


def precio_clp(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if 0 < value < 10**15 and math.isfinite(value) and value == int(value) else None
    value = str(value).strip()
    if len(value) > 64:
        return None
    value = re.sub(r"^(?:CLP\s*\$?|\$)\s*", "", value, flags=re.I)
    if not re.fullmatch(r"\d+(?:[.,]\d+)*", value):
        return None
    # Centavos declarados deben ser cero: no truncar un valor incompatible con CLP.
    tail = re.search(r"[.,](\d{1,2})$", value)
    if tail:
        if int(tail[1]):
            return None
        value = value[:tail.start()]
    if not re.fullmatch(r"\d+|\d{1,3}(?:\.\d{3})+|\d{1,3}(?:,\d{3})+", value):
        return None
    number = int(value.replace(".", "").replace(",", ""))
    return number if number > 0 else None


class Documento(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.blocks, self.meta = [], {}
        self.script = None
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("type", "").lower().strip() == "application/ld+json":
            self.script = []
        if tag == "meta":
            key = attrs.get("property") or attrs.get("itemprop") or attrs.get("name")
            if key and "content" in attrs:
                self.meta.setdefault(key.lower(), []).append(attrs["content"])

    def handle_data(self, text):
        if self.script is not None:
            self.script.append(text)

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            self.blocks.append("".join(self.script))
            self.script = None


def _types(node):
    raw = node.get("@type", [])
    return {str(t).rsplit("/", 1)[-1] for t in (raw if isinstance(raw, list) else [raw])}


def _nodes(node):
    # No recorrer isRelatedTo, ItemList, reviews ni shippingDetails.
    if isinstance(node, list):
        for item in node:
            yield from _nodes(item)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "mainEntity"):
            yield from _nodes(node.get(key))


def _url(value, base):
    try:
        parsed = urlsplit(urljoin(base or "", str(value)))
        return parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), parsed.query
    except ValueError:
        return None


def _availability(value):
    value = str(value or "").rsplit("/", 1)[-1].lower().replace("_", "").replace(" ", "")
    if value in ("instock", "limitedavailability"):
        return True
    if value in ("outofstock", "soldout", "discontinued", "preorder", "presale", "backorder"):
        return False
    return None


def _unique(values):
    values = set(values)
    return next(iter(values)) if len(values) == 1 else None


def leer(html, url=None):
    doc = Documento(html)
    nodes, malformed = [], False
    for block in doc.blocks:
        try:
            nodes.extend(_nodes(json.loads(block)))
        except (ValueError, TypeError, RecursionError):
            malformed = True
    products = [n for n in nodes if "Product" in _types(n)]
    index = {n["@id"]: n for n in nodes if isinstance(n.get("@id"), str)}
    # mainEntity puede ser una referencia al mismo Product del @graph (Ripley).
    # Un stub sin offers no es otra variante; dos ofertas distintas si lo son.
    complete = [p for p in products if "offers" in p]
    products = list({json.dumps(p, sort_keys=True): p for p in (complete or products)}.values())
    error = None
    offers = []
    selected = None
    if products:
        matches = [p for p in products if url and any(
            p.get(k) and _url(p[k], url) == _url(url, url) for k in ("url", "@id")
        )]
        if len(matches) == 1:
            selected = matches[0]
        elif (len(products) == 1 and not products[0].get("url")
              and not str(products[0].get("@id", "")).startswith(("http://", "https://"))):
            selected = products[0]
        elif len(products) == 1 and not url:
            selected = products[0]
        else:
            error = "producto_ambiguo"
        if selected is not None:
            raw = selected.get("offers", [])
            offers = raw if isinstance(raw, list) else [raw]
    else:
        # Compatibilidad con fichas que publican una sola oferta sin Product.
        offers = [n for n in nodes if "Offer" in _types(n) or (
            not _types(n) and "price" in n)]
    readings = []
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        ref = offer.get("@id")
        offer = index.get(ref, offer) if isinstance(ref, str) else offer
        if "AggregateOffer" in _types(offer):
            error = "oferta_agregada"
            continue
        if url and offer.get("url") and _url(offer["url"], url) != _url(url, url):
            continue
        if offer.get("priceValidUntil"):
            try:
                if date.fromisoformat(str(offer["priceValidUntil"])[:10]) < datetime.now(timezone.utc).date():
                    error = "oferta_vencida"
                    continue
            except ValueError:
                error = "vigencia_invalida"
                continue
        currency = offer.get("priceCurrency") or (selected or {}).get("priceCurrency")
        currency = str(currency).upper() if currency else None
        available = _availability(offer.get("availability") or (selected or {}).get("availability"))
        price = precio_clp(offer.get("price"))
        if currency not in (None, "CLP"):
            error = "moneda_no_clp"
            continue
        if price:
            readings.append((price, currency, available))
    if readings:
        # Incluso si un precio anterior coincide, dos ofertas distintas no
        # identifican la variante o el medio de pago comprado por el usuario.
        if len(set(readings)) == 1 and not error:
            price, currency, available = readings[0]
            return dict(precio=price, moneda=currency, disponible=available, metodo="ld+json", error=None)
        error = error or "oferta_ambigua"
    elif products or offers:
        error = error or "sin_oferta_valida"

    # Una declaracion estructurada contradictoria no se rescata con otro numero.
    if not error and not malformed:
        prices = doc.meta.get("product:price:amount", []) or doc.meta.get("price", [])
        currency = _unique(doc.meta.get("product:price:currency", []) or doc.meta.get("pricecurrency", []))
        currency = currency.upper() if currency else None
        price = _unique(precio_clp(p) for p in prices)
        stock = doc.meta.get("product:availability", []) or doc.meta.get("availability", [])
        if price and currency in (None, "CLP"):
            return dict(precio=price, moneda=currency, disponible=_unique(_availability(s) for s in stock),
                        metodo="meta", error=None)
        if prices:
            error = "moneda_no_clp" if currency not in (None, "CLP") else "oferta_ambigua"
    return dict(precio=None, moneda=None, disponible=None, metodo=None,
                error=error or ("json_invalido" if malformed else "sin_precio"))
