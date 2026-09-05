"""Estado de las dos capas: watchlist, cabeceras condicionales, huellas, contador.

Va en un sidecar y NO en el .db, y la segunda parte es la importante.

POR QUE NO EN EL .db. Primero por volumen: las cabeceras y huellas de 300
entidades cambian en cada disparo (cada 10 min) y el .db se commitea, asi que
dejaria un diff binario por corrida.

Pero la razon de fondo la descubrio un fallo real en produccion. La watchlist si
estuvo en el .db, y aparecio commiteada con CERO filas mientras en local se
construia bien. El mecanismo:

    git push origin main            # rechazado, el bot pushea cada 10 min
    git reset --hard FETCH_HEAD     # descarta el .db propio, watchlist incluida
    python reaplicar.py ...         # solo sabe de precios; su init_db() recrea
                                    # la tabla vacia, y ESA es la que se commitea

`reaplicar.py` existe porque el .db es binario y git no lo puede fusionar, asi
que ante un conflicto se descarta el propio y se reaplican las observaciones
sobre el del remoto. Funciona para los precios porque hay un sidecar con ellos.
La watchlist no tenia sidecar, asi que se perdia entera en cada push rechazado
-- y sin ruido: el workflow salia verde y la tabla existia, solo que vacia.

La conclusion general: no metas en el .db nada que la ruta de recuperacion de
conflictos no sepa reconstruir. La watchlist ademas es estado DERIVADO --
recomputable desde Solotodo en cualquier momento -- asi que no gana nada con
estar versionada.

Se conserva entre corridas con actions/cache. Perderlo no rompe nada: la
watchlist se reconstruye sola (`vencida()` da True sin datos) y las cabeceras
ausentes solo cuestan un ciclo sin peticiones condicionales.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

# v2: la watchlist se movio del .db a este sidecar. Un archivo v1 se descarta.
VERSION = 3  # invalida huellas anteriores a la validacion de moneda y stock


def ruta_para(perfil):
    return Path(f".estado-rapido-{perfil}.json")


def _ahora():
    return datetime.now(timezone.utc)


def cargar(perfil):
    """Devuelve el estado completo como dict, o uno vacio si no hay o no sirve."""
    vacio = {"disparo": 0, "cache": {}, "watchlist": [], "watchlist_ts": None,
             "ultimo_intento_lenta": None, "rotacion": 0}
    ruta = ruta_para(perfil)
    if not ruta.exists():
        return vacio
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        # Un sidecar corrupto no puede tumbar la corrida: se descarta y se empieza
        # de cero, que es un estado valido.
        return vacio

    if not isinstance(datos, dict) or datos.get("version") != VERSION:
        return vacio
    try:
        result = {
            "disparo": int(datos.get("disparo") or 0),
            # Las claves de JSON son texto; los entity_id se usan como enteros.
            "cache": {int(k): v for k, v in (datos.get("cache") or {}).items()},
            "watchlist": datos.get("watchlist") or [],
            "watchlist_ts": datos.get("watchlist_ts"),
            "ultimo_intento_lenta": datos.get("ultimo_intento_lenta"),
            "rotacion": int(datos.get("rotacion") or 0),
        }
        if (result["disparo"] < 0 or not isinstance(result["watchlist"], list)
                or any(not isinstance(v, dict) for v in result["cache"].values())
                or any(not isinstance(v, dict) or not all(k in v for k in
                    ("entity_id", "store_id", "url", "precio")) for v in result["watchlist"])):
            return vacio
        return result
    except (ValueError, TypeError, AttributeError):
        return vacio


def guardar(perfil, disparo, cache, watchlist=None, watchlist_ts=None,
            ultimo_intento_lenta=None, rotacion=0):
    """Vuelca el estado. `cache` puede venir con claves int o str."""
    ruta = ruta_para(perfil)
    temporal = ruta.with_suffix(".tmp")
    temporal.write_text(
        json.dumps(
            {
                "version": VERSION,
                "disparo": int(disparo),
                "cache": {str(k): v for k, v in cache.items()},
                "watchlist": watchlist or [],
                "watchlist_ts": watchlist_ts,
                "ultimo_intento_lenta": ultimo_intento_lenta,
                "rotacion": rotacion,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    temporal.replace(ruta)
    return ruta


def vencida(watchlist, watchlist_ts, horas=12):
    """True si la watchlist no existe o quedo mas vieja que `horas`.

    Es lo que hace que la capa lenta se dispare sola cuando corresponde, sin
    depender de un cron aparte que puede no ejecutarse.
    """
    if not watchlist or not watchlist_ts:
        return True
    try:
        cuando = datetime.fromisoformat(str(watchlist_ts).replace("Z", "+00:00"))
    except ValueError:
        return True
    if cuando.tzinfo is None:
        cuando = cuando.replace(tzinfo=timezone.utc)
    return _ahora() - cuando >= timedelta(hours=horas)


def sello():
    """Marca de tiempo para acompanar una watchlist recien construida."""
    return _ahora().isoformat()


def podar(cache, entity_ids):
    """Descarta entradas de entidades que ya no estan en la watchlist.

    Sin esto el sidecar crece indefinidamente: cada rotacion de la watchlist deja
    atras cabeceras de entidades que nadie volvera a consultar.
    """
    vigentes = set(entity_ids)
    return {k: v for k, v in cache.items() if k in vigentes}
