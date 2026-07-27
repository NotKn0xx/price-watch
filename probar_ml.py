"""
Diagnostico de la API de Mercado Libre.

Responde una sola pregunta: con un token de `client_credentials` (de aplicacion,
sin usuario detras), que endpoints de productos podemos leer?

Si la respuesta es "los que necesitamos", el bot pide su propio token en cada
corrida y no hace falta guardar ni rotar nada. Si no, hay que montar el flujo de
authorization_code con refresh token rotatorio y un Worker que lo custodie.

Uso:
    python probar_ml.py

Pide el client secret por prompt oculto: no queda en el historial del shell ni
en los argumentos del proceso. NUNCA imprime el token ni el secret.
"""

import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
API = "https://api.mercadolibre.com"
SITIO = "MLC"  # Chile


def pedir(nombre, oculto=False):
    valor = os.environ.get(nombre)
    if valor:
        print(f"  {nombre}: tomado del entorno")
        return valor.strip()
    if oculto:
        return getpass.getpass(f"  Pega el {nombre} (no se vera al escribir): ").strip()
    return input(f"  {nombre}: ").strip()


def post_form(url, datos):
    cuerpo = urllib.parse.urlencode(datos).encode()
    pet = urllib.request.Request(
        url,
        data=cuerpo,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(pet, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        detalle = e.read().decode("utf-8", "replace")[:400]
        return e.code, detalle


def get(ruta, token):
    pet = urllib.request.Request(
        API + ruta,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(pet, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:200]
    except Exception as e:  # timeouts, DNS, etc.
        return 0, str(e)[:200]


def resumir(datos):
    """Una linea que diga si vino algo util, sin volcar la respuesta entera."""
    if isinstance(datos, str):
        return datos.replace("\n", " ")[:140]
    if isinstance(datos, dict):
        for clave in ("results", "products"):
            if isinstance(datos.get(clave), list):
                n = len(datos[clave])
                if n and isinstance(datos[clave][0], dict):
                    p = datos[clave][0]
                    nombre = p.get("name") or p.get("title") or "?"
                    precio = p.get("price", p.get("buy_box_winner", {}).get("price"))
                    return f"{n} resultados · ej: {str(nombre)[:48]} · precio {precio}"
                return f"{n} resultados"
        if "name" in datos or "title" in datos:
            return f"{str(datos.get('name') or datos.get('title'))[:60]}"
        return "respuesta ok · " + ", ".join(list(datos)[:6])
    return str(datos)[:120]


def main():
    print("\n=== Credenciales ===")
    client_id = pedir("ML_CLIENT_ID")
    client_secret = pedir("ML_CLIENT_SECRET", oculto=True)
    if not client_id or not client_secret:
        sys.exit("Faltan credenciales.")

    print("\n=== Paso 1: token por client_credentials ===")
    estado, datos = post_form(
        TOKEN_URL,
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )

    if estado != 200 or not isinstance(datos, dict) or "access_token" not in datos:
        print(f"  FALLO (HTTP {estado})")
        print(f"  {resumir(datos)}")
        print(
            "\n  Conclusion: client_credentials NO sirve.\n"
            "  Hay que usar authorization_code + refresh token, y el Worker debe\n"
            "  custodiar y rotar el refresh token."
        )
        return

    token = datos["access_token"]
    # Del token solo se informa que existe y cuanto dura. Nunca su valor.
    print(f"  OK · token recibido ({len(token)} caracteres, no se muestra)")
    print(f"  expira_en: {datos.get('expires_in')}s · scope: {datos.get('scope')}")
    print(f"  trae refresh_token: {'refresh_token' in datos}")

    print("\n=== Paso 2: que endpoints responden ===")
    pruebas = [
        ("busqueda clasica de items", f"/sites/{SITIO}/search?q=iphone&limit=1"),
        ("busqueda de productos de catalogo",
         f"/products/search?site_id={SITIO}&status=active&q=iphone&limit=1"),
        ("categorias del sitio", f"/sites/{SITIO}/categories"),
        ("detalle de categoria (celulares)", "/categories/MLC1055"),
        ("tendencias por categoria", f"/trends/{SITIO}/MLC1055"),
        ("moneda", "/currencies/CLP"),
    ]

    sirve = []
    for etiqueta, ruta in pruebas:
        estado, datos = get(ruta, token)
        marca = "OK  " if estado == 200 else "FALLA"
        print(f"  [{marca}] {estado:>3}  {etiqueta}")
        print(f"           {resumir(datos)}")
        if estado == 200:
            sirve.append(etiqueta)

    print("\n=== Conclusion ===")
    if sirve:
        print("  client_credentials SI da acceso a:")
        for s in sirve:
            print(f"    - {s}")
        print(
            "\n  Si entre esos esta la busqueda de productos con precios, el bot puede\n"
            "  pedir su token en cada corrida: sin refresh token, sin rotacion y sin\n"
            "  Worker intermediario."
        )
    else:
        print(
            "  El token se emite pero no abre ningun endpoint de productos.\n"
            "  Toca authorization_code + refresh token con el Worker de custodia."
        )


if __name__ == "__main__":
    main()
