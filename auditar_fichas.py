"""Muestra pequena de fichas reales. Solo lectura, sin Telegram ni escrituras DB.

python auditar_fichas.py --max 6 --output auditoria-local/fichas.json
La comparacion con SQLite mide discrepancia, no exactitud: puede estar atrasado.
"""

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from capa_rapida import consultar
from extractores import es_parseable, extraer_detalle
from lectura_producto import Documento, _nodes, _types


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max", type=int, default=6)
    parser.add_argument("--store", type=int)
    parser.add_argument("--output", type=Path, default=Path("auditoria-local/fichas.json"))
    args = parser.parse_args()
    results, seen = [], set()
    for path in sorted(Path(__file__).parent.glob("price_watch_*.db")):
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            rows = conn.execute("SELECT store_id, url, price FROM store_prices WHERE id IN "
                                "(SELECT MAX(id) FROM store_prices GROUP BY store_id) ORDER BY store_id").fetchall()
        for store_id, url, historical in rows:
            if args.store and args.store != store_id:
                continue
            if not es_parseable(store_id) or not url or (store_id, path.name) in seen:
                continue
            if len(results) >= min(max(args.max, 1), 20):
                break
            seen.add((store_id, path.name))
            status, html, headers = consultar(url)
            reading = extraer_detalle(html, headers.get("url_final") or url) if html else {}
            row = dict(db=path.name, store_id=store_id, url=url, precio_sqlite=historical,
                       estado=status, http=headers.get("status"), error_http=headers.get("error"), **reading)
            if html and reading.get("error") == "producto_ambiguo":
                row["productos_declarados"] = []
                for block in Documento(html).blocks:
                    try:
                        for node in _nodes(json.loads(block)):
                            if "Product" in _types(node):
                                row["productos_declarados"].append({k: node.get(k) for k in
                                    ("@id", "url", "name", "sku", "offers")})
                    except (ValueError, TypeError):
                        pass
            results.append(row)
            print(json.dumps(row, ensure_ascii=True))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"fecha": datetime.now(timezone.utc).isoformat(),
        "nota": "Muestra de diagnostico, no estimacion de precision.", "fichas": results},
        ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
