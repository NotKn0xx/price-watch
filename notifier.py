import os

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

_session = None


def _get_session():
    global _session
    if _session is None:
        s = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=2,  # 0s, 2s, 4s
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("POST",),
            raise_on_status=False,
        )
        s.mount("https://", HTTPAdapter(max_retries=retry))
        _session = s
    return _session


def send_alert(text: str) -> bool:
    """Envia la alerta. Devuelve True si salio, False si no.

    No lanza excepciones a proposito: una caida de Telegram no puede abortar
    la corrida ni hacernos perder el historial de precios recolectado.
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[ALERTA - Telegram no configurado]\n" + text)
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        resp = _get_session().post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "disable_web_page_preview": False,
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        print(f"  [Telegram] fallo de red: {exc}")
        return False

    if not resp.ok:
        # El cuerpo trae el motivo real (chat_id malo, bot bloqueado, etc.).
        print(f"  [Telegram] HTTP {resp.status_code}: {resp.text[:200]}")
        return False
    return True
