"""Tests de las dos capas nuevas.

No tocan la red: todo lo que aca se prueba es la logica de decision. Las
mediciones contra la API viva estan anotadas en los docstrings de cada modulo,
que es donde corresponde, porque no se pueden reproducir de forma determinista.
"""

import unittest
from datetime import datetime, timedelta, timezone

import capa_lenta
import capa_rapida
import extractores
import vigilancia


def ts(horas_atras):
    return datetime.now(timezone.utc) - timedelta(hours=horas_atras)


def serie(*tramos):
    """Construye filas de pricing_history desde (horas_atras, precio, disponible)."""
    return [
        {"ts": ts(h), "precio": p, "normal": n, "disponible": d}
        for h, p, d, n in tramos
    ]


class TestNormalizarPrecio(unittest.TestCase):
    def test_formatos_chilenos(self):
        # Los tres formatos que aparecen en las 30 tiendas medidas.
        self.assertEqual(extractores._a_entero("1.629.900"), 1629900)
        self.assertEqual(extractores._a_entero("1629900.00"), 1629900)
        self.assertEqual(extractores._a_entero("1,629,900"), 1629900)
        self.assertEqual(extractores._a_entero("295990"), 295990)

    def test_descarta_centavos_solo_con_dos_digitos(self):
        # ".00" son centavos; ".900" son miles. La diferencia es todo el precio.
        self.assertEqual(extractores._a_entero("52990.00"), 52990)
        self.assertEqual(extractores._a_entero("52.990"), 52990)

    def test_valores_invalidos(self):
        for malo in (None, "", "gratis", "0", "$"):
            self.assertIsNone(extractores._a_entero(malo), malo)


class TestExtraccion(unittest.TestCase):
    def test_ld_json_anidado_en_offers(self):
        html = """<script type="application/ld+json">
        {"@type":"Product","name":"x","offers":{"@type":"Offer","price":"123990"}}
        </script>"""
        precio, metodo, _ = extractores.extraer(html)
        self.assertEqual(precio, 123990)
        self.assertEqual(metodo, extractores.LD_JSON)

    def test_ld_json_malformado_se_rechaza(self):
        # Sin estructura valida no podemos asociar el precio al producto.
        html = '<script type="application/ld+json">{"price": "45990",,}</script>'
        precio, _, _ = extractores.extraer(html)
        self.assertIsNone(precio)

    def test_referencia_no_resuelve_una_oferta_ambigua(self):
        # Una ficha con accesorios: sin referencia se toma el primero y es el
        # producto equivocado. Es el mismo error de emparejar por cercania.
        html = """<script type="application/ld+json">
        {"@graph":[{"price":"9990"},{"price":"249990"}]}</script>"""
        sin_ref, _, _ = extractores.extraer(html)
        con_ref, _, _ = extractores.extraer(html, referencia=250000)
        self.assertIsNone(sin_ref)
        self.assertIsNone(con_ref)

    def test_avisa_cuando_cambia_el_metodo(self):
        html = '<meta itemprop="price" content="75990">'
        precio, metodo, cambio = extractores.extraer(
            html, metodo_esperado=extractores.LD_JSON
        )
        self.assertEqual(precio, 75990)
        self.assertEqual(metodo, extractores.META)
        self.assertTrue(cambio, "un cambio de metodo es senal de que la tienda cambio")


class TestVerificarExtractor(unittest.TestCase):
    HTML = '<script type="application/ld+json">{"price":"59990"}</script>'

    def test_coincide(self):
        clase, precio, _, err = extractores.verificar_extractor(self.HTML, 59990)
        self.assertEqual(clase, extractores.COINCIDE)
        self.assertEqual(precio, 59990)
        self.assertAlmostEqual(err, 0.0)

    def test_diferencia_dentro_del_rango_es_posible_cambio(self):
        # Caso real de Ripley: API $53.990, HTML $59.990. Solotodo remuestrea cada
        # ~4h, asi que puede ser un precio nuevo, no un extractor roto.
        clase, _, _, _ = extractores.verificar_extractor(
            self.HTML, 53990, rango=(50000, 70000)
        )
        self.assertEqual(clase, extractores.POSIBLE_CAMBIO)
        self.assertTrue(extractores.admisible(clase))

    def test_diferencia_fuera_de_rango_es_extractor_roto(self):
        clase, _, _, _ = extractores.verificar_extractor(
            self.HTML, 800000, rango=(750000, 900000)
        )
        self.assertEqual(clase, extractores.EXTRACTOR_ROTO)
        self.assertFalse(extractores.admisible(clase))

    def test_sin_precio_legible(self):
        clase, _, _, _ = extractores.verificar_extractor("<html></html>", 59990)
        self.assertEqual(clase, extractores.SIN_LECTURA)
        self.assertFalse(extractores.admisible(clase))


class TestCapaLenta(unittest.TestCase):
    def test_tramo_agotado_no_pesa_en_la_referencia(self):
        # Un precio publicado sin stock no es un precio al que alguien pudiera
        # comprar. Es la correccion que la capa normalizada no permitia hacer.
        filas = serie((72, 100000, True, None), (48, 40000, False, None),
                      (24, 100000, True, None))
        tramos = capa_lenta._tramos_disponibles(filas)
        self.assertNotIn(40000, [p for p, _ in tramos])

    def test_percentil_pondera_por_tiempo_no_por_muestras(self):
        # 1 muestra sostenida 48h debe pesar mas que 3 muestras de 1h.
        filas = serie((50, 100000, True, None), (2, 80000, True, None),
                      (1.5, 80000, True, None), (1, 80000, True, None))
        tramos = capa_lenta._tramos_disponibles(filas)
        pct = capa_lenta._percentil_ponderado(tramos, 80000)
        self.assertLess(pct, 0.5, "el precio barato duro poco: percentil bajo")

    def test_resumen_exige_muestras_minimas(self):
        self.assertIsNone(capa_lenta.resumen(serie((10, 1000, True, None))))

    def test_resumen_calcula_volatilidad_y_percentil(self):
        filas = serie(*[(100 - i * 5, 100000 + (i % 2) * 20000, True, None)
                        for i in range(12)])
        r = capa_lenta.resumen(filas, precio_actual=100000)
        self.assertIsNotNone(r)
        self.assertGreater(r["volatilidad"], 0.0)
        self.assertLessEqual(r["percentil"], 1.0)
        self.assertEqual(r["niveles"], 2)

    def test_precio_plano_da_volatilidad_cero(self):
        filas = serie(*[(100 - i * 5, 90000, True, None) for i in range(12)])
        r = capa_lenta.resumen(filas, precio_actual=90000)
        self.assertEqual(r["volatilidad"], 0.0)


class TestNormalSostenido(unittest.TestCase):
    def test_normal_nunca_cobrado_es_descuento_inventado(self):
        # SERNAC detecto 142.000 productos con descuento inflado en CyberDay 2025.
        # Con la serie por tienda eso se comprueba en vez de sospecharse.
        filas = serie(*[(100 - i * 5, 50000, True, 120000) for i in range(12)])
        declarado, sostuvo, dias = capa_lenta.normal_sostenido(filas)
        self.assertEqual(declarado, 120000)
        self.assertFalse(sostuvo)
        self.assertEqual(dias, 0.0)

    def test_normal_realmente_cobrado_se_sostiene(self):
        filas = serie(*[(400 - i * 20, 120000, True, 120000) for i in range(15)]
                      + [(2, 50000, True, 120000)])
        declarado, sostuvo, dias = capa_lenta.normal_sostenido(filas)
        self.assertEqual(declarado, 120000)
        self.assertTrue(sostuvo)
        self.assertGreater(dias, 1.0)


class TestCorroborar(unittest.TestCase):
    def test_caida_aislada(self):
        r = {9: {"percentil": 0.02}, 18: {"percentil": 0.8}, 11: {"percentil": 0.9}}
        clase, cayeron, comparadas = capa_lenta.corroborar(r, 9)
        self.assertEqual(clase, "aislada")
        self.assertEqual((cayeron, comparadas), (0, 2))

    def test_campana_coordinada(self):
        r = {9: {"percentil": 0.02}, 18: {"percentil": 0.05}, 11: {"percentil": 0.03}}
        clase, _, _ = capa_lenta.corroborar(r, 9)
        self.assertEqual(clase, "campana")

    def test_sin_otras_tiendas(self):
        clase, _, comparadas = capa_lenta.corroborar({9: {"percentil": 0.01}}, 9)
        self.assertEqual(clase, "sin_comparacion")
        self.assertEqual(comparadas, 0)


class TestVigilancia(unittest.TestCase):
    def base(self, **kw):
        r = {"precio": 200000, "volatilidad": 0.20, "percentil": 0.1, "baseline": 250000}
        r.update(kw)
        return r

    def test_no_parseable_no_se_vigila(self):
        self.assertIsNone(vigilancia.nivel(self.base(), parseable=False))

    def test_bajo_el_piso_de_precio_no_se_vigila(self):
        self.assertIsNone(vigilancia.nivel(self.base(precio=5000)))

    def test_niveles_por_volatilidad(self):
        self.assertEqual(vigilancia.nivel(self.base(volatilidad=0.30)), "alta")
        self.assertEqual(vigilancia.nivel(self.base(volatilidad=0.05)), "media")
        self.assertEqual(vigilancia.nivel(self.base(volatilidad=0.001)), "baja")

    def test_percentil_bajo_puntua_mas(self):
        cerca = vigilancia.puntuar(self.base(percentil=0.02))
        lejos = vigilancia.puntuar(self.base(percentil=0.95))
        self.assertGreater(cerca, lejos)

    def test_cadencia_respeta_la_frecuencia_de_cada_nivel(self):
        # `alta` entra en todos los disparos, sea cual sea su semilla.
        self.assertTrue(vigilancia.toca_ahora("alta", 6, semilla=3))
        self.assertTrue(vigilancia.toca_ahora("alta", 7, semilla=3))
        # `media` entra 1 de cada 6, en el disparo que le toca por semilla.
        self.assertTrue(vigilancia.toca_ahora("media", 9, semilla=3))
        self.assertFalse(vigilancia.toca_ahora("media", 10, semilla=3))
        self.assertFalse(vigilancia.toca_ahora("baja", 10, semilla=3))

    def test_cada_entidad_entra_una_vez_por_ciclo(self):
        for nivel_, cada in vigilancia.CADENCIA.items():
            for semilla in range(cada):
                entradas = [
                    d for d in range(cada) if vigilancia.toca_ahora(nivel_, d, semilla)
                ]
                self.assertEqual(len(entradas), 1, f"{nivel_} semilla {semilla}")

    def test_el_nivel_no_dispara_entero_de_golpe(self):
        # REGRESION. Con `disparo % cada == 0` todo un nivel entraba en el mismo
        # disparo. Con la watchlist real de perfumeria (25 alta / 25 media / 291
        # baja) eso daba un lote de 50 en el disparo 36 con CERO entidades de
        # `baja`: las 291 competian por las ranuras sobrantes, perdian por puntaje
        # y no se consultaban nunca. Un tercio de la watchlist era decorativo.
        wl = [{"nivel": "alta", "puntaje": 1.0, "entity_id": i} for i in range(25)]
        wl += [{"nivel": "media", "puntaje": 0.5, "entity_id": 1000 + i} for i in range(25)]
        wl += [{"nivel": "baja", "puntaje": 0.1, "entity_id": 5000 + i} for i in range(291)]

        vistas = set()
        for disparo in range(1, 37):
            lote = vigilancia.lote_del_disparo(wl, disparo, cupo=50)
            self.assertLessEqual(len(lote), 50)
            vistas |= {c["entity_id"] for c in lote}

        self.assertEqual(len(vistas), len(wl), "ninguna entidad puede quedar sin consultar")

    def test_lote_respeta_el_cupo(self):
        wl = [{"nivel": "alta", "puntaje": i, "entity_id": i} for i in range(80)]
        lote = vigilancia.lote_del_disparo(wl, disparo=0, cupo=50)
        self.assertEqual(len(lote), 50)
        # Se posterga lo menos interesante, no lo primero de la lista.
        self.assertEqual(min(c["puntaje"] for c in lote), 30)

    def test_ranuras_escalan_con_la_cadencia(self):
        # `media` se consulta 1 de cada 6 disparos, asi que admite 6 veces mas
        # entidades sin gastar mas por disparo. Sin esto el escalonamiento no ahorra.
        cand = [{"resumen": self.base(volatilidad=0.05), "parseable": True}
                for _ in range(400)]
        wl = vigilancia.construir_watchlist(cand, cupo=50)
        self.assertEqual(len(wl), int(50 * 0.30 * 6))


class TestCapaRapida(unittest.TestCase):
    def test_huella_ignora_el_cuerpo(self):
        self.assertEqual(capa_rapida.huella(1000, True), capa_rapida.huella(1000, True))
        self.assertNotEqual(capa_rapida.huella(1000, True), capa_rapida.huella(999, True))
        self.assertNotEqual(capa_rapida.huella(1000, True), capa_rapida.huella(1000, False))

    def test_304_no_parsea_y_conserva_el_precio(self):
        from unittest.mock import patch
        item = {"entity_id": 1, "store_id": 9, "url": "https://tienda.cl/p", "precio": 50000,
                "huella": capa_rapida.huella(50000, True), "disponible": True, "moneda": "CLP"}
        with patch("capa_rapida.consultar", return_value=("sin_cambio", None, {})):
            r = capa_rapida.revisar_una(item)
        self.assertEqual(r["estado"], capa_rapida.SIN_CAMBIO)
        self.assertFalse(r["cambio"])
        self.assertEqual(r["precio"], 50000)
        self.assertTrue(r["disponible"])

    def test_precio_igual_no_marca_cambio(self):
        from unittest.mock import patch
        html = '<script type="application/ld+json">{"price":50000,"priceCurrency":"CLP","availability":"https://schema.org/InStock"}</script>'
        item = {"entity_id": 1, "store_id": 9, "url": "https://tienda.cl/p", "precio": 50000,
                "huella": capa_rapida.huella(50000, True)}
        with patch("capa_rapida.consultar", return_value=("nuevo", html, {})):
            r = capa_rapida.revisar_una(item)
        self.assertEqual(r["precio"], 50000)
        self.assertFalse(r["cambio"])

    def test_maqueta_rota_se_reporta_no_se_silencia(self):
        from unittest.mock import patch
        with patch("capa_rapida.consultar", return_value=("nuevo", "<html>sin precio</html>", {})):
            r = capa_rapida.revisar_una({"entity_id": 1, "store_id": 9, "url": "https://tienda.cl/p"})
        self.assertEqual(r["error"], "sin_precio")
        self.assertFalse(r["cambio"])


class TestEstadoRapido(unittest.TestCase):
    """El sidecar guarda watchlist, cabeceras, huellas y contador.

    La watchlist vive aca y NO en el .db por un fallo real en produccion: ver
    el docstring de estado_rapido.py.
    """

    def setUp(self):
        import os
        import tempfile

        self.tmp = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        import os
        import shutil

        os.chdir(self.cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ida_y_vuelta(self):
        import estado_rapido

        wl = [{"entity_id": 12, "store_id": 9, "url": "https://tienda.cl/p", "nivel": "alta", "precio": 5000}]
        ts = estado_rapido.sello()
        estado_rapido.guardar("p", 7, {12: {"etag": "a", "huella": "h"}}, wl, ts)
        e = estado_rapido.cargar("p")

        self.assertEqual(e["disparo"], 7)
        # JSON convierte las claves a texto; los entity_id se usan como enteros.
        self.assertIn(12, e["cache"])
        self.assertEqual(e["cache"][12]["etag"], "a")
        self.assertEqual(len(e["watchlist"]), 1)
        self.assertEqual(e["watchlist"][0]["entity_id"], 12)

    def test_sin_sidecar_arranca_de_cero(self):
        import estado_rapido

        e = estado_rapido.cargar("nada")
        self.assertEqual(e["disparo"], 0)
        self.assertEqual(e["watchlist"], [])

    def test_sidecar_corrupto_no_tumba_la_corrida(self):
        import estado_rapido

        estado_rapido.ruta_para("p").write_text("{roto", encoding="utf-8")
        self.assertEqual(estado_rapido.cargar("p")["disparo"], 0)

    def test_version_vieja_se_descarta(self):
        import json

        import estado_rapido

        # v1 guardaba la watchlist en el .db; su sidecar no sirve.
        estado_rapido.ruta_para("p").write_text(
            json.dumps({"version": 1, "disparo": 5, "cache": {"1": {}}}),
            encoding="utf-8",
        )
        self.assertEqual(estado_rapido.cargar("p")["disparo"], 0)

    def test_vencida_sin_watchlist(self):
        import estado_rapido

        self.assertTrue(estado_rapido.vencida([], None))
        self.assertTrue(estado_rapido.vencida([{"entity_id": 1}], None))

    def test_vencida_por_antiguedad(self):
        from datetime import datetime, timedelta, timezone

        import estado_rapido

        wl = [{"entity_id": 1}]
        fresca = datetime.now(timezone.utc).isoformat()
        vieja = (datetime.now(timezone.utc) - timedelta(hours=13)).isoformat()
        self.assertFalse(estado_rapido.vencida(wl, fresca, horas=12))
        self.assertTrue(estado_rapido.vencida(wl, vieja, horas=12))

    def test_marca_de_tiempo_invalida_cuenta_como_vencida(self):
        import estado_rapido

        self.assertTrue(estado_rapido.vencida([{"entity_id": 1}], "no-es-fecha"))

    def test_podar_descarta_entidades_fuera_de_la_watchlist(self):
        import estado_rapido

        cache = {1: {"huella": "a"}, 2: {"huella": "b"}, 3: {"huella": "c"}}
        self.assertEqual(set(estado_rapido.podar(cache, [1, 3])), {1, 3})

    def test_la_watchlist_sobrevive_al_reset_del_db(self):
        """REGRESION del fallo que la saco del .db.

        En produccion el workflow, ante un push rechazado, hace `git reset --hard`
        y reconstruye el .db con reaplicar.py, que solo sabe de precios. Cuando la
        watchlist vivia ahi se perdia entera en cada conflicto y quedaba
        commiteada VACIA, con el workflow en verde. Verificado en el historial de
        git: la tabla aparecio con 0 filas y nunca tuvo datos.

        El sidecar es independiente del .db, asi que un reset no lo toca.
        """
        import estado_rapido

        wl = [{"entity_id": i, "store_id": 9, "url": "https://tienda.cl/p", "precio": 5000, "nivel": "alta"} for i in range(5)]
        estado_rapido.guardar("p", 3, {}, wl, estado_rapido.sello())

        # Simula el reset: el .db desaparece por completo.
        import os

        for f in os.listdir("."):
            if f.endswith(".db"):
                os.remove(f)

        self.assertEqual(len(estado_rapido.cargar("p")["watchlist"]), 5)

    def test_el_db_ya_no_declara_la_tabla(self):
        import re

        import db

        # La palabra "watchlist" sigue apareciendo en el comentario de
        # store_prices, que es de la capa francotirador y no tiene que ver.
        # Lo que importa es que no haya CREATE TABLE.
        self.assertIsNone(
            re.search(r"CREATE TABLE[^;]*\bwatchlist\b", db.SCHEMA, re.I)
        )

    def test_init_db_dropea_la_tabla_vieja(self):
        import os
        import sqlite3

        import db

        ruta = os.path.join(self.tmp, "viejo.db")
        con = sqlite3.connect(ruta)
        con.execute("CREATE TABLE watchlist (entity_id INTEGER PRIMARY KEY)")
        con.execute("INSERT INTO watchlist VALUES (1)")
        con.commit()
        con.close()

        original = db.DB_PATH
        db.DB_PATH = ruta
        try:
            db.init_db()
        finally:
            db.DB_PATH = original

        con = sqlite3.connect(ruta)
        tablas = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        con.close()
        self.assertNotIn("watchlist", tablas)


class TestRotacion(unittest.TestCase):
    def test_rota_para_no_dejar_la_cola_sin_historia(self):
        import run_capas

        ents = list(range(10))
        primera = run_capas._rotar(ents, disparo=0, tope=4)
        segunda = run_capas._rotar(ents, disparo=1, tope=4)
        self.assertEqual(primera, [0, 1, 2, 3])
        self.assertEqual(segunda, [4, 5, 6, 7])
        self.assertNotEqual(primera, segunda, "sin rotacion la cola nunca entra")

    def test_da_la_vuelta_sin_perder_elementos(self):
        import run_capas

        ents = list(range(10))
        tercera = run_capas._rotar(ents, disparo=2, tope=4)
        self.assertEqual(tercera, [8, 9, 0, 1])
        self.assertEqual(len(tercera), 4)

    def test_sin_recorte_si_cabe_entero(self):
        import run_capas

        self.assertEqual(run_capas._rotar([1, 2], disparo=5, tope=10), [1, 2])


if __name__ == "__main__":
    unittest.main()
