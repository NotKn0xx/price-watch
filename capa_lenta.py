"""Capa lenta: catalogo, historia y decision de que vigilar rapido.

Lee la capa CRUDA de Solotodo (`/entities/` + `/entities/{id}/pricing_history/`)
en vez de la normalizada (`/products/browse/`), y esa es toda la diferencia.

Por que importa, medido el 04-08-2026 sobre 52 productos multi-tienda de
perfumeria (categoria 780, tiendas 9/11/18):

    dispersion entre tiendas:  mediana 0% | p90 45% | max 58%
    productos sobre 50%:       3/52  (6% del catalogo)
    agrupaciones con volumen distinto: 2/52

El caso concreto: "Chanel Allure Homme Sport" agrupa 50ml, 100ml y 150ml en un
solo product_id, con precios reales de $123.200 a $192.500. Si el de 150ml se
agota, el minimo agregado cae al de 50ml y la deteccion ve una baja del 36% que
nunca ocurrio. Ese 6% de cola cae justo encima del ALERT_MAX_RATIO=0.50 de
perfumes, o sea que contamina exactamente el rango donde se alerta.

Una entidad es una tienda y un SKU. No hay nada que agrupar, asi que ese modo de
falla desaparece por construccion en vez de compensarse con un umbral grueso.

Lo que ademas se gana:
  - `is_available` por muestra: un quiebre de stock deja de parecer un cambio de precio.
  - Historia hacia atras: hasta 393 dias medidos en una entidad, contra las 48h de
    calentamiento que hoy exige MIN_SPAN_HOURS antes de confiar en la mediana.
  - `normal_price` por muestra, que permite comprobar si el "precio normal" alguna
    vez se sostuvo de verdad en vez de creerle a la tienda.

Costo medido: 0,37s por llamada, 12 llamadas seguidas sin rate limit ni 429.
"""

from datetime import datetime, timedelta, timezone

from detector import weighted_median
from solotodo import get_session, BASE_URL

# Peso minimo de un tramo. Igual que en detector.py: un cambio recien detectado no
# puede entrar con peso ~0 a la mediana.
PESO_MINIMO_SEGUNDOS = 900

# Ventana por defecto para baseline y percentil.
VENTANA_DIAS = 90

# Bajo esta cantidad de muestras disponibles no se calcula nada: con 2 lecturas
# cualquier estadistico es ruido con nombre elegante.
MIN_MUESTRAS = 6


def _ahora():
    return datetime.now(timezone.utc)


def _parse(ts):
    """Los timestamps de pricing_history vienen ISO-8601 con Z."""
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def entidades(category_id, store_ids, db_brand_id=None, page_size=300,
              max_pages=40, timeout=45):
    """Entidades activas de una categoria, consultando UNA TIENDA A LA VEZ.

    Pedir varias tiendas en la misma llamada parece lo natural y es un error: la
    API devuelve los resultados agrupados por tienda, asi que las primeras paginas
    se las lleva entera la tienda con mas catalogo. Medido: 6 paginas pidiendo 6
    tiendas devolvieron 588 entidades, las 588 de Falabella y 0 del resto.

    Eso no se nota como fallo -- devuelve datos validos -- pero deja la watchlist
    con una sola tienda, y sin varias tiendas por producto la corroboracion cruzada
    no tiene contra que comparar, que es justamente la senal que da valor a todo esto.
    """
    for store_id in store_ids:
        yield from _entidades_de_tienda(
            category_id, store_id, db_brand_id, page_size, max_pages, timeout
        )


def _entidades_de_tienda(category_id, store_id, db_brand_id=None, page_size=300,
                         max_pages=40, timeout=45):
    """Entidades de una categoria en una sola tienda.

    Devuelve dicts con entity_id, store_id, product_id, nombre, url y el ultimo
    precio conocido. El product_id se conserva solo para poder agrupar entre
    tiendas al corroborar; el seguimiento nunca se hace sobre el.
    """
    sesion = get_session()
    pagina = 1
    vistos = set()

    while pagina <= max_pages:
        params = [("categories", category_id), ("is_active", "true")]
        params += [("stores", store_id)]
        if db_brand_id:
            # Misma trampa que en /products/browse/: el parametro es `db_brands`.
            # Verificado contra /entities/ el 04-08-2026 -- categoria 6 en
            # Falabella da 25.846 entidades sin filtro y 4.065 con db_brands=890,
            # mientras que `brands=890` devuelve las 25.846 sin avisar de nada.
            params.append(("db_brands", db_brand_id))
        params += [("page", pagina), ("page_size", page_size)]

        resp = sesion.get(f"{BASE_URL}/entities/", params=params, timeout=timeout)
        resp.raise_for_status()
        datos = resp.json()

        resultados = datos.get("results", [])
        if not resultados:
            break

        for e in resultados:
            eid = e.get("id")
            if eid in vistos:
                continue
            vistos.add(eid)

            registro = e.get("active_registry") or {}
            precio = registro.get("offer_price")
            producto = e.get("product") or {}

            yield {
                "entity_id": eid,
                "store_id": e.get("store_id"),
                "product_id": producto.get("id"),
                "nombre": e.get("name") or producto.get("name"),
                "url": e.get("external_url"),
                "precio": int(float(precio)) if precio else None,
                "disponible": bool(registro.get("is_available")),
            }

        if not datos.get("next"):
            break
        pagina += 1


def historial(entity_id, dias=VENTANA_DIAS, timeout=90):
    """Serie de precios de UNA entidad. Cada fila es una tienda, un SKU, un instante."""
    desde = (_ahora() - timedelta(days=dias)).strftime("%Y-%m-%dT%H:%M:%SZ")
    resp = get_session().get(
        f"{BASE_URL}/entities/{entity_id}/pricing_history/",
        params={"timestamp_after": desde},
        timeout=timeout,
    )
    resp.raise_for_status()

    filas = []
    for f in resp.json():
        ts = _parse(f.get("timestamp"))
        precio = f.get("offer_price")
        if ts is None or precio is None:
            continue
        try:
            precio = int(float(precio))
        except (TypeError, ValueError):
            continue
        if precio <= 0:
            continue
        normal = f.get("normal_price")
        try:
            normal = int(float(normal)) if normal is not None else None
        except (TypeError, ValueError):
            normal = None
        filas.append(
            {
                "ts": ts,
                "precio": precio,
                "normal": normal,
                "disponible": bool(f.get("is_available")),
            }
        )

    filas.sort(key=lambda f: f["ts"])
    return filas


def _tramos_disponibles(filas, ahora=None, window_days=None):
    """Convierte las filas en [(precio, segundos)] contando SOLO tiempo disponible.

    Un precio publicado mientras el producto estaba agotado no es un precio al que
    alguien pudiera comprar, asi que no debe pesar en la referencia. Es la version
    explicita de lo que hoy se compensa a ciegas con el umbral: sin `is_available`
    no habia como distinguir "bajo de precio" de "volvio el stock barato".
    """
    ahora = ahora or _ahora()
    inicio = ahora - timedelta(days=window_days) if window_days else None
    tramos = []
    for i, fila in enumerate(filas):
        fin = filas[i + 1]["ts"] if i + 1 < len(filas) else ahora
        if not fila["disponible"]:
            continue
        # Recorte contra la ventana: un tramo que empezo hace 200 dias solo
        # aporta lo que cae dentro. Mismo criterio que detector.baseline_from_segments.
        desde = max(fila["ts"], inicio) if inicio else fila["ts"]
        hasta = min(fin, ahora)
        if hasta <= desde:
            continue
        segundos = max((hasta - desde).total_seconds(), PESO_MINIMO_SEGUNDOS)
        tramos.append((fila["precio"], segundos))
    return tramos


def _percentil_ponderado(tramos, precio):
    """Fraccion del TIEMPO en que la entidad estuvo a este precio o mas barata.

    Se pondera por tiempo y no por cantidad de muestras porque el muestreo de
    Solotodo es irregular (mediana 4,06h pero p90 13,2h): contar muestras le daria
    mas peso a los tramos que casualmente se midieron mas seguido.

    Un percentil 2 dice "estuvo mas barata solo el 2% del tiempo" y es una
    afirmacion comprobable. Un "cayo 50%" no dice nada si el producto oscila 50%
    todo el ano.
    """
    total = sum(s for _, s in tramos)
    if total <= 0:
        return None
    debajo = sum(s for p, s in tramos if p <= precio)
    return debajo / total


def resumen(filas, precio_actual=None, ahora=None, window_days=VENTANA_DIAS):
    """Estadisticos de una entidad a partir de su serie propia.

    Devuelve None si no hay muestras disponibles suficientes.
    """
    tramos = _tramos_disponibles(filas, ahora, window_days)
    if len(tramos) < MIN_MUESTRAS:
        return None

    if precio_actual is None:
        disponibles = [f for f in filas if f["disponible"]]
        if not disponibles:
            return None
        precio_actual = disponibles[-1]["precio"]

    base = weighted_median(tramos)
    if not base:
        return None

    precios = sorted(p for p, _ in tramos)
    total = sum(s for _, s in tramos)

    # p10 y p90 ponderados por tiempo, para medir dispersion sin que un pico de
    # dos horas defina el rango.
    def _cuantil(q):
        objetivo = total * q
        acumulado = 0.0
        for precio, segundos in sorted(tramos):
            acumulado += segundos
            if acumulado >= objetivo:
                return precio
        return precios[-1]

    p10, p90 = _cuantil(0.10), _cuantil(0.90)

    return {
        "baseline": base,
        "precio": precio_actual,
        "ratio": precio_actual / base,
        "percentil": _percentil_ponderado(tramos, precio_actual),
        "minimo": precios[0],
        "maximo": precios[-1],
        "p10": p10,
        "p90": p90,
        # Rango intercuantil relativo: 0 = precio plano, 0.5 = oscila la mitad de
        # su valor. Robusto a outliers, que es justo lo que sobra aca.
        "volatilidad": (p90 - p10) / base if base else 0.0,
        "muestras": len(tramos),
        "dias": (tramos and total / 86400) or 0.0,
        "niveles": len(set(precios)),
    }


def normal_sostenido(filas, tolerancia=0.02):
    """Comprueba si el `normal_price` que declara la tienda alguna vez se cobro.

    SERNAC detecto en CyberDay 2025 mas de 142.000 productos con descuentos
    inflados, y que el 91% de los vendedores habia subido precios ANTES del evento.
    O sea: el "precio normal" es, con frecuencia medida, una ficcion.

    Con la serie por tienda eso deja de ser sospecha. Si el normal_price declarado
    nunca aparecio como precio efectivamente cobrado, el descuento es inventado y
    se puede afirmar con evidencia en vez de heuristica.

    Devuelve (normal_declarado, se_sostuvo, dias_a_ese_precio).
    """
    disponibles = [f for f in filas if f["disponible"]]
    if not disponibles:
        return None, None, 0.0

    declarado = disponibles[-1]["normal"]
    if not declarado:
        return None, None, 0.0

    segundos = 0.0
    for i, fila in enumerate(filas):
        if not fila["disponible"] or abs(fila["precio"] - declarado) / declarado > tolerancia:
            continue
        fin = filas[i + 1]["ts"] if i + 1 < len(filas) else _ahora()
        segundos += max((fin - fila["ts"]).total_seconds(), PESO_MINIMO_SEGUNDOS)

    dias = segundos / 86400
    # Un dia acumulado como umbral: menos que eso es un precio de vitrina que se
    # publico para poder tacharlo, no un precio que la tienda haya sostenido.
    return declarado, dias >= 1.0, dias


def corroborar(resumenes_por_tienda, store_id):
    """Contrasta la caida de una tienda contra las demas que venden lo mismo.

    Esta es la senal que la capa normalizada no permite calcular, porque ahi el
    precio ya viene colapsado a un minimo entre tiendas y las dos situaciones se
    ven identicas:

        una cae y el resto sostiene  -> error o liquidacion puntual  (interesante)
        caen todas a la vez          -> campana coordinada           (no es hallazgo)

    Devuelve (clase, cuantas_cayeron, cuantas_comparadas).
    """
    otros = [(sid, r) for sid, r in resumenes_por_tienda.items() if sid != store_id and r]
    if not otros:
        return "sin_comparacion", 0, 0

    # "Cayo" = esta por debajo del percentil 25 de su propia historia. Se usa el
    # percentil y no un % fijo para no volver a comparar productos distintos entre si.
    cayeron = sum(1 for _, r in otros
                 if r.get("percentil") is not None and r["percentil"] <= 0.25)

    if cayeron == 0:
        return "aislada", 0, len(otros)
    if cayeron >= max(1, round(len(otros) * 0.6)):
        return "campana", cayeron, len(otros)
    return "parcial", cayeron, len(otros)
