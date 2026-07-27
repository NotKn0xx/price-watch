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


class BaseConDB(unittest.TestCase):
    """Base temporal por test. La repetian seis clases con el mismo cuerpo."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.path = Path(self.tmp.name)
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = self.path
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self._orig_db_path
        self.path.unlink(missing_ok=True)

    def _hash(self):
        """Huella del archivo. Sirve para comprobar la propiedad que hace
        viable commitear el .db: sin cambios de precio no se escribe nada."""
        return hashlib.md5(self.path.read_bytes()).hexdigest()


class TestDB(BaseConDB):
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


class TestPurgaDeCategorias(BaseConDB):
    """El perfil manda: lo que no vigila, no se guarda.

    Reproduce el arrastre real que dejo 1.334 productos de celulares, TV y
    linea blanca dentro de la base de perfumes al dividir el bot en perfiles.
    """

    def setUp(self):
        super().setUp()
        with db.get_conn() as conn:
            db.upsert_products(conn, [
                {"product_id": 1, "name": "Perfume A", "category_id": 780},
                {"product_id": 2, "name": "Perfume B", "category_id": 780},
                {"product_id": 3, "name": "Lavadora",  "category_id": 19},
                {"product_id": 4, "name": "iPhone",    "category_id": 6},
            ])
            db.flush_prices(conn, {}, [(1, 50000), (2, 60000), (3, 300000), (4, 900000)])
            db.record_alert(conn, 3, 150000, "prueba", 9, "http://x")

    def _ids(self, tabla, col="product_id"):
        with db.get_conn() as conn:
            return sorted(r[0] for r in conn.execute(f"SELECT {col} FROM {tabla}"))

    def test_borra_productos_de_categorias_ajenas(self):
        with db.get_conn() as conn:
            n = db.purge_foreign_categories(conn, [780])
        self.assertEqual(n, 2)
        self.assertEqual(self._ids("products"), [1, 2])

    def test_arrastra_su_historial_y_sus_alertas(self):
        with db.get_conn() as conn:
            db.purge_foreign_categories(conn, [780])
        self.assertEqual(self._ids("price_history"), [1, 2])
        self.assertEqual(self._ids("alerts"), [])

    def test_no_toca_las_categorias_vigiladas(self):
        with db.get_conn() as conn:
            db.purge_foreign_categories(conn, [780])
            segs = db.load_segments(conn, [1, 2])
        self.assertEqual([s["price"] for s in segs[1]], [50000])
        self.assertEqual([s["price"] for s in segs[2]], [60000])

    def test_es_idempotente_y_no_reescribe_el_archivo(self):
        """Sin sobrantes no debe tocar el .db: si no, cada corrida commitearia."""
        with db.get_conn() as conn:
            db.purge_foreign_categories(conn, [780])
        antes = hashlib.md5(self.path.read_bytes()).hexdigest()
        with db.get_conn() as conn:
            self.assertEqual(db.purge_foreign_categories(conn, [780]), 0)
        self.assertEqual(antes, hashlib.md5(self.path.read_bytes()).hexdigest())

    def test_perfil_con_varias_categorias_las_respeta_todas(self):
        with db.get_conn() as conn:
            self.assertEqual(db.purge_foreign_categories(conn, [780, 19, 6]), 0)
        self.assertEqual(self._ids("products"), [1, 2, 3, 4])

    def test_lista_vacia_no_borra_nada(self):
        """Un perfil sin CATEGORIES no debe vaciar la base entera."""
        with db.get_conn() as conn:
            self.assertEqual(db.purge_foreign_categories(conn, []), 0)
        self.assertEqual(self._ids("products"), [1, 2, 3, 4])


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


# --- Capa francotirador: historial por tienda ------------------------------
#
# Estos tests cubren lo que despues se afirma en publico. Un falso positivo
# aqui no es un bug: es acusar a una tienda de algo que no hizo.

import francotirador
from francotirador import observaciones_de


def _entidad(store_id, oferta, normal=None, disponible=True, url="http://x"):
    return {
        "store_id": store_id,
        "external_url": url,
        "active_registry": {
            "is_available": disponible,
            "offer_price": oferta,
            "normal_price": normal if normal is not None else oferta,
        },
    }


class TestObservacionesDe(unittest.TestCase):
    def test_convierte_precios_de_texto_a_entero(self):
        """La API devuelve '81990.00', no 81990."""
        filas = observaciones_de([_entidad(9, "81990.00", "99990.00")])
        self.assertEqual(filas[0][1:4], (9, 81990, 99990))

    def test_descarta_no_disponible(self):
        """Un precio de algo agotado no es un precio: mandaria gente a una
        pagina sin stock."""
        self.assertEqual(observaciones_de([_entidad(9, "1000", disponible=False)]), [])

    def test_descarta_sin_registro_activo(self):
        self.assertEqual(observaciones_de([{"store_id": 9, "active_registry": None}]), [])

    def test_descarta_precio_ilegible_o_no_positivo(self):
        for malo in (None, "", "gratis", "0", "-500"):
            with self.subTest(precio=malo):
                self.assertEqual(observaciones_de([_entidad(9, malo)]), [])

    def test_descarta_entidad_sin_tienda(self):
        self.assertEqual(observaciones_de([_entidad(None, "1000")]), [])

    def test_filtra_por_store_ids(self):
        ents = [_entidad(9, "1000"), _entidad(11, "2000"), _entidad(999, "3000")]
        filas = observaciones_de(ents, store_ids={9, 11})
        self.assertEqual(sorted(f[1] for f in filas), [9, 11])

    def test_sin_store_ids_no_filtra(self):
        ents = [_entidad(9, "1000"), _entidad(999, "3000")]
        self.assertEqual(len(observaciones_de(ents)), 2)

    def test_entrada_vacia_o_nula(self):
        self.assertEqual(observaciones_de([]), [])
        self.assertEqual(observaciones_de(None), [])

    def test_normal_price_ausente_no_rompe(self):
        e = _entidad(9, "1000")
        e["active_registry"]["normal_price"] = None
        self.assertEqual(observaciones_de([e])[0][3], None)


class TestStorePrices(BaseConDB):
    def _observar(self, obs):
        with db.get_conn() as conn:
            segs = db.load_store_segments(conn, [o[0] for o in obs])
            return db.flush_store_prices(conn, segs, obs)

    def test_abre_un_tramo_por_tienda(self):
        """Mismo producto en dos tiendas son dos series independientes."""
        sc, ca = self._observar([(1, 9, 1000, 1200, "u"), (1, 11, 1100, 1100, "v")])
        self.assertEqual((sc, ca), (0, 2))

    def test_precio_igual_deja_el_archivo_intacto(self):
        """La propiedad que hace viable commitear el .db a git."""
        self._observar([(1, 9, 1000, 1200, "u")])
        antes = self._hash()
        sc, ca = self._observar([(1, 9, 1000, 1200, "u")])
        self.assertEqual((sc, ca), (1, 0))
        self.assertEqual(self._hash(), antes)

    def test_cambio_en_una_tienda_no_toca_la_otra(self):
        """El nucleo de la corroboracion entre tiendas: si solo una se movio,
        la serie de la otra debe quedar igual."""
        self._observar([(1, 9, 1000, 1200, "u"), (1, 11, 1100, 1100, "v")])
        self._observar([(1, 9, 800, 1200, "u"), (1, 11, 1100, 1100, "v")])
        with db.get_conn() as conn:
            segs = db.load_store_segments(conn, [1])
        self.assertEqual([s["price"] for s in segs[(1, 9)]], [1000, 800])
        self.assertEqual([s["price"] for s in segs[(1, 11)]], [1100])

    def test_la_url_se_refresca_al_cambiar_precio(self):
        """Las tiendas rotan URLs; una url vieja es un link roto en un video
        ya publicado."""
        self._observar([(1, 9, 1000, None, "vieja")])
        self._observar([(1, 9, 900, None, "nueva")])
        with db.get_conn() as conn:
            segs = db.load_store_segments(conn, [1])
        self.assertEqual(segs[(1, 9)][-1]["url"], "nueva")

    def test_prune_conserva_el_tramo_vigente(self):
        """Su last_seen es antiguo por diseno: borrarlo perderia la referencia."""
        self._observar([(1, 9, 1000, None, "u")])
        with db.get_conn() as conn:
            db.prune_store_prices(conn, keep_days=0)
            quedan = conn.execute("SELECT COUNT(*) FROM store_prices").fetchone()[0]
        self.assertEqual(quedan, 1)

    def test_retencion_mas_larga_que_price_history(self):
        """El detector de alzas previas mira 60-90 dias: con los 90 de
        price_history el margen seria cero."""
        import inspect
        d = inspect.signature(db.prune_store_prices).parameters["keep_days"].default
        h = inspect.signature(db.prune_history).parameters["keep_days"].default
        self.assertGreater(d, h)


class TestVigilar(BaseConDB):
    """vigilar() con la red simulada: un producto que falla no debe llevarse
    la corrida ni dejar huecos en el resto de la watchlist."""

    def setUp(self):
        super().setUp()
        self._orig_get = francotirador.get_entities

    def tearDown(self):
        francotirador.get_entities = self._orig_get
        super().tearDown()

    def test_un_producto_que_falla_no_detiene_al_resto(self):
        def falso(product_id, **kw):
            if product_id == 2:
                raise RuntimeError("500 de Solotodo")
            return [_entidad(9, "1000")]

        francotirador.get_entities = falso
        vistos = []
        with db.get_conn() as conn:
            sc, ca, fallidos, obs = francotirador.vigilar(
                conn, [1, 2, 3], demora=0, al_fallar=lambda p, e: vistos.append(p)
            )
        self.assertEqual((ca, fallidos), (2, 1))
        self.assertEqual(vistos, [2])
        # Las observaciones se devuelven para poder reaplicarlas sobre un .db
        # fresco si el rebase conflictua. Ver reaplicar.py.
        self.assertEqual(len(obs), 2)

    def test_watchlist_vacia_no_toca_la_red(self):
        def explota(*a, **k):
            raise AssertionError("no deberia consultarse la API")

        francotirador.get_entities = explota
        with db.get_conn() as conn:
            self.assertEqual(francotirador.vigilar(conn, [], demora=0), (0, 0, 0, []))


class TestElegirWatchlist(BaseConDB):
    """La seleccion no debe premiar el ruido por stock.

    price_history guarda el precio mas barato ENTRE tiendas: si la tienda barata
    se agota, el precio "sube" sin que nadie lo haya cambiado. Contar tramos
    elegiria justo esos productos.
    """

    def _sembrar(self, product_id, precios):
        with db.get_conn() as conn:
            for p in precios:
                conn.execute(
                    "INSERT INTO price_history (product_id, price) VALUES (?, ?)",
                    (product_id, p),
                )

    def _elegir(self, limite=10):
        import vigilar
        with db.get_conn() as conn:
            return vigilar.elegir_watchlist(conn, limite)

    def test_el_que_oscila_entre_dos_precios_pierde(self):
        """Caso real de la base: $102.990 <-> $111.990 cuatro veces. Muchos
        tramos, solo dos niveles: es quiebre de stock, no movimiento."""
        self._sembrar(1, [102990, 111990] * 5)          # 10 tramos, 2 niveles
        self._sembrar(2, [100000, 92000, 85000, 78000])  # 4 tramos, 4 niveles
        self.assertEqual(self._elegir()[0], 2)

    def test_descarta_amplitud_insignificante(self):
        """Bajo 8% entre maximo y minimo no hay nada publicable."""
        self._sembrar(1, [69890, 69900, 69930])
        self.assertNotIn(1, self._elegir())

    def test_descarta_producto_sin_movimiento(self):
        self._sembrar(1, [50000])
        self.assertEqual(self._elegir(), [])

    def test_respeta_el_limite(self):
        for pid in range(1, 8):
            self._sembrar(pid, [100000, 80000, 60000])
        self.assertEqual(len(self._elegir(limite=3)), 3)


class TestTiendaRepetida(unittest.TestCase):
    """La API devuelve varias entidades de la MISMA tienda: son fichas
    distintas del mismo producto en ese comercio. Medido en produccion:
    Falabella publicaba un Cacharel en 3 fichas a $29.990, $45.990 y $39.990.

    Sin colapsarlas, la serie mezcla fichas y parece oscilacion de precio --
    el falso "subio" que este modulo existe para evitar.
    """

    def test_observaciones_de_deja_una_fila_por_tienda(self):
        ents = [_entidad(9, "45990"), _entidad(9, "29990"), _entidad(9, "39990")]
        filas = observaciones_de(ents)
        self.assertEqual(len(filas), 1)

    def test_conserva_la_mas_barata(self):
        """Es lo que pagaria quien compra en esa tienda."""
        ents = [_entidad(9, "45990"), _entidad(9, "29990"), _entidad(9, "39990")]
        self.assertEqual(observaciones_de(ents)[0][2], 29990)

    def test_la_url_es_la_de_la_ficha_barata(self):
        """Hay que mandar a comprar a la barata, no a la cara."""
        ents = [_entidad(9, "45990", url="https://www.falabella.com/cara"),
                _entidad(9, "29990", url="https://www.falabella.com/barata")]
        self.assertEqual(observaciones_de(ents)[0][4], "https://www.falabella.com/barata")

    def test_no_mezcla_tiendas_distintas(self):
        ents = [_entidad(9, "45990"), _entidad(9, "29990"),
                _entidad(11, "21690"), _entidad(11, "29990")]
        filas = sorted(observaciones_de(ents), key=lambda f: f[1])
        self.assertEqual([(f[1], f[2]) for f in filas], [(9, 29990), (11, 21690)])

    def test_ignora_las_agotadas_al_elegir_la_barata(self):
        """Una ficha agotada mas barata no puede ganar: mandaria a una pagina
        sin stock."""
        ents = [_entidad(9, "10000", disponible=False), _entidad(9, "29990")]
        self.assertEqual(observaciones_de(ents)[0][2], 29990)


class TestInvarianteUnTramoPorTienda(BaseConDB):
    """El invariante se defiende en la capa de base, no solo en el llamador:
    es de la tabla, no de quien la usa."""

    def _vigentes(self, product_id, store_id):
        with db.get_conn() as conn:
            return conn.execute(
                "SELECT price FROM store_prices WHERE product_id=? AND store_id=?",
                (product_id, store_id),
            ).fetchall()

    def test_dos_observaciones_de_la_misma_tienda_no_abren_dos_tramos(self):
        with db.get_conn() as conn:
            sc, ca = db.flush_store_prices(conn, {}, [
                (1, 9, 1500, None, "cara"),
                (1, 9, 1000, None, "barata"),
            ])
        self.assertEqual(ca, 1)
        self.assertEqual(len(self._vigentes(1, 9)), 1)

    def test_gana_la_mas_barata_tambien_en_la_capa_de_base(self):
        with db.get_conn() as conn:
            db.flush_store_prices(conn, {}, [
                (1, 9, 1500, None, "cara"),
                (1, 9, 1000, None, "barata"),
            ])
        self.assertEqual(self._vigentes(1, 9)[0]["price"], 1000)

    def test_duplicado_repetido_no_reescribe_el_archivo(self):
        """Si el duplicado abriera tramo en cada corrida, el .db cambiaria
        siempre y el repo creceria sin control."""
        obs = [(1, 9, 1500, None, "cara"), (1, 9, 1000, None, "barata")]
        with db.get_conn() as conn:
            db.flush_store_prices(conn, db.load_store_segments(conn, [1]), obs)
        antes = hashlib.md5(self.path.read_bytes()).hexdigest()
        with db.get_conn() as conn:
            sc, ca = db.flush_store_prices(conn, db.load_store_segments(conn, [1]), obs)
        self.assertEqual((sc, ca), (1, 0))
        self.assertEqual(hashlib.md5(self.path.read_bytes()).hexdigest(), antes)


class TestEmparejamientoConfiable(unittest.TestCase):
    """Guardia contra comparar productos distintos agrupados bajo una misma
    ficha de Solotodo. Sin esto, la corroboracion entre tiendas -- el control
    central contra acusar en falso -- compara peras con manzanas.
    """

    def setUp(self):
        from francotirador import emparejamiento_confiable
        self.ok = emparejamiento_confiable

    def test_precios_parecidos_son_comparables(self):
        """Mediana medida en produccion: 3%."""
        self.assertTrue(self.ok([29990, 30990, 29500]))

    def test_rechaza_el_refill_contra_el_frasco(self):
        """Caso real: Lancome L'Elixir Refill $36.990 vs frasco $132.990."""
        self.assertFalse(self.ok([36990, 132990, 132990]))

    def test_rechaza_tamanos_distintos_bajo_la_misma_ficha(self):
        """Caso real: Montblanc Signature $29.990 vs $64.990."""
        self.assertFalse(self.ok([29990, 64990]))

    def test_una_sola_tienda_no_permite_corroborar(self):
        """Sin segunda opinion no hay corroboracion: publicar seria justo lo
        que queremos evitar."""
        self.assertFalse(self.ok([29990]))

    def test_lista_vacia(self):
        self.assertFalse(self.ok([]))

    def test_en_el_umbral_exacto_se_acepta(self):
        self.assertTrue(self.ok([100, 150], dispersion_maxima=0.50))

    def test_pasado_el_umbral_se_rechaza(self):
        self.assertFalse(self.ok([100, 151], dispersion_maxima=0.50))

    def test_ignora_precios_nulos_o_no_positivos(self):
        self.assertTrue(self.ok([29990, None, 30990, 0]))

    def test_si_al_filtrar_queda_una_sola_no_corrobora(self):
        self.assertFalse(self.ok([29990, None, 0]))


class TestUrlConfiable(unittest.TestCase):
    """La url viene de un tercero y el plan es publicarla con el identificador
    de afiliado pegado. Una url manipulada mandaria lectores a un sitio ajeno
    CON la firma de la marca encima."""

    def setUp(self):
        from francotirador import url_confiable
        self.ok = url_confiable

    def test_dominios_reales_verificados_contra_la_api(self):
        for url, sid in [
            ("https://www.falabella.com/falabella-cl/product/123/x", 9),
            ("https://www.paris.cl/producto-123.html", 11),
            ("https://simple.ripley.cl/producto-123", 18),
        ]:
            with self.subTest(url=url):
                self.assertTrue(self.ok(url, sid))

    def test_dominio_desnudo_sin_subdominio(self):
        self.assertTrue(self.ok("https://falabella.com/x", 9))

    def test_rechaza_dominio_ajeno(self):
        self.assertFalse(self.ok("https://evil.cl/x", 9))

    def test_rechaza_sufijo_enganoso(self):
        """`falabella.com.evil.cl` pasaria una comparacion por sufijo de texto.
        Por eso se compara por etiquetas de dominio."""
        self.assertFalse(self.ok("https://falabella.com.evil.cl/x", 9))

    def test_rechaza_dominio_pegado(self):
        self.assertFalse(self.ok("https://notfalabella.com/x", 9))

    def test_rechaza_la_tienda_equivocada(self):
        """Url de Paris declarada como Falabella."""
        self.assertFalse(self.ok("https://www.paris.cl/x", 9))

    def test_rechaza_http_sin_cifrar(self):
        self.assertFalse(self.ok("http://www.falabella.com/x", 9))

    def test_rechaza_esquemas_peligrosos(self):
        for u in ("javascript:alert(1)", "data:text/html,<script>alert(1)</script>",
                  "file:///etc/passwd"):
            with self.subTest(url=u):
                self.assertFalse(self.ok(u, 9))

    def test_rechaza_credenciales_en_la_url(self):
        """`https://www.falabella.com@evil.cl/` tiene host evil.cl."""
        self.assertFalse(self.ok("https://www.falabella.com@evil.cl/x", 9))

    def test_rechaza_tienda_desconocida(self):
        """Ante lo que no se conoce, no publicar."""
        self.assertFalse(self.ok("https://www.falabella.com/x", 99999))

    def test_rechaza_vacios(self):
        for u in (None, "", "   "):
            with self.subTest(url=u):
                self.assertFalse(self.ok(u, 9))

    def test_observaciones_de_descarta_la_url_pero_conserva_el_precio(self):
        """El precio sigue siendo valido para la serie; lo que no puede
        sobrevivir es el enlace."""
        e = _entidad(9, "29990", url="https://evil.cl/x")
        fila = observaciones_de([e])[0]
        self.assertEqual(fila[2], 29990)
        self.assertIsNone(fila[4])

    def test_observaciones_de_conserva_la_url_legitima(self):
        e = _entidad(9, "29990", url="https://www.falabella.com/x")
        self.assertEqual(observaciones_de([e])[0][4], "https://www.falabella.com/x")


class TestInvarianteUnTramoPorProducto(BaseConDB):
    """Mismo invariante que en store_prices, aplicado al catalogo.

    Hoy browse_category deduplica con su set `emitted`, pero el invariante es
    de la tabla: si entraran dos filas del mismo producto, open_segment_from()
    tomaria un tramo vigente arbitrario.
    """

    def _precios(self, product_id=1):
        with db.get_conn() as conn:
            return [r["price"] for r in conn.execute(
                "SELECT price FROM price_history WHERE product_id=?", (product_id,))]

    def test_producto_repetido_no_abre_dos_tramos(self):
        with db.get_conn() as conn:
            _, cambios = db.flush_prices(conn, {}, [(1, 2000), (1, 1000)])
        self.assertEqual(cambios, 1)
        self.assertEqual(len(self._precios()), 1)

    def test_gana_el_mas_barato(self):
        with db.get_conn() as conn:
            db.flush_prices(conn, {}, [(1, 2000), (1, 1000)])
        self.assertEqual(self._precios(), [1000])

    def test_repetido_estable_no_reescribe_el_archivo(self):
        obs = [(1, 2000), (1, 1000)]
        with db.get_conn() as conn:
            db.flush_prices(conn, db.load_segments(conn, [1]), obs)
        antes = self._hash()
        with db.get_conn() as conn:
            sc, ca = db.flush_prices(conn, db.load_segments(conn, [1]), obs)
        self.assertEqual((sc, ca), (1, 0))
        self.assertEqual(self._hash(), antes)
