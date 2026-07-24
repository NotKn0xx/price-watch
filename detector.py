from statistics import median

MIN_HISTORY = 3
DROP_RATIO_THRESHOLD = 0.5  # precio actual <= 50% de la mediana historica
MIN_DISCOUNT_FLAG = 60  # % de descuento que reporta el propio sitio

MIN_CATEGORY_SAMPLE = 30  # productos minimos en la categoria para confiar en el percentil
CATEGORY_OUTLIER_PERCENTILE = 0.01  # percentil mas bajo de precios de la categoria en este scan


def _clp(n) -> str:
    return f"${n:,.0f}".replace(",", ".")


def check_anomaly(product: dict, history_prices: list) -> str:
    """Devuelve un motivo de alerta (str) o None si no hay nada raro."""
    price = product.get("price")
    if not price or price <= 0:
        return None

    if len(history_prices) >= MIN_HISTORY:
        baseline = median(history_prices)
        if baseline and price <= baseline * DROP_RATIO_THRESHOLD:
            drop_pct = round((1 - price / baseline) * 100)
            return f"Precio cayo {drop_pct}% vs su mediana historica ({_clp(baseline)} -> {_clp(price)})"

    discount = product.get("discount") or 0
    old_price = product.get("old_price")
    if discount >= MIN_DISCOUNT_FLAG and old_price and price <= old_price * 0.4:
        return f"Descuento sospechoso de {discount}% ({_clp(old_price)} -> {_clp(price)})"

    return None


def check_category_outlier(product: dict, sorted_category_prices: list) -> str:
    """Marca productos en el percentil mas bajo de precio de su categoria en este scan.

    No depende de historial propio: sirve para cachar errores de precio evidentes
    (ej. un producto premium listado muy por debajo de lo normal) desde el primer scan.
    """
    price = product.get("price")
    if not price or price <= 0:
        return None

    n = len(sorted_category_prices)
    if n < MIN_CATEGORY_SAMPLE:
        return None

    cutoff_idx = max(0, int(n * CATEGORY_OUTLIER_PERCENTILE) - 1)
    cutoff_price = sorted_category_prices[cutoff_idx]
    if price <= cutoff_price:
        return f"Precio en el percentil mas bajo de la categoria ({_clp(price)}, {n} productos comparados)"

    return None
