"""Casos adversos de precision, red y entrega; sin red ni mensajes reales."""

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import capa_lenta
import capa_rapida
import estado_rapido
import http_tienda
import lectura_producto
import notifier
import run_capas
import run
import db
from types import SimpleNamespace
from db import SCHEMA
from reaplicar import reaplicar_alertas


URL = "https://tienda.cl/producto"


def html_product(price=40000, **extra):
    offer = {"@type": "Offer", "price": price, "priceCurrency": "CLP",
             "availability": "https://schema.org/InStock", **extra}
    return ld({"@type": "Product", "url": URL, "offers": offer})


def ld(node):
    return '<script type="application/ld+json">' + json.dumps(node) + '</script>'


class TestPrecioSeguro(unittest.TestCase):
    def test_no_convierte_signos_cuotas_o_basura_en_precios(self):
        for value in (-100, "-100", True, "12 cuotas de 4990", "NaN", "1e5", "1.20.000",
                      float("inf"), 123.5, "123.50", "9" * 5000):
            with self.subTest(value=str(value)[:30]):
                self.assertIsNone(lectura_producto.precio_clp(value))

    def test_cero_decimal_no_multiplica_por_diez(self):
        self.assertEqual(lectura_producto.precio_clp("52990.0"), 52990)
        self.assertEqual(lectura_producto.precio_clp(52990.0), 52990)
        self.assertEqual(lectura_producto.precio_clp("$ 52.990,00"), 52990)

    def test_descuento_real_gana_sobre_accesorio_cercano_a_historia(self):
        document = html_product(40000) + ld({"@type": "Product", "url": "https://tienda.cl/otro",
            "offers": {"price": 99000, "priceCurrency": "CLP"}})
        from extractores import extraer
        self.assertEqual(extraer(document, referencia=100000, url=URL)[0], 40000)

    def test_envio_no_es_precio_de_producto(self):
        doc = html_product(40000, shippingDetails={"shippingRate": {"price": 2990}})
        self.assertEqual(lectura_producto.leer(doc, URL)["precio"], 40000)

    def test_no_rescata_usd_con_un_precio_meta(self):
        doc = html_product(200, priceCurrency="USD") + '<meta itemprop="price" content="50000">'
        self.assertIsNone(lectura_producto.leer(doc, URL)["precio"])

    def test_stock_agotado_y_desconocido_no_se_inventan(self):
        self.assertIs(lectura_producto.leer(html_product(availability="https://schema.org/OutOfStock"), URL)["disponible"], False)
        self.assertIsNone(lectura_producto.leer(html_product(availability=None), URL)["disponible"])

    def test_no_elige_minimo_de_variantes_o_tarjetas(self):
        for offers in ([{"price": 20000}, {"price": 40000}],
                       {"@type": "AggregateOffer", "lowPrice": 20000, "highPrice": 40000}):
            self.assertIsNone(lectura_producto.leer(ld({"@type": "Product", "offers": offers}))["precio"])

    def test_meta_no_depende_del_orden_de_atributos(self):
        doc = "<meta content='52990' itemprop='price'><meta content='CLP' itemprop='priceCurrency'>"
        self.assertEqual(lectura_producto.leer(doc)["precio"], 52990)

    def test_referencia_mainentity_de_ripley_no_duplica_producto(self):
        product = {"@type": "Product", "@id": URL + "#product",
                   "offers": {"price": 40000, "priceCurrency": "CLP"}}
        doc = ld({"@graph": [{"@type": "WebPage", "mainEntity": {
            "@type": "Product", "@id": URL + "#product"}}, product]})
        self.assertEqual(lectura_producto.leer(doc, URL)["precio"], 40000)

    def test_no_acepta_url_de_otra_variante(self):
        self.assertIsNone(lectura_producto.leer(html_product(), URL + "?variant=2")["precio"])

    def test_no_publica_precio_con_vigencia_expirada(self):
        self.assertIsNone(lectura_producto.leer(html_product(priceValidUntil="2020-01-01"), URL)["precio"])


class Response:
    def __init__(self, status=200, headers=None, data=b"<html></html>"):
        self.status_code = status
        self.headers = {"Content-Type": "text/html", **(headers or {})}
        self.encoding = "utf-8"
        self.data = data
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def iter_content(self, size):
        yield self.data


class TestRedSegura(unittest.TestCase):
    def test_destinos_privados_se_rechazan_antes_de_get(self):
        for ip in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fd00::1"):
            session = Mock()
            with patch("http_tienda.socket.getaddrinfo", return_value=[(None, None, None, None, (ip, 443))]):
                with self.assertRaises(ValueError):
                    http_tienda.descargar(URL, {}, 5, session)
            session.get.assert_not_called()

    def test_no_acepta_credenciales_protocolos_o_puertos_arbitrarios(self):
        for url in ("file:///etc/passwd", "https://user:pass@tienda.cl/p", "https://tienda.cl:8080/p"):
            with self.assertRaises(ValueError):
                http_tienda.validar_url(url)

    @patch("http_tienda.socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 443))])
    def test_redirect_fuera_del_host_no_se_sigue(self, dns):
        response = Response(302, {"Location": "https://otro.cl/p"})
        session = Mock()
        session.get.return_value = response
        with self.assertRaisesRegex(ValueError, "redireccion_otro_host"):
            http_tienda.descargar(URL, {}, 5, session)
        self.assertEqual(session.get.call_count, 1)
        self.assertTrue(response.closed)

    @patch("http_tienda.socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 443))])
    def test_respuesta_grande_se_cierra_y_rechaza(self, dns):
        response = Response(data=b"x" * 100)
        session = Mock()
        session.get.return_value = response
        with patch("http_tienda.MAX_BYTES", 10), self.assertRaisesRegex(ValueError, "demasiado_grande"):
            http_tienda.descargar(URL, {}, 5, session)
        self.assertTrue(response.closed)
        self.assertFalse(session.trust_env)


class TestEntrega(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.addCleanup(self.conn.close)
        self.fila = dict(entity_id=1, product_id=1, store_id=9, url=URL, nombre="Producto",
                         nivel="alta", precio=100000, baseline=100000, p10=80000, minimo=60000)
        self.result = dict(entity_id=1, store_id=9, url=URL, estado="nuevo", precio=40000,
                           disponible=True, moneda="CLP", cambio=True, huella="h", cache={})

    def test_fallo_de_envio_se_reintenta_con_precio_igual_y_304(self):
        cache = {}
        with patch("run_capas.DRY_RUN", False), patch("run_capas.send_alert", side_effect=[False, True]) as send, \
             patch("run_capas.capa_rapida.revisar_lote", side_effect=[[self.result],
                [{**self.result, "estado": "sin_cambio", "cambio": False}]]), \
             patch("reaplicar.guardar_alerta"):
            _, sent, cache = run_capas.correr_rapida(self.conn, 1, cache, [self.fila])
            self.assertEqual(sent, 0)
            _, sent, cache = run_capas.correr_rapida(self.conn, 2, cache, [self.fila])
            self.assertEqual(sent, 1)
            self.assertEqual(send.call_count, 2)

    def test_etag_roto_no_congela_parser(self):
        result = {**self.result, "error": "sin_precio", "cache": {"etag": "roto"}}
        with patch("run_capas.capa_rapida.revisar_lote", return_value=[result]):
            _, _, cache = run_capas.correr_rapida(self.conn, 1, {}, [self.fila])
        self.assertNotIn("etag", cache[1])
        self.assertNotIn("huella", cache[1])
        self.assertGreater(cache[1]["reintentar_despues"], 0)

    def test_no_repite_consultas_durante_espera_tras_bloqueo(self):
        cache = {1: {"url": URL, "reintentar_despues": 99999999999}}
        with patch("run_capas.capa_rapida.revisar_lote", return_value=[]) as scrape:
            stats, _, _ = run_capas.correr_rapida(self.conn, 1, cache, [self.fila])
        scrape.assert_called_once_with([])
        self.assertEqual(stats["en_espera"], 1)

    def test_sin_stock_o_moneda_no_alerta(self):
        for extra in ({"disponible": False}, {"disponible": None}, {"moneda": None}):
            with patch("run_capas.capa_rapida.revisar_lote", return_value=[{**self.result, **extra}]), \
                 patch("run_capas.send_alert") as send:
                run_capas.correr_rapida(self.conn, 1, {}, [self.fila])
                send.assert_not_called()

    def test_cupo_reconsidera_precio_igual(self):
        with patch("run_capas.count_alerts_since", return_value=999), \
             patch("run_capas.capa_rapida.revisar_lote", return_value=[self.result]):
            _, n, cache = run_capas.correr_rapida(self.conn, 1, {}, [self.fila])
            self.assertEqual(n, 0)
        with patch("run_capas.capa_rapida.revisar_lote", return_value=[{**self.result, "cambio": False}]), \
             patch("run_capas.send_alert", return_value=True), patch("run_capas.DRY_RUN", False), \
             patch("reaplicar.guardar_alerta"):
            _, n, _ = run_capas.correr_rapida(self.conn, 2, cache, [self.fila])
            self.assertEqual(n, 1)

    def test_no_duplica_producto_en_un_mismo_lote(self):
        hallazgo = run_capas._evaluar(self.fila, 40000)
        with patch("run_capas.DRY_RUN", False), patch("run_capas.send_alert", return_value=True) as send, \
             patch("reaplicar.guardar_alerta"):
            n, _ = run_capas._alertar(self.conn, [(self.fila, 40000, hallazgo)] * 2, {})
        self.assertEqual(n, 1)
        self.assertEqual(send.call_count, 1)

    def test_piso_precio_tambien_en_rapida(self):
        self.assertIsNone(run_capas._evaluar(self.fila, 1000))

    def test_reaplicar_envios_es_idempotente(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alertas.json"
            path.write_text(json.dumps([[1, 40000, "rebaja", 9, URL, "2026-09-05 12:00:00"]]))
            reaplicar_alertas(self.conn, path)
            reaplicar_alertas(self.conn, path)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM alerts").fetchone()[0], 1)

    def test_fallo_de_capa_guarda_estado_y_sale_con_error(self):
        state = dict(disparo=4, cache={}, watchlist=[self.fila], watchlist_ts=estado_rapido.sello())
        with patch("run_capas.init_db"), patch("run_capas.estado_rapido.cargar", return_value=state), \
             patch("run_capas.get_conn", return_value=contextlib.nullcontext(self.conn)), \
             patch("run_capas.correr_rapida", side_effect=RuntimeError("fallo simulado")), \
             patch("run_capas.estado_rapido.guardar") as save, patch("run_capas.MODO", "rapida"), \
             patch("run_capas._resumen"), patch("run_capas.traceback.print_exc"), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "Fallo de capa rapida"):
                run_capas.run_once()
            save.assert_called_once()


class TestSeguridadYEstadistica(unittest.TestCase):
    def test_telegram_no_revela_token_en_errores(self):
        session = Mock()
        session.post.side_effect = notifier.requests.RequestException("https://api.telegram.org/botSECRETO/sendMessage")
        output = io.StringIO()
        with patch("notifier._get_session", return_value=session), patch("notifier.TELEGRAM_TOKEN", "SECRETO"), \
             patch("notifier.TELEGRAM_CHAT_ID", "1"), contextlib.redirect_stdout(output):
            self.assertFalse(notifier.send_alert("test"))
        self.assertNotIn("SECRETO", output.getvalue())

    def test_telegram_exige_ok_del_api(self):
        session = Mock()
        session.post.return_value.ok = True
        session.post.return_value.json.return_value = {"ok": False}
        with patch("notifier._get_session", return_value=session), patch("notifier.TELEGRAM_TOKEN", "fake"), \
             patch("notifier.TELEGRAM_CHAT_ID", "1"):
            self.assertFalse(notifier.send_alert("test"))

    def test_percentil_cero_cuenta_como_caida(self):
        self.assertEqual(capa_lenta.corroborar({1: {"percentil": 0}, 2: {"percentil": 0}}, 1)[0], "campana")

    def test_agotado_no_alarga_normal_sostenido(self):
        now = datetime.now(timezone.utc)
        rows = [{"ts": now - timedelta(hours=48), "precio": 100, "normal": 100, "disponible": True},
                {"ts": now - timedelta(hours=47), "precio": 100, "normal": 100, "disponible": False},
                {"ts": now - timedelta(minutes=5), "precio": 50, "normal": 100, "disponible": True}]
        _, sustained, days = capa_lenta.normal_sostenido(rows)
        self.assertFalse(sustained)
        self.assertAlmostEqual(days, 1 / 24)

    def test_sidecar_json_valido_con_estructura_invalida_se_descarta(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            for data in ([], {"version": estado_rapido.VERSION, "disparo": "hola"},
                         {"version": estado_rapido.VERSION, "cache": {"invalido": {}}}):
                path.write_text(json.dumps(data))
                with patch("estado_rapido.ruta_para", return_value=path):
                    self.assertEqual(estado_rapido.cargar("p")["disparo"], 0)


class TestPrecioDelEnlace(unittest.TestCase):
    def run_scan(self, real_price):
        with tempfile.TemporaryDirectory() as directory, patch("db.DB_PATH", Path(directory) / "test.db"):
            db.init_db()
            with db.get_conn() as conn:
                conn.execute("INSERT INTO price_history(product_id, price, first_seen, last_seen) "
                             "VALUES(1,100000,datetime('now','-10 days'),datetime('now','-10 days'))")
            profile = SimpleNamespace(CATEGORIES=[{"id": 780, "name": "Test"}], STORE_IDS={9: "Test"})
            product = dict(product_id=1, category_id=780, name="Producto", price=40000, old_price=None)
            entity = dict(store_id=9, external_url=URL,
                          active_registry=dict(offer_price=str(real_price), normal_price="100000", is_available=True))
            with patch("run.profile", profile), patch("run.browse_category", return_value=[product]), \
                 patch("run.best_entity_for_alert", return_value=entity), patch("run.DRY_RUN", False), \
                 patch("run.send_alert", return_value=True) as send, patch("run.guardar"), \
                 patch("reaplicar.guardar_alerta"), patch("run._write_summary"), contextlib.redirect_stdout(io.StringIO()):
                count = run.run_once()
            with db.get_conn() as conn:
                stored = conn.execute("SELECT price FROM alerts").fetchall()
            return count, send.call_args, [row[0] for row in stored]

    def test_no_envia_rebaja_que_no_existe_en_la_entidad(self):
        count, message, stored = self.run_scan(90000)
        self.assertEqual((count, message, stored), (0, None, []))

    def test_mensaje_y_cooldown_usan_precio_real_del_enlace(self):
        count, message, stored = self.run_scan(35000)
        self.assertEqual(count, 1)
        self.assertIn("$35.000", message.args[0])
        self.assertNotIn("$40.000", message.args[0])
        self.assertEqual(stored, [35000])


if __name__ == "__main__":
    unittest.main()
