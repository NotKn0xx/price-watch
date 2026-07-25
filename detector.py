from datetime import datetime, timezone

# Cuanta historia real necesitamos antes de confiar en la mediana propia.
# Antes bastaban 3 filas; corriendo cada hora eso eran 3 horas, asi que la
# "mediana historica" era practicamente el precio actual y nunca disparaba.
MIN_SPAN_HOURS = 48

# Con el esquema comprimido, un producto que nunca cambio de precio tiene UN
# solo tramo -- y es justamente la mejor referencia posible. Lo que exigimos es
# cobertura temporal (MIN_SPAN_HOURS), no cantidad de tramos ni de muestras.

# Piso de peso de un tramo, para que un cambio de precio recien detectado no
# entre con peso ~0 a la mediana. Va atado al intervalo de escaneo mas corto
# (15 min en la ventana diurna).
MIN_SEGMENT_WEIGHT_SECONDS = 900

# Niveles de severidad segun cuanto cae el precio vs. su referencia
# (mediana historica propia, o precio normal reportado por la tienda).
# Se evalua de mas estricto a menos estricto y gana el primero que matchea.
DROP_TIERS = (
    (0.50, "ULTRA descuento"),  # cae a <=50% de su referencia
    (0.65, "Gran descuento"),  # cae a <=65%
    (0.80, "Descuento leve"),  # cae a <=80%
)

# Sin historia propia la unica referencia es el "precio normal" que reporta la
# misma tienda, y ese numero viene inflado por campanas comerciales. Solo
# confiamos en el nivel mas severo.
STORE_DISCOUNT_THRESHOLD = DROP_TIERS[0][0]

_TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f")


def _clp(n) -> str:
    return f"${n:,.0f}".replace(",", ".")


def _parse_ts(value):
    if isinstance(value, datetime):
        return value
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except (TypeError, ValueError):
            continue
    return None


def _drop_tier(ratio: float):
    for threshold, label in DROP_TIERS:
        if ratio <= threshold:
            return label
    return None


def weighted_median(pairs):
    """Mediana ponderada de [(valor, peso)]."""
    pairs = sorted(p for p in pairs if p[1] > 0)
    total = sum(w for _, w in pairs)
    if not pairs or total <= 0:
        return None
    half = total / 2
    acc = 0.0
    for value, weight in pairs:
        acc += weight
        if acc >= half:
            return value
    return pairs[-1][0]


def utcnow():
    """SQLite guarda CURRENT_TIMESTAMP en UTC; comparamos contra eso."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def baseline_from_segments(segments, now=None):
    """Mediana ponderada por TIEMPO y cobertura temporal de los tramos.

    Ponderar por tiempo (y no por cantidad de muestras) evita que una falla de
    precio que dura 2 horas arrastre su propia referencia, y hace que un precio
    que se sostuvo una semana pese lo que corresponde.

    La duracion de cada tramo se deduce del inicio del tramo siguiente; el
    ultimo tramo (el vigente) se extiende hasta ahora. Asi el peso es correcto
    sin que el scanner tenga que ir escribiendo `last_seen` en cada corrida.

    Devuelve (baseline, span_hours, minimo_historico).
    """
    now = now or utcnow()

    starts = []
    for seg in segments:
        start = _parse_ts(seg["first_seen"])
        if start is not None:
            starts.append((start, seg["price"]))
    if not starts:
        return None, 0.0, None

    starts.sort()
    pairs = []
    for i, (start, price) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else now
        weight = max((end - start).total_seconds(), MIN_SEGMENT_WEIGHT_SECONDS)
        pairs.append((price, weight))

    span_hours = max((now - starts[0][0]).total_seconds() / 3600, 0.0)
    return weighted_median(pairs), span_hours, min(p for _, p in starts)


def check_anomaly(product: dict, segments: list, min_price: int = 0):
    """Devuelve dict(reason, ratio, source) o None si no hay nada raro.

    Compara el precio actual del producto contra SU PROPIA referencia
    (mediana historica ponderada por tiempo, o el precio normal que reporta
    la tienda), nunca contra otros productos de la categoria.
    """
    price = product.get("price")
    if not price or price <= 0:
        return None

    # Piso absoluto: una caida del 60% en un producto de $3.000 es ruido, no
    # una falla de precio que valga la pena mirar.
    if min_price and price < min_price:
        return None

    baseline, span_hours, historic_min = baseline_from_segments(segments)

    if baseline and span_hours >= MIN_SPAN_HOURS:
        ratio = price / baseline
        tier = _drop_tier(ratio)
        if tier:
            drop_pct = round((1 - ratio) * 100)
            extra = ""
            if historic_min is not None and price < historic_min:
                extra = " - es su precio mas bajo registrado"
            return {
                "reason": (
                    f"{tier}: cayo {drop_pct}% vs su mediana de {span_hours / 24:.0f}d "
                    f"({_clp(baseline)} -> {_clp(price)}){extra}"
                ),
                "ratio": ratio,
                "source": "history",
            }
        # Con historia confiable, el descuento que reporta la tienda ya no
        # aporta nada: la referencia propia manda.
        return None

    old_price = product.get("old_price")
    if old_price and old_price > price:
        ratio = price / old_price
        if ratio <= STORE_DISCOUNT_THRESHOLD:
            discount = product.get("discount") or round((1 - ratio) * 100)
            return {
                "reason": (
                    f"ULTRA descuento reportado por la tienda: {discount}% "
                    f"({_clp(old_price)} -> {_clp(price)})"
                ),
                "ratio": ratio,
                "source": "store",
            }

    return None
