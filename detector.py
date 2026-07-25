from statistics import median

MIN_HISTORY = 3

# Niveles de severidad segun cuanto cae el precio vs. su referencia
# (mediana historica propia, o precio normal reportado por la tienda).
# Se evalua de mas estricto a menos estricto y gana el primero que matchea.
DROP_TIERS = (
    (0.50, "ULTRA descuento"),  # cae a <=50% de su referencia
    (0.65, "Gran descuento"),  # cae a <=65%
    (0.80, "Descuento leve"),  # cae a <=80%
)


def _clp(n) -> str:
    return f"${n:,.0f}".replace(",", ".")


def _drop_tier(ratio: float) -> str:
    for threshold, label in DROP_TIERS:
        if ratio <= threshold:
            return label
    return None


def check_anomaly(product: dict, history_prices: list) -> str:
    """Devuelve un motivo de alerta (str) o None si no hay nada raro.

    Compara el precio actual del producto contra SU PROPIA referencia
    (mediana historica del producto, o precio normal que reporta la
    tienda), nunca contra otros productos de la categoria.
    """
    price = product.get("price")
    if not price or price <= 0:
        return None

    if len(history_prices) >= MIN_HISTORY:
        baseline = median(history_prices)
        if baseline:
            tier = _drop_tier(price / baseline)
            if tier:
                drop_pct = round((1 - price / baseline) * 100)
                return (
                    f"{tier}: cayo {drop_pct}% vs su mediana historica "
                    f"({_clp(baseline)} -> {_clp(price)})"
                )

    # Sin historial propio todavia, la unica referencia es el "precio normal"
    # que reporta la misma tienda -- y eso incluye promociones normales de
    # 20-40% off. Solo vale la pena confiar en el nivel mas severo (ULTRA)
    # para no alertar con descuentos de campana comercial corriente.
    old_price = product.get("old_price")
    if old_price and old_price > price:
        ratio = price / old_price
        ultra_threshold = DROP_TIERS[0][0]
        if ratio <= ultra_threshold:
            discount = product.get("discount") or 0
            return (
                f"ULTRA descuento reportado por la tienda: {discount}% "
                f"({_clp(old_price)} -> {_clp(price)})"
            )

    return None
