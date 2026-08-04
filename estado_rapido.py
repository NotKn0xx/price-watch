"""Estado efimero de la capa rapida: cabeceras condicionales, huellas y contador.

Va en un sidecar y NO en el .db, por la misma razon que .observaciones-*.json: el
.db se commitea, y esto cambia en cada disparo. Guardar aca las cabeceras de 300
entidades cada 10 minutos dejaria un diff binario por corrida y haria crecer el
repo sin control, que es exactamente el problema que price_history ya resolvio
comprimiendo tramos.

Como el runner de Actions es efimero, el sidecar se conserva entre corridas con
actions/cache, no con git. Perderlo no rompe nada: sin cabeceras previas la
siguiente corrida pide sin condicional (un 200 en vez de un 304) y sin huellas
previas marca todo como cambiado una vez. Se degrada, no falla.
"""

import json
from pathlib import Path

VERSION = 1


def ruta_para(perfil):
    return Path(f".estado-rapido-{perfil}.json")


def cargar(perfil):
    """Devuelve (disparo, cache). Cache es {entity_id: {etag, last_modified, huella}}."""
    ruta = ruta_para(perfil)
    if not ruta.exists():
        return 0, {}
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        # Un sidecar corrupto no puede tumbar la corrida: se descarta y se empieza
        # de cero, que es un estado valido.
        return 0, {}

    if datos.get("version") != VERSION:
        return 0, {}

    # Las claves de JSON son texto; los entity_id se usan como enteros.
    cache = {int(k): v for k, v in (datos.get("cache") or {}).items()}
    return int(datos.get("disparo") or 0), cache


def guardar(perfil, disparo, cache):
    """Vuelca el estado. `cache` puede venir con claves int o str."""
    ruta = ruta_para(perfil)
    ruta.write_text(
        json.dumps(
            {
                "version": VERSION,
                "disparo": int(disparo),
                "cache": {str(k): v for k, v in cache.items()},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return ruta


def podar(cache, entity_ids):
    """Descarta entradas de entidades que ya no estan en la watchlist.

    Sin esto el sidecar crece indefinidamente: cada rotacion de la watchlist deja
    atras cabeceras de entidades que nadie volvera a consultar.
    """
    vigentes = set(entity_ids)
    return {k: v for k, v in cache.items() if k in vigentes}
