"""Reaplica las observaciones de una corrida sobre un .db recien traido.

Existe por un fallo real, reproducido en laboratorio: cuando el remoto avanza
mientras la corrida escribe, `git pull --rebase` no puede fusionar el .db porque
es binario. El rebase queda a medias y **los reintentos no pueden funcionar**:
cada uno vuelve a fallar por el rebase pendiente. El workflow sale con error y
las observaciones de esa corrida se pierden para siempre.

La salida es que el .db es estado DERIVADO. Si las observaciones se guardan
aparte, ante un conflicto se descarta el commit propio, se toma el .db del
remoto y se reaplican encima. Sin repetir una sola peticion a la API.

Reaplicar el conjunto completo de observaciones da el resultado correcto porque
flush_* solo escribe cuando el precio cambia: sobre el .db fresco, las que ya
estan no hacen nada y las que faltan abren tramo.

Uso:
    python reaplicar.py .observaciones-perfumes.json
"""

import json
import os
import sys
from pathlib import Path

SUFIJO = ".json"


def ruta_para(perfil):
    """Sidecar por perfil. Va en .gitignore: es estado de una corrida, no del
    repositorio."""
    return Path(f".observaciones-{perfil}{SUFIJO}")


def guardar(perfil, tienda_obs=(), catalogo_obs=()):
    """Vuelca las observaciones de la corrida.

    Se guardan SIEMPRE, no solo cuando hubo cambios: el conflicto puede ocurrir
    igual y sin el volcado no hay nada que reaplicar.
    """
    ruta = ruta_para(perfil)
    ruta.write_text(
        json.dumps(
            {
                "perfil": perfil,
                "tienda": [list(o) for o in tienda_obs],
                "catalogo": [list(o) for o in catalogo_obs],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return ruta


def reaplicar(ruta):
    """Aplica un sidecar sobre el .db que apunte DB_PATH. Devuelve (catalogo,
    tienda) con la cantidad de tramos abiertos en cada tabla."""
    from db import (
        flush_prices,
        flush_store_prices,
        get_conn,
        init_db,
        load_segments,
        load_store_segments,
    )

    datos = json.loads(Path(ruta).read_text(encoding="utf-8"))
    catalogo = [tuple(o) for o in datos.get("catalogo") or []]
    tienda = [tuple(o) for o in datos.get("tienda") or []]

    init_db()
    with get_conn() as conn:
        cambios_catalogo = 0
        if catalogo:
            segs = load_segments(conn, [o[0] for o in catalogo])
            _, cambios_catalogo = flush_prices(conn, segs, catalogo)

        cambios_tienda = 0
        if tienda:
            segs = load_store_segments(conn, [o[0] for o in tienda])
            _, cambios_tienda = flush_store_prices(conn, segs, tienda)

    return cambios_catalogo, cambios_tienda


def main(argv):
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    ruta = Path(argv[1])
    if not ruta.exists():
        # No es un error: una corrida que no observo nada no deja sidecar.
        print(f"sin sidecar {ruta}, nada que reaplicar")
        return 0
    catalogo, tienda = reaplicar(ruta)
    print(f"reaplicado {ruta.name}: {catalogo} tramos de catalogo, {tienda} por tienda")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
