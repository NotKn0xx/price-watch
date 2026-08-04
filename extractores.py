"""Extraccion del precio desde el HTML de la tienda, para la capa rapida.

La capa lenta (Solotodo) resuelve catalogo e historia, pero remuestrea cada ~4h
(mediana 4,06h / p90 13,2h, medido sobre 393 dias de una entidad). Una falla de
precio que dura menos que eso es invisible ahi. Esta capa mira directo a la
tienda, y por eso necesita sacar el precio del HTML servido.

MEDICION (04-08-2026, 56 tiendas con una URL viva cada una, tomada de Solotodo):

    30/56 (54%) entregan el precio sin ejecutar JavaScript
    26/56 renderizan en cliente -> requieren navegador, quedan fuera por ahora

El criterio de "entrega el precio" no fue que apareciera *algun* numero, sino que
coincidiera con el precio que Solotodo reportaba para esa misma entidad con menos
de 2% de diferencia. Un extractor que devuelve cualquier cosa es peor que ninguno:
alimenta la deteccion con basura que parece dato.

Hallazgo util: Falabella y Ripley -- las dos mas grandes -- SI sirven ld+json en
la ficha de producto. En la pagina de CATEGORIA no hay ningun precio en el HTML
(medido: 0 precios visibles en las 6 tiendas grandes), y por eso una prueba
anterior sobre listados concluyo lo contrario. La ficha y el listado se comportan
distinto; aca solo se usan fichas.
"""

import json
import re

LD_JSON = "ld+json"
META = "meta"
TEXTO = "texto"

# Orden de confianza. ld+json es dato estructurado declarado por la tienda;
# `texto` es leer el precio de la maqueta y se rompe con cualquier rediseno,
# asi que va ultimo y solo como respaldo.
METODOS = (LD_JSON, META, TEXTO)

# Registro validado el 04-08-2026: store_id de Solotodo -> metodo que reprodujo
# el precio de la API con <2% de error. El id es el mismo que usan los perfiles,
# asi que se cruza directo con profiles/*.py.
#
# Esto NO es una lista de tiendas soportadas para siempre: es la foto de un dia.
# `extraer()` detecta solos los cambios de metodo y `verificar_extractor()` es lo
# que decide si una tienda entra a la watchlist. Si una tienda deja de aparecer
# aca, no se rompe nada: simplemente se queda en la capa lenta.
TIENDAS = {
    9: (LD_JSON, "Falabella"),
    18: (LD_JSON, "Ripley"),
    8147: (LD_JSON, "Falabella Marketplace"),
    56: (LD_JSON, "Infor-Ingen"),
    6398: (LD_JSON, "Eylstore"),
    2438: (LD_JSON, "TecTec"),
    31: (LD_JSON, "Cintegral"),
    3165: (LD_JSON, "Tecnomas.cl"),
    887: (LD_JSON, "MegaDrive"),
    2801: (LD_JSON, "ETChile"),
    2636: (LD_JSON, "Trulu Store"),
    3263: (LD_JSON, "Notebooksya!"),
    2339: (LD_JSON, "Natcom"),
    953: (LD_JSON, "Play Factory"),
    6: (LD_JSON, "TYT Gamer Chile"),
    23: (LD_JSON, "MacOnline"),
    6101: (LD_JSON, "BackOnline"),
    2471: (LD_JSON, "Reuse"),
    7652: (LD_JSON, "Digitek"),
    5903: (LD_JSON, "CTMAN"),
    398: (LD_JSON, "CCLink"),
    955: (LD_JSON, "Tecno Master"),
    264: (LD_JSON, "Todoclick"),
    1514: (LD_JSON, "Tecnocam"),
    6662: (LD_JSON, "Gestion y Equipos"),
    5639: (LD_JSON, "CSByte"),
    128: (META, "AllTec"),
    257: (META, "Nice One"),
    45: (META, "Winpy"),
    86: (META, "SP Digital"),
}

# Tiendas medidas que renderizan en cliente. Se anotan para no volver a probarlas
# en cada iteracion y para tener claro que su ausencia esta medida, no olvidada.
RENDERIZAN_EN_CLIENTE = {
    11: "Paris",
    12: "PC Factory",
    14: "Wei",
    280: "UG Store",
    392: "MegaBytes",
    5144: "MyShop",
    397: "Centrale",
    3890: "Nuevatec",
}

# Tolerancia al validar un extractor contra el precio de Solotodo. No es 0 porque
# las dos fuentes se leen en instantes distintos y la tienda puede haber movido el
# precio en el intermedio.
TOLERANCIA_VALIDACION = 0.02

_RE_LD = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)
_RE_LD_PRECIO = re.compile(r'"(?:price|lowPrice)"\s*:\s*"?([\d.,]+)', re.I)
_RE_META = re.compile(
    r'(?:itemprop=["\']price["\'][^>]*content=|'
    r'property=["\']product:price:amount["\'][^>]*content=)["\']([\d.,]+)["\']',
    re.I,
)
_RE_TEXTO = re.compile(r"\$\s?(\d{1,3}(?:[.,]\d{3})+)")

# Claves donde Schema.org deja el precio, en orden de especificidad.
_CLAVES_PRECIO = ("price", "lowPrice")


def _a_entero(bruto):
    """Normaliza un precio de texto a CLP entero, o None.

    En CLP no hay decimales, pero las tiendas mezclan formatos: "1.629.900",
    "1629900.00", "1,629,900". La regla que funciona en los 30 casos medidos: si
    el ultimo separador deja exactamente 2 digitos a la derecha son centavos y se
    descartan; en cualquier otro caso el separador es de miles.
    """
    if bruto is None:
        return None
    texto = str(bruto).strip()
    if not texto:
        return None

    m = re.search(r"[.,](\d+)$", texto)
    if m and len(m.group(1)) == 2:
        texto = texto[: m.start()]

    digitos = re.sub(r"[^\d]", "", texto)
    if not digitos:
        return None
    try:
        valor = int(digitos)
    except ValueError:
        return None
    return valor if valor > 0 else None


def _caminar_ld(nodo):
    """Recorre un ld+json ya parseado y va entregando los precios que encuentre.

    El precio puede venir en el nodo raiz, en `offers`, en una lista de `offers`,
    o dentro de un `@graph`. Recorrer en vez de asumir una forma evita tener un
    extractor por tienda.
    """
    if isinstance(nodo, dict):
        for clave in _CLAVES_PRECIO:
            if clave in nodo:
                valor = _a_entero(nodo[clave])
                if valor:
                    yield valor
        for hijo in nodo.values():
            if isinstance(hijo, (dict, list)):
                yield from _caminar_ld(hijo)
    elif isinstance(nodo, list):
        for hijo in nodo:
            yield from _caminar_ld(hijo)


def precios_ld_json(html):
    """Todos los precios declarados en bloques application/ld+json."""
    encontrados = []
    for bloque in _RE_LD.findall(html):
        crudo = bloque.strip()
        try:
            encontrados.extend(_caminar_ld(json.loads(crudo)))
        except (ValueError, TypeError):
            # ld+json malformado es comun (comas colgando, HTML sin escapar).
            # Antes de descartar el bloque, se intenta la lectura plana.
            encontrados.extend(
                v for v in (_a_entero(p) for p in _RE_LD_PRECIO.findall(crudo)) if v
            )
    return encontrados


def precios_meta(html):
    """Precios en <meta itemprop="price"> / product:price:amount."""
    return [v for v in (_a_entero(p) for p in _RE_META.findall(html)) if v]


def precios_texto(html):
    """Precios escritos como $ 123.456 en la maqueta. Ultimo recurso."""
    return [v for v in (_a_entero(p) for p in _RE_TEXTO.findall(html)) if v]

_LECTORES = {
    LD_JSON: precios_ld_json,
    META: precios_meta,
    TEXTO: precios_texto,
}


def extraer(html, metodo_esperado=None, referencia=None):
    """Saca el precio del HTML. Devuelve (precio, metodo, cambio_de_metodo).

    `referencia` es el ultimo precio conocido de esa entidad. Cuando se pasa, se
    elige el candidato mas cercano a la referencia en vez del primero que aparezca;
    sin eso, una ficha que ademas muestra accesorios o "productos relacionados"
    puede entregar el precio de otra cosa. Es el mismo error de emparejar por
    cercania que ya nos costo caro: aca la referencia acota, no adivina.

    `cambio_de_metodo` en True significa que el metodo registrado dejo de servir y
    respondio otro. No es un error -- el precio sirve igual -- pero es la senal de
    que la tienda cambio y que el registro quedo viejo.
    """
    orden = []
    if metodo_esperado in _LECTORES:
        orden.append(metodo_esperado)
    orden += [m for m in METODOS if m != metodo_esperado]

    for metodo in orden:
        candidatos = _LECTORES[metodo](html)
        if not candidatos:
            continue
        if referencia:
            precio = min(candidatos, key=lambda p: abs(p - referencia))
        else:
            precio = candidatos[0]
        return precio, metodo, (metodo != metodo_esperado and metodo_esperado is not None)

    return None, None, False


COINCIDE = "coincide"
POSIBLE_CAMBIO = "posible_cambio"
EXTRACTOR_ROTO = "extractor_roto"
SIN_LECTURA = "sin_lectura"


def verificar_extractor(html, precio_conocido, metodo_esperado=None, rango=None):
    """Clasifica la lectura de una entidad antes de admitirla a la watchlist.

    Una entidad solo se vigila rapido si podemos confiar en lo que leemos de ella.
    Sin esta puerta, una tienda que cambia su maqueta deja de fallar de forma
    visible y empieza a emitir precios plausibles pero equivocados, que es el modo
    de falla caro.

    OJO con el atajo obvio: comparar contra el precio de Solotodo y rechazar si no
    calza confunde dos cosas distintas, y una de ellas es justo lo que buscamos.
    Caso real medido el 04-08-2026: una entidad de Ripley daba $53.990 en la API y
    $59.990 en el HTML (11,1%). Solotodo remuestrea cada ~4h, asi que esa
    diferencia puede ser un extractor roto O un precio que de verdad cambio desde
    la ultima pasada. Descartarla de plano seria botar el hallazgo.

    Por eso se decide con el rango historico propio de la entidad, no solo con el
    ultimo precio. `rango` es (p10, p90) de capa_lenta.resumen:

        coincide        -> calza con la API, dentro de tolerancia
        posible_cambio  -> no calza, pero cae dentro del rango historico plausible
        extractor_roto  -> no calza y ademas esta fuera de rango
        sin_lectura     -> no se pudo leer ningun precio

    Solo `extractor_roto` y `sin_lectura` bloquean la admision. `posible_cambio`
    entra a la watchlist y se resuelve solo en la siguiente pasada de la capa lenta.

    Devuelve (clase, precio_leido, metodo, error_relativo).
    """
    precio, metodo, _ = extraer(html, metodo_esperado, referencia=precio_conocido)
    if precio is None:
        return SIN_LECTURA, None, metodo, None
    if not precio_conocido:
        return SIN_LECTURA, precio, metodo, None

    error = abs(precio - precio_conocido) / precio_conocido
    if error <= TOLERANCIA_VALIDACION:
        return COINCIDE, precio, metodo, error

    if rango:
        p10, p90 = rango
        # Se ensancha el rango historico porque una caida nueva por definicion cae
        # bajo el minimo visto. Lo que se descarta es lo absurdo -- un precio de
        # accesorio o de otro producto de la ficha -- no lo simplemente bajo.
        if p10 and p90 and (p10 * 0.4) <= precio <= (p90 * 1.6):
            return POSIBLE_CAMBIO, precio, metodo, error

    return EXTRACTOR_ROTO, precio, metodo, error


def admisible(clase):
    """Si una lectura permite que la entidad entre a la watchlist."""
    return clase in (COINCIDE, POSIBLE_CAMBIO)


def metodo_de(store_id):
    """Metodo registrado para una tienda, o None si no esta medida."""
    registro = TIENDAS.get(store_id)
    return registro[0] if registro else None


def nombre_de(store_id):
    registro = TIENDAS.get(store_id)
    if registro:
        return registro[1]
    return RENDERIZAN_EN_CLIENTE.get(store_id, f"tienda {store_id}")


def es_parseable(store_id):
    return store_id in TIENDAS
