"""Tests de la logica de deteccion y del almacenamiento.

Correr con:  python -m unittest -v
"""

import hashlib
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import db
import detector
from detector import baseline_from_segments, check_anomaly, utcnow, weighted_median

NOW = utcnow()


def seg(price, hours_ago):
    """Tramo que empezo hace `hours_ago` horas."""
    return {
        "price": price,
        "first_seen": (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S"),
        "last_seen": (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S"),
    }


class TestWeightedMedian(unittest.TestCase):
    def test_caso_simple(self):
        self.assertEqual(weighted_median([(100, 1), (200, 1), (300, 1)]), 200)

    def test_el_peso_manda_sobre_el_conteo(self):
        # Tres muestras baratas de 1s no deben ganarle a una cara de 10 dias.
        pares = [(50, 1), (50, 1), (50, 1), (100, 10 * 86400)]
        self.assertEqual(weighted_median(pares), 100)

    def test_sin_datos(self):
        self.assertIsNone(weighted_median([]))
        self.assertIsNone(weighted_median([(100, 0)]))


class TestBaseline(unittest.TestCase):
    def test_tramo_unico_estable(self):
        base, span, minimo = baseline_from_segments([seg(100000, 240)], now=NOW)
        self.assertEqual(base, 100000)
        self.assertAlmostEqual(span, 240, delta=1)
        self.assertEqual(minimo, 100000)

    def test_el_tramo_vigente_se_extiende_hasta_ahora(self):
        # 100k durante 10 dias, luego 50k hace 1h: la mediana ponderada por
        # tiempo debe seguir siendo 100k.
        base, _, _ = baseline_from_segments([seg(100000, 240), seg(50000, 1)], now=NOW)
        self.assertEqual(base, 100000)

    def test_precio_bajo_sostenido_pasa_a_ser_la_norma(self):
        # 30 dias a 100k, y ya lleva 20 dias a 40k -> la referencia baja.
        base, _, _ = baseline_from_segments(
            [seg(100000, 30 * 24), seg(40000, 20 * 24)], now=NOW
        )
        self.assertEqual(base, 40000)

    def test_regresion_producto_estable_mas_viejo_que_la_ventana(self):
        """Un producto que no cambia de precio hace 60 dias SI debe tener base.

        Su `last_seen` queda antiguo por diseno (no se reescribe mientras el
        precio no cambia), asi que filtrar por fecha en SQL lo dejaba fuera y
        se perdia justo la referencia mas solida que existe.
        """
        base, span, _ = baseline_from_segments([seg(100000, 60 * 24)], window_days=30, now=NOW)
        self.assertEqual(base, 100000)
        self.assertAlmostEqual(span, 30 * 24, delta=1)  # recortado a la ventana

    def test_tramo_enteramente_fuera_de_la_ventana_se_ignora(self):
        base, _, _ = baseline_from_segments(
            [seg(999999, 90 * 24), seg(100000, 40 * 24)], window_days=30, now=NOW
        )
        self.assertEqual(base, 100000)

    def test_sin_tramos(self):
        base, span, minimo = baseline_from_segments([], now=NOW)
        self.assertIsNone(base)
        self.assertEqual(span, 0.0)
        self.assertIsNone(minimo)


class TestCheckAnomaly(unittest.TestCase):
    def test_sin_historia_solo_dispara_el_descuento_fuerte_de_tienda(self):
        hist = [seg(100000, 1)]  # 1 hora: no alcanza MIN_SPAN_HOURS
        leve = check_anomaly({"price": 80000, "old_price": 100000, "discount": 20}, hist)
        self.assertIsNone(leve)
        fuerte = check_anomaly({"price": 40000, "old_price": 100000, "discount": 60}, hist)
        self.assertIsNotNone(fuerte)
        self.assertEqual(fuerte["source"], "store")

    def test_con_historia_dispara_por_mediana_propia(self):
        r = check_anomaly({"price": 45000, "old_price": None}, [seg(100000, 240)])
        self.assertIsNotNone(r)
        self.assertEqual(r["source"], "history")
        self.assertAlmostEqual(r["ratio"], 0.45, places=2)
        self.assertIn("mas bajo registrado", r["reason"])

    def test_caida_moderada_no_alerta_con_umbral_por_defecto(self):
        # 25% abajo: dentro del ruido de dispersion entre tiendas.
        self.assertIsNone(check_anomaly({"price": 75000, "old_price": None}, [seg(100000, 240)]))

    def test_umbral_configurable_por_perfil(self):
        prod = {"price": 58000, "old_price": None}  # 42% abajo
        hist = [seg(100000, 240)]
        self.assertIsNone(check_anomaly(prod, hist, alert_max_ratio=0.50))
        self.assertIsNotNone(check_anomaly(prod, hist, alert_max_ratio=0.60))

    def test_el_fallback_de_tienda_nunca_es_mas_laxo_que_050(self):
        # Aunque el perfil permita 0.60, el descuento declarado por la tienda
        # es una senal mas debil y se mantiene exigente.
        prod = {"price": 58000, "old_price": 100000, "discount": 42}
        self.assertIsNone(check_anomaly(prod, [seg(100000, 1)], alert_max_ratio=0.60))

    def test_piso_de_precio(self):
        hist = [seg(15000, 240)]
        self.assertIsNone(check_anomaly({"price": 5000}, hist, min_price=20000))
        self.assertIsNotNone(check_anomaly({"price": 5000}, hist, min_price=0))

    def test_una_falla_corta_no_arrastra_su_propia_referencia(self):
        # Clave: si la caida de 3h moviera la mediana, la alerta se auto-anularia.
        r = check_anomaly({"price": 40000}, [seg(100000, 240), seg(40000, 3)])
        self.assertIsNotNone(r)

    def test_precio_invalido(self):
        self.assertIsNone(check_anomaly({"price": 0}, [seg(100000, 240)]))
        self.assertIsNone(check_anomaly({"price": None}, [seg(100000, 240)]))


class TestDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.path = Path(self.tmp.name)
        self._orig = db.DB_PATH
        db.DB_PATH = self.path
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self._orig
        self.path.unlink(missing_ok=True)

    def _hash(self):
        return hashlib.md5(self.path.read_bytes()).hexdigest()

    def _observar(self, pares):
        with db.get_conn() as conn:
            segs = db.load_segments(conn, [p for p, _ in pares])
            return db.flush_prices(conn, segs, pares)

    def test_primera_observacion_abre_tramo(self):
        sin_cambio, cambios = self._observar([(1, 1000), (2, 2000)])
        self.assertEqual((sin_cambio, cambios), (0, 2))

    def test_precio_igual_no_escribe_nada(self):
        """La propiedad que hace viable commitear el .db a git."""
        self._observar([(1, 1000), (2, 2000)])
        antes = self._hash()
        sin_cambio, cambios = self._observar([(1, 1000), (2, 2000)])
        self.assertEqual((sin_cambio, cambios), (2, 0))
        self.assertEqual(antes, self._hash(), "el archivo cambio sin cambiar precios")

    def test_cambio_cierra_el_tramo_anterior_y_abre_uno(self):
        self._observar([(1, 1000)])
        self._observar([(1, 900)])
        with db.get_conn() as conn:
            segs = db.load_segments(conn, [1])[1]
        self.assertEqual([s["price"] for s in segs], [1000, 900])
        # El tramo cerrado tiene last_seen posterior a su first_seen.
        self.assertGreaterEqual(segs[0]["last_seen"], segs[0]["first_seen"])

    def test_upsert_products_no_reescribe_filas_identicas(self):
        prods = [{"product_id": 1, "name": "A", "category_id": 780}]
        with db.get_conn() as conn:
            db.upsert_products(conn, prods)
        antes = self._hash()
        with db.get_conn() as conn:
            db.upsert_products(conn, prods)
        self.assertEqual(antes, self._hash())

    def test_prune_conserva_el_tramo_vigente_aunque_sea_antiguo(self):
        """Regresion: el tramo vigente tiene last_seen viejo por diseno."""
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO price_history (product_id, price, first_seen, last_seen) "
                "VALUES (1, 5000, datetime('now','-300 days'), datetime('now','-300 days'))"
            )
        with db.get_conn() as conn:
            db.prune_history(conn, keep_days=90)
            segs = db.load_segments(conn, [1])
        self.assertEqual(len(segs.get(1, [])), 1)

    def test_prune_borra_tramos_cerrados_viejos(self):
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO price_history (product_id, price, first_seen, last_seen) "
                "VALUES (1, 5000, datetime('now','-300 days'), datetime('now','-299 days'))"
            )
            conn.execute("INSERT INTO price_history (product_id, price) VALUES (1, 6000)")
        with db.get_conn() as conn:
            db.prune_history(conn, keep_days=90)
            segs = db.load_segments(conn, [1])
        self.assertEqual([s["price"] for s in segs[1]], [6000])

    def test_presupuesto_y_cooldown_de_alertas(self):
        with db.get_conn() as conn:
            db.record_alert(conn, 1, 5000, "test", 9, "http://x")
            db.record_alert(conn, 2, 7000, "test", 9, "http://y")
        with db.get_conn() as conn:
            self.assertEqual(db.count_alerts_since(conn, 24), 2)
            recientes = db.load_recent_alerts(conn, [1, 2, 3])
        self.assertEqual(recientes, {1: 5000, 2: 7000})


class TestMigracion(unittest.TestCase):
    def test_migra_esquema_viejo_colapsando_corridas(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        path = Path(tmp.name)
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL, price INTEGER, checked_at TEXT);
            CREATE TABLE alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER, price INTEGER, reason TEXT, sent_at TEXT);
            """
        )
        # 4 corridas: 1000, 1000, 900, 1000  -> 3 tramos (el 1000 vuelve).
        for i, precio in enumerate([1000, 1000, 900, 1000]):
            conn.execute(
                "INSERT INTO price_history (product_id, price, checked_at) "
                "VALUES (1, ?, datetime('now', ?))",
                (precio, f"-{10 - i} hours"),
            )
        conn.commit()
        conn.close()

        orig = db.DB_PATH
        try:
            db.DB_PATH = path
            db.init_db()
            with db.get_conn() as c:
                segs = db.load_segments(c, [1])[1]
                cols = {r["name"] for r in c.execute("PRAGMA table_info(alerts)")}
        finally:
            db.DB_PATH = orig
            path.unlink(missing_ok=True)

        self.assertEqual([s["price"] for s in segs], [1000, 900, 1000])
        self.assertIn("store_id", cols)
        self.assertIn("url", cols)


if __name__ == "__main__":
    unittest.main(verbosity=2)
