"""Decide QUE entidades vigila la capa rapida y CADA CUANTO.

El cupo NO lo impone la plataforma. Conviene dejarlo dicho porque es facil
justificarlo mal: el plan gratuito de Workers limita a 50 subpeticiones externas
por invocacion, pero ese limite no aplica aca, porque el Worker solo dispara y el
scraping ocurre en el runner (ver capa_rapida: el tope de 10ms de CPU del plan
gratuito hace imposible parsear dentro del Worker).

El cupo es una decision de cortesia. Las 9 tiendas grandes medidas devuelven 200
sin bloqueo, pero eso se midio a 6 peticiones/dia. Pasar de golpe a miles es
exactamente lo que despierta defensas que hoy estan dormidas. 50 por disparo, con
el cron de 10 min ya existente (90 disparos/dia), son ~4.500 peticiones/dia
repartidas entre 30 tiendas: unas 150 por tienda al dia. Se sube por etapas y
midiendo.

Aun con cupo holgado, repartirlo parejo seria un desperdicio. Medido: una RTX 5070
tuvo 2 precios distintos en 393 dias. Vigilar eso cada 10 minutos es gastar 56.000
peticiones al ano para observar dos cambios.

De ahi el escalonamiento: la frecuencia se asigna por cuanto se mueve el precio,
no parejo.
"""

# URL por disparo. Presupuesto de cortesia, revisable al alza midiendo, no un
# limite de plataforma.
CUPO_POR_DISPARO = 50

# Cada cuantos disparos le toca a cada nivel. Con el cron actual de 10 min:
#   ALTA  -> cada 10 min     MEDIA -> cada 1h     BAJA -> cada 6h
CADENCIA = {"alta": 1, "media": 6, "baja": 36}

# Reparto del cupo. La mayoria del presupuesto va a lo que se mueve; `baja` existe
# para que una entidad estable no desaparezca del todo y podamos notar si empieza
# a moverse.
REPARTO = {"alta": 0.50, "media": 0.30, "baja": 0.20}

# Umbrales de volatilidad (rango intercuantil relativo, ver capa_lenta.resumen).
# Calibrados contra lo medido: hardware da 4-8% de dispersion entre tiendas y
# perfumeria 0% mediana con p90 45%. Un producto que en 90 dias movio menos del 3%
# de su baseline no justifica una ranura rapida.
VOLATIL_ALTA = 0.15
VOLATIL_MEDIA = 0.03

# Piso de precio. Heredado de los perfiles por la misma razon: una caida del 60%
# en algo de $3.000 no es una falla de precio aprovechable.
MIN_PRECIO_CLP = 20000


def puntuar(resumen, parseable=True, min_precio=MIN_PRECIO_CLP):
    """Puntaje de interes de una entidad para la capa rapida.

    Combina tres cosas, y ninguna sirve sola:

      volatilidad -> si nunca se mueve, mirarla seguido no encuentra nada
      cercania al minimo historico -> ya esta en zona interesante
      valor -> una caida grande sobre un precio chico no paga el viaje

    Devuelve 0.0 si la entidad no es candidata (no parseable, muy barata, sin
    historia suficiente).
    """
    if not resumen or not parseable:
        return 0.0

    precio = resumen.get("precio") or 0
    if precio < min_precio:
        return 0.0

    volatilidad = resumen.get("volatilidad") or 0.0
    if volatilidad < VOLATIL_MEDIA:
        # Plana de verdad. Sigue siendo elegible para `baja`, pero con puntaje
        # bajo: la unica razon de mirarla es enterarse si deja de ser plana.
        base = 0.1
    else:
        base = min(volatilidad / VOLATIL_ALTA, 2.0)

    # Un percentil bajo significa que hoy esta mas barata que casi toda su historia.
    percentil = resumen.get("percentil")
    cercania = (1.0 - percentil) if percentil is not None else 0.0

    # El valor entra con logaritmo: un producto de $2.000.000 importa mas que uno
    # de $50.000, pero no cuarenta veces mas.
    from math import log10

    valor = log10(max(precio, min_precio) / min_precio + 1)

    return base * (1.0 + cercania) * (1.0 + valor)


def nivel(resumen, parseable=True, min_precio=MIN_PRECIO_CLP):
    """Nivel de vigilancia: 'alta', 'media', 'baja' o None (no se vigila)."""
    if puntuar(resumen, parseable, min_precio) <= 0.0:
        return None
    volatilidad = resumen.get("volatilidad") or 0.0
    if volatilidad >= VOLATIL_ALTA:
        return "alta"
    if volatilidad >= VOLATIL_MEDIA:
        return "media"
    return "baja"


def construir_watchlist(candidatos, cupo=CUPO_POR_DISPARO, reparto=None):
    """Selecciona y clasifica las entidades a vigilar.

    `candidatos` son dicts con al menos entity_id, store_id, url, resumen y
    parseable. Devuelve la lista con `nivel` y `puntaje` asignados, recortada al
    cupo de cada nivel.

    El cupo se calcula sobre las ranuras EFECTIVAS de cada nivel, no sobre el cupo
    por disparo: `media` solo se consulta 1 de cada 6 disparos, asi que puede tener
    6 veces mas entidades sin gastar mas por disparo. Sin esta correccion el
    escalonamiento no ahorraria nada.
    """
    reparto = reparto or REPARTO
    por_nivel = {"alta": [], "media": [], "baja": []}

    for c in candidatos:
        n = nivel(c.get("resumen"), c.get("parseable", True))
        if n is None:
            continue
        c = dict(c)
        c["nivel"] = n
        c["puntaje"] = puntuar(c.get("resumen"), c.get("parseable", True))
        por_nivel[n].append(c)

    seleccion = []
    for n, lista in por_nivel.items():
        lista.sort(key=lambda c: c["puntaje"], reverse=True)
        ranuras = int(cupo * reparto.get(n, 0.0) * CADENCIA[n])
        seleccion.extend(lista[:ranuras])

    return seleccion


def toca_ahora(nivel_entidad, disparo, semilla=0):
    """Si a esta entidad le toca en el disparo numero `disparo`.

    El reparto dentro del ciclo NO es un detalle: sin el, el escalonamiento se
    rompe. Reproducido con la watchlist real de perfumeria (341 entidades: 25
    alta, 25 media, 291 baja):

        disparo 36 -> lote de 50: 25 alta + 25 media + **0 baja**

    Con `disparo % cada == 0` todo un nivel dispara de golpe, asi que en el
    disparo 36 las 291 entidades de `baja` competian por las ranuras que sobraban
    del cupo, perdian el ordenamiento por puntaje, y quedaban sin consultarse
    NUNCA. Un tercio de la watchlist era decorativo y nada lo delataba.

    La correccion es desplazar cada entidad dentro de su ciclo con `semilla`
    (el entity_id): una entidad de `media` dispara cuando disparo % 6 coincide con
    su propio resto, de modo que en cada disparo entra ~1/6 del nivel en vez del
    nivel entero. Eso ademas vuelve valida la formula de ranuras de
    `construir_watchlist`, que ya asumia este reparto.
    """
    cada = CADENCIA.get(nivel_entidad)
    if not cada:
        return False
    return disparo % cada == semilla % cada


def lote_del_disparo(watchlist, disparo, cupo=CUPO_POR_DISPARO):
    """Las entidades que se consultan en este disparo, respetando el cupo.

    El cupo deberia sobrar casi siempre gracias al reparto de `toca_ahora`. Si
    aun asi se excede, se prioriza por puntaje y el resto espera: preferimos
    postergar lo menos interesante antes que pasarnos del presupuesto.
    """
    lote = [
        c for c in watchlist
        if toca_ahora(c.get("nivel"), disparo, c.get("entity_id") or 0)
    ]
    lote.sort(key=lambda c: c.get("puntaje", 0.0), reverse=True)
    return lote[:cupo]


def resumen_reparto(watchlist):
    """Conteo por nivel y peticiones/dia estimadas, para el panel de la corrida."""
    conteo = {"alta": 0, "media": 0, "baja": 0}
    for c in watchlist:
        if c.get("nivel") in conteo:
            conteo[c["nivel"]] += 1

    # 90 disparos/dia con el cron actual (cada 10 min, 12:00-02:59 UTC).
    disparos_dia = 90
    peticiones = sum(n * (disparos_dia / CADENCIA[k]) for k, n in conteo.items())
    return conteo, round(peticiones)
