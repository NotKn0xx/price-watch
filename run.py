import importlib
import os
import time
import traceback

from dotenv import load_dotenv

load_dotenv()

from db import (
    count_alerts_since,
    flush_prices,
    get_conn,
    init_db,
    load_recent_alerts,
    load_segments,
    prune_history,
    purge_foreign_categories,
    record_alert,
    upsert_products,
)
from detector import check_anomaly, has_mature_history, utcnow
from notifier import send_alert
from reaplicar import guardar
from solotodo import best_entity_for_alert, browse_category
from lectura_producto import precio_clp

PROFILE = os.environ.get("PROFILE", "perfumes")
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
profile = importlib.import_module(f"profiles.{PROFILE}")

SOLOTODO_PRODUCT_URL = "https://www.solotodo.cl/products/{}"


def _cfg(name, default):
    return getattr(profile, name, default)


def scan_category(conn, target, store_ids):
    """Escanea una categoria, persiste precios y devuelve (candidatos, stats,
    observaciones).

    Las observaciones se devuelven para poder reaplicarlas sobre un .db fresco
    si el remoto avanza y el rebase conflictua en el binario. Ver reaplicar.py.
    """
    category_name = target["name"]
    now = utcnow()

    products = list(
        browse_category(
            target["id"],
            store_ids,
            db_brand_id=target.get("db_brand_id"),
            page_size=_cfg("PAGE_SIZE", 100),
            max_pages=_cfg("MAX_PAGES_PER_CATEGORY", 50),
            page_delay=_cfg("REQUEST_DELAY_SECONDS", 0.3),
        )
    )
    stats = {"categoria": category_name, "productos": 0, "con_historia": 0, "cambios": 0, "candidatos": 0}
    if not products:
        return [], stats, []

    ids = [p["product_id"] for p in products]

    # Todo el estado de la categoria en 2 queries, en vez de una por producto.
    segments = load_segments(conn, ids)
    recent_alerts = load_recent_alerts(conn, ids, within_hours=_cfg("ALERT_COOLDOWN_HOURS", 24))

    min_price = _cfg("MIN_PRICE_CLP", 0)
    max_ratio = _cfg("ALERT_MAX_RATIO", 0.50)
    window_days = _cfg("HISTORY_WINDOW_DAYS", 30)
    redrop = _cfg("REALERT_ON_EXTRA_DROP", 0.10)
    candidates = []

    for product in products:
        segs = segments.get(product["product_id"], [])
        if has_mature_history(segs, now):
            stats["con_historia"] += 1

        finding = check_anomaly(
            product, segs, min_price=min_price, alert_max_ratio=max_ratio,
            window_days=window_days, now=now,
        )
        if not finding:
            continue

        already = recent_alerts.get(product["product_id"])
        # Reabrimos la alerta si el precio siguio bajando de forma relevante;
        # un cooldown de 24h fijas se comeria justamente la mejor caida.
        if already is not None and product["price"] > already * (1 - redrop):
            continue
        candidates.append((product, finding, category_name))

    upsert_products(conn, products)
    observaciones = [(p["product_id"], p["price"]) for p in products]
    _, changed = flush_prices(conn, segments, observaciones)

    stats.update(productos=len(products), cambios=changed, candidatos=len(candidates))
    return candidates, stats, observaciones


def build_message(product, finding, entity, store_name, category_name):
    price = f"${product['price']:,.0f}".replace(",", ".")
    return (
        f"[{category_name}] posible falla de precio\n"
        f"{product['name']}\n\n"
        f"{price} en {store_name}\n"
        f"{finding['reason']}\n\n"
        f"Comprar: {entity['external_url']}\n"
        f"Comparar: {SOLOTODO_PRODUCT_URL.format(product['product_id'])}"
    )


def run_once():
    init_db()
    store_ids = list(profile.STORE_IDS.keys())
    started = time.time()
    all_stats, candidates, errores, observaciones = [], [], [], []

    # --- Fase 1: escanear y persistir. Se commitea al salir del `with`, o sea
    # ANTES de tocar Telegram: una caida de Telegram ya no puede hacernos
    # perder el historial recolectado en esta corrida.
    with get_conn() as conn:
        # El perfil manda: si el .db trae categorias que ya no se vigilan
        # (tipico al reutilizar un archivo tras redefinir perfiles), se van.
        sobrantes = purge_foreign_categories(conn, [c["id"] for c in profile.CATEGORIES])
        if sobrantes:
            print(f"Descartados {sobrantes} productos de categorias ajenas al perfil '{PROFILE}'")

        for target in profile.CATEGORIES:
            print(f"Escaneando '{target['name']}'...")
            try:
                found, stats, observadas = scan_category(conn, target, store_ids)
                candidates += found
                all_stats.append(stats)
                observaciones += observadas
                print(
                    f"  {stats['productos']} productos | {stats['con_historia']} con historia "
                    f"| {stats['cambios']} cambios de precio | {stats['candidatos']} candidatos"
                )
            except Exception as exc:
                errores.append(f"{target['name']}: {exc}")
                print(f"  Error en '{target['name']}':")
                traceback.print_exc()
        prune_history(conn, keep_days=_cfg("KEEP_HISTORY_DAYS", 90))

    # Siempre, aunque no haya habido cambios: si el remoto avanza y el rebase
    # conflictua en el .db binario, esto es lo unico que permite reconstruir la
    # corrida sin repetir peticiones. Ver reaplicar.py.
    guardar(PROFILE, catalogo_obs=observaciones)

    # --- Fase 2: alertar. Transaccion aparte y commit por alerta.
    candidates.sort(key=lambda c: c[1]["ratio"])  # ratio mas bajo = caida mas fuerte
    enviadas, descartadas = 0, 0

    with get_conn() as conn:
        presupuesto = _cfg("MAX_ALERTS_PER_DAY", 40) - count_alerts_since(conn, 24)
        cupo = min(_cfg("MAX_ALERTS_PER_RUN", 5), max(presupuesto, 0))
        if candidates and cupo <= 0:
            print(f"  Presupuesto diario de alertas agotado ({_cfg('MAX_ALERTS_PER_DAY', 40)})")

        max_chequeos = _cfg("MAX_ALERT_CHECKS_PER_RUN", 25)
        for chequeos, (product, finding, category_name) in enumerate(candidates):
            if enviadas >= cupo or chequeos >= max_chequeos:
                break

            entity = best_entity_for_alert(product["product_id"], store_ids)
            if entity is None:
                # browse lista productos sin ninguna entidad disponible (~7%
                # de la muestra): no es una oferta comprable ahora mismo.
                descartadas += 1
                continue

            # El minimo agregado de browse puede estar atrasado o venir de otra
            # ficha. Precio, descuento, enlace y registro deben describir la
            # misma entidad que acabamos de comprobar.
            registro = entity.get("active_registry") or {}
            precio = precio_clp(registro.get("offer_price"))
            product = {**product, "price": precio,
                       "old_price": precio_clp(registro.get("normal_price")), "discount": None}
            finding = check_anomaly(
                product, load_segments(conn, [product["product_id"]]).get(product["product_id"], []),
                min_price=_cfg("MIN_PRICE_CLP", 0),
                alert_max_ratio=_cfg("ALERT_MAX_RATIO", 0.50),
                window_days=_cfg("HISTORY_WINDOW_DAYS", 30),
            )
            if not finding:
                descartadas += 1
                continue
            reciente = load_recent_alerts(conn, [product["product_id"]],
                within_hours=_cfg("ALERT_COOLDOWN_HOURS", 24)).get(product["product_id"])
            if reciente is not None and precio > reciente * (1 - _cfg("REALERT_ON_EXTRA_DROP", 0.10)):
                continue

            store_name = profile.STORE_IDS.get(entity["store_id"], "?")
            msg = build_message(product, finding, entity, store_name, category_name)

            if DRY_RUN:
                print("  [DRY RUN]", msg.replace("\n", " | "))
                enviadas += 1
                continue

            if send_alert(msg):
                record_alert(
                    conn, product["product_id"], product["price"],
                    finding["reason"], entity["store_id"], entity["external_url"], journal_profile=PROFILE,
                )
                conn.commit()  # cada alerta queda registrada apenas sale
                enviadas += 1
                print("  ALERTA:", product["name"])
            else:
                # No la registramos: si fue un fallo transitorio, la proxima
                # corrida vuelve a intentarlo.
                print("  (no se pudo enviar, se reintenta en la proxima corrida)")

    duracion = time.time() - started
    print(f"Listo en {duracion:.1f}s | {enviadas} alertas | {descartadas} sin stock")
    _write_summary(all_stats, enviadas, descartadas, errores, duracion)
    if errores:
        raise RuntimeError(f"Fallaron {len(errores)} categorias; el historial disponible fue guardado")
    return enviadas


def _write_summary(all_stats, enviadas, descartadas, errores, duracion):
    """Resumen al panel de la corrida en GitHub Actions.

    Sin esto una corrida exitosa sin cambios de precio no deja ninguna huella
    (justamente porque no commitea), y se vuelve indistinguible de un workflow
    que nunca corrio.
    """
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lineas = [
        f"## {PROFILE}",
        "",
        "| Categoria | Productos | Con historia | Cambios | Candidatos |",
        "|---|--:|--:|--:|--:|",
    ]
    for s in all_stats:
        lineas.append(
            f"| {s['categoria']} | {s['productos']} | {s['con_historia']} "
            f"| {s['cambios']} | {s['candidatos']} |"
        )
    lineas += ["", f"**{enviadas} alertas enviadas** en {duracion:.1f}s."]
    if descartadas:
        lineas.append(f" {descartadas} candidatos descartados por falta de stock.")
    if errores:
        lineas += ["", "### Errores", *(f"- {e}" for e in errores)]
    lineas.append("")
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lineas) + "\n")
    except OSError as exc:
        print(f"  (no se pudo escribir el resumen: {exc})")


if __name__ == "__main__":
    run_once()
