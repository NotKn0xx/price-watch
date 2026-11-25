"""GET de fichas publicas con limites de destino, redireccion y descarga.

Defensa de aplicacion; un firewall de salida sigue siendo necesario para cubrir
DNS rebinding entre la validacion DNS y la conexion hecha por requests.
"""

import ipaddress
import socket
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import requests

MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3


def validar_url(url, host_inicial=None):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in ("https", "http") or not parsed.hostname
                or parsed.username or parsed.password or "\\" in url
                or any(ord(c) < 33 for c in url)
                or parsed.port not in (None, 80, 443)):
            raise ValueError("url_no_permitida")
        host = parsed.hostname.lower().rstrip(".")
        if host_inicial and host.removeprefix("www.") != host_inicial.removeprefix("www."):
            raise ValueError("redireccion_otro_host")
        addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                       type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError("destino_no_publico")
        return host
    except (TypeError, UnicodeError, OSError) as exc:
        raise ValueError("destino_invalido") from exc


def descargar(url, headers, timeout, sesion=None):
    owned = sesion is None
    session = sesion or requests.Session()
    # No usar .netrc ni proxies del entorno con URLs proporcionadas por terceros.
    session.trust_env = False
    started = time.monotonic()
    try:
        host = validar_url(url)
        for redirect in range(MAX_REDIRECTS + 1):
            validar_url(url, host)
            with session.get(url, headers=headers, timeout=(min(timeout, 10), timeout),
                             allow_redirects=False, stream=True) as response:
                code = response.status_code
                result = {"status": code, "etag": response.headers.get("ETag"),
                          "last_modified": response.headers.get("Last-Modified"), "url_final": url}
                if code in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location or redirect == MAX_REDIRECTS:
                        raise ValueError("redireccion_invalida")
                    next_url = urljoin(url, location)
                    if urlsplit(url).scheme == "https" and urlsplit(next_url).scheme != "https":
                        raise ValueError("redireccion_insegura")
                    url = next_url
                    # Validadores del recurso original no describen otro recurso.
                    headers = {k: v for k, v in headers.items() if not k.startswith("If-")}
                    continue
                if code == 304:
                    return "sin_cambio", None, result
                if code != 200:
                    result["error"] = f"HTTP {code}"
                    retry = response.headers.get("Retry-After")
                    if retry:
                        try:
                            seconds = int(retry) if retry.isdigit() else parsedate_to_datetime(retry).timestamp() - time.time()
                            result["retry_after"] = max(0, seconds)
                        except (ValueError, TypeError, OverflowError):
                            pass
                    return "error", None, result
                content_type = response.headers.get("Content-Type", "").lower()
                if not any(t in content_type for t in ("text/html", "application/xhtml+xml")):
                    raise ValueError("contenido_no_html")
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError("respuesta_demasiado_grande")
                    if time.monotonic() - started > timeout * 2:
                        raise ValueError("tiempo_total_excedido")
                    chunks.append(chunk)
                encoding = response.encoding
                if not encoding or encoding.lower() == "iso-8859-1":
                    encoding = "utf-8"
                try:
                    html = b"".join(chunks).decode(encoding, errors="replace")
                except LookupError:
                    html = b"".join(chunks).decode("utf-8", errors="replace")
                return "nuevo", html, result
        raise ValueError("redireccion_invalida")
    finally:
        if owned:
            session.close()
