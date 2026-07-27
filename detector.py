"""Decide si el precio actual de un producto es anomalo respecto de SU PROPIA
historia. Nunca compara contra otros productos de la categoria.
"""

from datetime import datetime, timedelta, timezone

# Cuanta historia real necesitamos antes de confiar en la referencia propia.
# Antes bastaban 3 filas; corriendo cada hora eso eran 3 horas, asi que la
# "mediana historica" era practicamente el precio actual y nunca disparaba.
MIN_SPAN_HOURS = 48

# Ventana de referencia por defecto.
DEFAULT_WINDOW_DAYS = 30

# Piso de peso de un tramo, para que un cambio de precio recien detectado no
# entre con peso ~0 a la mediana. Atado al intervalo de escaneo mas corto.
MIN_SEGMENT_WEIGHT_SECONDS = 900

# Etiquetas de severidad segun cuanto cae el precio vs. su referencia. Son
# solo nombres: quien decide si se alerta es alert_max_ratio, configurable
# por perfil.
DROP_TIERS = (
    (0.50, "ULTRA descuento"),  # cae a <=50% de su referencia
    (0.65, "Gran descuento"),  # cae a <=65%
    (0.80, "Descuento leve"),  # cae a <=80%
)

# El "precio normal" que reporta la tienda viene inflado por campanas
# comerciales, asi que como fuente es mas debil que la historia propia y
# nunca lo dejamos mas permisivo que esto.
STORE_DISCOUNT_MAX_RATIO = 0.50

_TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f")


def utcnow():
    """SQLite guarda CURRENT_TIMESTAMP en UTC; comparamos contra eso."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


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


def has_mature_history(segments, now=None) -> bool:
    """True si el producto ya acumulo historia suficiente para usar su mediana."""
    if not segments:
        return False
    first = _parse_ts(segments[0]["first_seen"])
    if first is None:
        return False
    return (now or utcnow()) - first >= timedelta(hours=MIN_SPAN_HOURS)


def baseline_from_segments(segments, window_days=DEFAULT_WINDOW_DAYS, now=None):
    """Mediana ponderada por TIEMPO sobre los ultimos `window_days`.

    Ponderar por tiempo (y no por cantidad de muestras) evita que una falla de
    precio que dura 2 horas arrastre su propia referencia, y hace que un precio
    que se sostuvo una semana pese lo que corresponde.

    Cada tramo dura hasta que empieza el siguiente; el ultimo, hasta ahora. Eso
    permite que el scanner no escriba nada cuando el precio no cambia (ver
    db.flush_prices), a costa de tener que recortar aca contra la ventana: un
    tramo que empezo hace 90 dias solo aporta los ultimos 30.

    Devuelve (baseline, span_hours, minimo_en_ventana).
    """
    now = now or utcnow()
    window_start = now - timedelta(days=window_days)

    starts = []
    for seg in segments:
        start = _parse_ts(seg["first_seen"])
        if start is not None:
            starts.append((start, seg["price"]))
    if not starts:
        return None, 0.0, None
    starts.sort()

    pairs = []
    prices = []
    for i, (start, price) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else now
        lo, hi = max(start, window_start), min(end, now)
        if hi <= lo:
            continue  # el tramo quedo fuera de la ventana
        weight = max((hi - lo).total_seconds(), MIN_SEGMENT_WEIGHT_SECONDS)
        pairs.append((price, weight))
        prices.append(price)

    if not pairs:
        return None, 0.0, None

    span_hours = max((now - max(starts[0][0], window_start)).total_seconds() / 3600, 0.0)
    return weighted_median(pairs), span_hours, min(prices)


def check_anomaly(
    product: dict,
    segments: list,
    min_price: int = 0,
    alert_max_ratio: float = 0.50,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now=None,
):
    """Devuelve dict(reason, ratio, source, baseline) o None si no hay nada raro.

    `alert_max_ratio` es el gatillo: el precio debe caer a esa fraccion o menos
    de su referencia. Como el precio que seguimos es el minimo entre tiendas,
    cuando la tienda mas barata se queda sin stock el precio "salta", y al
    reponer "cae" de golpe sin que nadie haya cambiado nada. El umbral existe
    para dejar ese ruido por debajo.

    MEDICION CORREGIDA (27-07-2026, 40-45 productos de perfumes con precios
    reales POR TIENDA, no agregados):

        mediana 3% · p90 50% · maximo 260% · el 10% supera el 50%

    La medicion anterior anotada aqui ("mediana 20%, p90 40%, maximo 45%, ningun
    producto sobre 50%") NO se sostiene: la cola es mucho peor. Los extremos son
    fallos de emparejamiento de Solotodo -- un Lancome L'Elixir *refill* de
    $36.990 agrupado con el frasco completo de $132.990.

    Aun asi el umbral 0.50 se mantiene, y no por inercia: de 45 productos, solo
    1 podia cruzarlo por un quiebre de stock, y ese ya quedaba fuera por
    `min_price`. El riesgo real es ~2%, no cero, y el piso absoluto lo absorbe.

    Si algun dia se baja `min_price`, hay que rehacer esta medicion antes.
    """
    price = product.get("price")
    if not price or price <= 0:
        return None

    # Piso absoluto: una caida del 60% en un producto de $3.000 es ruido, no
    # una falla de precio aprovechable.
    if min_price and price < min_price:
        return None

    baseline, span_hours, historic_min = baseline_from_segments(
        segments, window_days=window_days, now=now
    )

    if baseline and span_hours >= MIN_SPAN_HOURS:
        ratio = price / baseline
        if ratio <= alert_max_ratio:
            drop_pct = round((1 - ratio) * 100)
            extra = ""
            if historic_min is not None and price < historic_min:
                extra = " - es su precio mas bajo registrado"
            return {
                "reason": (
                    f"{_drop_tier(ratio) or 'Descuento'}: cayo {drop_pct}% vs su "
                    f"mediana de {span_hours / 24:.0f}d "
                    f"({_clp(baseline)} -> {_clp(price)}){extra}"
                ),
                "ratio": ratio,
                "source": "history",
                "baseline": baseline,
            }
        # Con historia confiable, el descuento que reporta la tienda no aporta
        # nada: la referencia propia manda.
        return None

    old_price = product.get("old_price")
    if old_price and old_price > price:
        ratio = price / old_price
        if ratio <= min(alert_max_ratio, STORE_DISCOUNT_MAX_RATIO):
            discount = product.get("discount") or round((1 - ratio) * 100)
            return {
                "reason": (
                    f"Descuento fuerte reportado por la tienda: {discount}% "
                    f"({_clp(old_price)} -> {_clp(price)}) - sin historia propia todavia"
                ),
                "ratio": ratio,
                "source": "store",
                "baseline": old_price,
            }

    return None
