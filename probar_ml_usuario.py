"""
Segundo diagnostico de Mercado Libre: token DE USUARIO (authorization_code).

`client_credentials` quedo descartado: emite token pero la API lo rechaza con 403
de PolicyAgent incluso en /currencies/CLP. Falta saber si un token con usuario
detras si abre la busqueda de productos, porque de eso depende que valga la pena
construir el Worker con custodia y rotacion del refresh token.

No hace falta que el Worker exista. El redirect va a dar a una URL que todavia no
responde, el navegador mostrara un error, y el codigo viene igual en la barra de
direcciones. De ahi lo copias.

Uso:
    python probar_ml_usuario.py

Implementa PKCE (S256) porque la app se creo con "Requiere PKCE" activado.
NUNCA imprime el secret, ni el codigo, ni los tokens.
"""

import base64
import getpass
import hashlib
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

AUTORIZAR = "https://auth.mercadolibre.cl/authorization"
TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
API = "https://api.mercadolibre.com"
SITIO = "MLC"
REDIRECT = "https://price-watch-web.libroclases.workers.dev/oauth/callback"


def b64url(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).decode().rstrip("=")


def pedir(nombre, oculto=False):
    valor = os.environ.get(nombre)
    if valor:
        print(f"  {nombre}: tomado del entorno")
        return valor.strip()
    if oculto:
        return getpass.getpass(f"  {nombre} (no se vera al escribir): ").strip()
    return input(f"  {nombre}: ").strip()


def post_form(url, datos):
    pet = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(datos).encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(pet, timeout=25) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:400]


def get(ruta, token):
    pet = urllib.request.Request(
        API + ruta,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(pet, timeout=25) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:200]
    except Exception as e:
        return 0, str(e)[:200]


def resumir(datos):
    if isinstance(datos, str):
        return datos.replace("\n", " ")[:150]
    if isinstance(datos, dict):
        for clave in ("results", "products"):
            if isinstance(datos.get(clave), list):
                n = len(datos[clave])
                if n and isinstance(datos[clave][0], dict):
                    p = datos[clave][0]
                    nombre = p.get("name") or p.get("title") or "?"
                    precio = p.get("price")
                    if precio is None:
                        precio = (p.get("buy_box_winner") or {}).get("price")
                    return f"{n} resultado(s) · {str(nombre)[:46]} · ${precio}"
                return f"{n} resultado(s)"
        if "name" in datos or "title" in datos:
            return str(datos.get("name") or datos.get("title"))[:70]
        return "ok · claves: " + ", ".join(list(datos)[:6])
    return str(datos)[:120]


def main():
    print("\n=== Credenciales ===")
    client_id = pedir("ML_CLIENT_ID")
    client_secret = pedir("ML_CLIENT_SECRET", oculto=True)
    if not client_id or not client_secret:
        sys.exit("Faltan credenciales.")

    # PKCE: el verifier se queda en memoria; solo viaja su hash.
    verifier = b64url(secrets.token_bytes(64))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    estado = b64url(secrets.token_bytes(16))

    url = AUTORIZAR + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": estado,
    })

    print("\n=== Paso 1: autoriza en tu navegador ===")
    print("\n  Abre esta URL:\n")
    print(f"  {url}\n")
    print("  Autoriza con TU cuenta. Vas a terminar en una pagina de error:")
    print("  es lo esperado, el Worker todavia no existe.")
    print("  Copia la URL COMPLETA de la barra de direcciones y pegala aqui.")
    print("  (El codigo dura pocos minutos y sirve una sola vez. No se lo pases a nadie.)\n")

    devuelta = input("  URL de retorno: ").strip()
    if not devuelta:
        sys.exit("Sin URL, no hay nada que canjear.")

    partes = urllib.parse.urlparse(devuelta)
    params = urllib.parse.parse_qs(partes.query)
    codigo = (params.get("code") or [""])[0]
    estado_vuelto = (params.get("state") or [""])[0]

    if not codigo:
        sys.exit(f"  Esa URL no trae ningun 'code'. Parametros vistos: {list(params)}")

    # Comparacion en tiempo constante: mismo criterio que el resto del proyecto.
    if not secrets.compare_digest(estado_vuelto, estado):
        sys.exit("  El 'state' no coincide. Aborta: la respuesta no corresponde a esta peticion.")
    print("  state verificado · codigo recibido (no se muestra)")

    print("\n=== Paso 2: canjear el codigo por tokens ===")
    est, datos = post_form(TOKEN_URL, {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": codigo,
        "redirect_uri": REDIRECT,
        "code_verifier": verifier,
    })

    if est != 200 or not isinstance(datos, dict) or "access_token" not in datos:
        print(f"  FALLO (HTTP {est})")
        print(f"  {resumir(datos)}")
        return

    token = datos["access_token"]
    print(f"  OK · access token de {len(token)} caracteres (no se muestra)")
    print(f"  expira_en: {datos.get('expires_in')}s")
    print(f"  trae refresh_token: {'refresh_token' in datos}")
    print(f"  scope: {datos.get('scope')}")
    print(f"  user_id: {datos.get('user_id')}")

    print("\n=== Paso 3: la pregunta que importa ===")
    pruebas = [
        ("busqueda clasica de items", f"/sites/{SITIO}/search?q=iphone&limit=3"),
        ("busqueda de catalogo", f"/products/search?site_id={SITIO}&status=active&q=iphone&limit=3"),
        ("items por categoria (celulares)", f"/sites/{SITIO}/search?category=MLC1055&limit=3"),
        ("moneda", "/currencies/CLP"),
        ("mi usuario", "/users/me"),
    ]
    sirve = []
    for etiqueta, ruta in pruebas:
        est, datos = get(ruta, token)
        print(f"  [{'OK  ' if est == 200 else 'FALLA'}] {est:>3}  {etiqueta}")
        print(f"           {resumir(datos)}")
        if est == 200:
            sirve.append(etiqueta)

    print("\n=== Conclusion ===")
    busqueda = any("busqueda" in s or "items por categoria" in s for s in sirve)
    if busqueda:
        print("  El token de usuario SI abre la busqueda de productos.")
        print("  Vale la pena construir el Worker con custodia y rotacion del refresh token.")
    else:
        print("  Ni con token de usuario se puede buscar productos.")
        print("  Mercado Libre NO sirve como fuente de datos: hay que replantear.")
        print("  (SoloTodo sigue funcionando; lo que se cae es la diversificacion de fuente.)")


if __name__ == "__main__":
    main()
