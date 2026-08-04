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

    def test_ld_json_malformado_cae_a_lectura_plana(self):
        # ld+json roto es comun; no debe costar la lectura.
        html = '<script type="application/ld+json">{"price": "45990",,}</script>'
        precio, _, _ = extractores.extraer(html)
        self.assertEqual(precio, 45990)

    def test_referencia_elige_el_candidato_correcto(self):
        # Una ficha con accesorios: sin referencia se toma el primero y es el
        # producto equivocado. Es el mismo error de emparejar por cercania.
        html = """<script type="application/ld+json">
        {"@graph":[{"price":"9990"},{"price":"249990"}]}</script>"""
        sin_ref, _, _ = extractores.extraer(html)
        con_ref, _, _ = extractores.extraer(html, referencia=250000)
        self.assertEqual(sin_ref, 9990)
        self.assertEqual(con_ref, 249990)

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
        # Medido: dos peticiones identicas devuelven cuerpos distintos (tokens de
        # sesion). La unica huella estable es la del dato ya extraido.
        self.assertEqual(capa_rapida.huella(1000, True), capa_rapida.huella(1000, True))
        self.assertNotEqual(capa_rapida.huella(1000, True), capa_rapida.huella(999, True))
        self.assertNotEqual(capa_rapida.huella(1000, True), capa_rapida.huella(1000, False))

    def test_304_no_parsea_y_conserva_el_precio(self):
        class Resp:
            status_code = 304
            headers = {"Last-Modified": "Mon, 04 Aug 2026 10:00:00 GMT"}

        class Sesion:
            def get(self, *a, **k):
                return Resp()

        item = {"entity_id": 1, "store_id": 9, "url": "http://x", "precio": 50000,
                "huella": capa_rapida.huella(50000, True),
                "cache": {"last_modified": "Mon, 04 Aug 2026 09:00:00 GMT"}}
        r = capa_rapida.revisar_una(item, sesion=Sesion())
        self.assertEqual(r["estado"], capa_rapida.SIN_CAMBIO)
        self.assertFalse(r["cambio"])
        self.assertEqual(r["precio"], 50000)

    def test_precio_igual_no_marca_cambio(self):
        class Resp:
            status_code = 200
            headers = {}
            text = '<script type="application/ld+json">{"price":"50000"}</script>'

        class Sesion:
            def get(self, *a, **k):
                return Resp()

        item = {"entity_id": 1, "store_id": 9, "url": "http://x", "precio": 50000,
                "huella": capa_rapida.huella(50000, True)}
        r = capa_rapida.revisar_una(item, sesion=Sesion())
        self.assertEqual(r["precio"], 50000)
        self.assertFalse(r["cambio"], "sin cambio real no debe gatillar trabajo aguas abajo")

    def test_maqueta_rota_se_reporta_no_se_silencia(self):
        class Resp:
            status_code = 200
            headers = {}
            text = "<html>sin precio</html>"

        class Sesion:
            def get(self, *a, **k):
                return Resp()

        r = capa_rapida.revisar_una(
            {"entity_id": 1, "store_id": 9, "url": "http://x", "precio": 50000},
            sesion=Sesion(),
        )
        self.assertEqual(r["error"], "sin_precio")
        self.assertFalse(r["cambio"])


class TestWatchlistDB(unittest.TestCase):
    """Persistencia de la watchlist. El .db va en git, asi que lo que se prueba
    aca no es solo que guarde bien: es que NO escriba cuando nada cambio."""

    def setUp(self):
        import os
        import tempfile

        import db

        self.tmp = tempfile.mkdtemp()
        self._original = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp, "t.db")
        self.db = db
        db.init_db()

    def tearDown(self):
        import shutil

        self.db.DB_PATH = self._original
        shutil.rmtree(self.tmp, ignore_errors=True)

    def entrada(self, eid=1, **kw):
        base = {
            "entity_id": eid, "store_id": 9, "product_id": 100, "category_id": 780,
            "nombre": "x", "url": "http://x", "metodo": "ld+json", "nivel": "alta",
            "puntaje": 1.0, "precio": 50000, "baseline": 100000,
            "p10": 80000, "p90": 120000, "percentil": 0.1, "volatilidad": 0.2,
        }
        base.update(kw)
        return base

    def test_alta_y_luego_sin_cambios_no_escribe(self):
        with self.db.get_conn() as conn:
            altas, cambios, bajas = self.db.guardar_watchlist(conn, [self.entrada()])
            self.assertEqual((altas, cambios, bajas), (1, 0, 0))
        with self.db.get_conn() as conn:
            altas, cambios, bajas = self.db.guardar_watchlist(conn, [self.entrada()])
            self.assertEqual((altas, cambios, bajas), (0, 0, 0), "reescribir igual ensucia el .db")

    def test_flotantes_se_redondean_para_no_ensuciar_git(self):
        with self.db.get_conn() as conn:
            self.db.guardar_watchlist(conn, [self.entrada(percentil=0.1)])
        with self.db.get_conn() as conn:
            # Ruido en el cuarto decimal no es un cambio real.
            _, cambios, _ = self.db.guardar_watchlist(conn, [self.entrada(percentil=0.10004)])
            self.assertEqual(cambios, 0)

    def test_cambio_real_si_se_escribe(self):
        with self.db.get_conn() as conn:
            self.db.guardar_watchlist(conn, [self.entrada()])
        with self.db.get_conn() as conn:
            _, cambios, _ = self.db.guardar_watchlist(conn, [self.entrada(nivel="baja")])
            self.assertEqual(cambios, 1)

    def test_bajas_solo_en_las_categorias_barridas(self):
        with self.db.get_conn() as conn:
            self.db.guardar_watchlist(
                conn,
                [self.entrada(1, category_id=780), self.entrada(2, category_id=2)],
            )
        with self.db.get_conn() as conn:
            # Se barre solo la 780: la entidad de la categoria 2 debe sobrevivir.
            _, _, bajas = self.db.guardar_watchlist(conn, [self.entrada(1)], [780])
            self.assertEqual(bajas, 0)
            self.assertEqual(len(self.db.cargar_watchlist(conn)), 2)

    def test_baja_cuando_desaparece_de_su_categoria(self):
        with self.db.get_conn() as conn:
            self.db.guardar_watchlist(conn, [self.entrada(1), self.entrada(2)])
        with self.db.get_conn() as conn:
            _, _, bajas = self.db.guardar_watchlist(conn, [self.entrada(1)], [780])
            self.assertEqual(bajas, 1)

    def test_actualizar_precio_solo_si_cambio(self):
        with self.db.get_conn() as conn:
            self.db.guardar_watchlist(conn, [self.entrada(precio=50000)])
            self.assertFalse(self.db.actualizar_precio_watchlist(conn, 1, 50000))
            self.assertTrue(self.db.actualizar_precio_watchlist(conn, 1, 44000))

    def test_watchlist_vacia_esta_vencida(self):
        with self.db.get_conn() as conn:
            self.assertTrue(self.db.watchlist_vencida(conn))
            self.db.guardar_watchlist(conn, [self.entrada()])
            self.assertFalse(self.db.watchlist_vencida(conn, horas=12))


class TestEstadoRapido(unittest.TestCase):
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

    def test_ida_y_vuelta_con_claves_enteras(self):
        import estado_rapido

        estado_rapido.guardar("p", 7, {12: {"etag": "a", "huella": "h"}})
        disparo, cache = estado_rapido.cargar("p")
        self.assertEqual(disparo, 7)
        # JSON convierte las claves a texto; los entity_id se usan como enteros.
        self.assertIn(12, cache)
        self.assertEqual(cache[12]["etag"], "a")

    def test_sin_sidecar_arranca_de_cero(self):
        import estado_rapido

        self.assertEqual(estado_rapido.cargar("nada"), (0, {}))

    def test_sidecar_corrupto_no_tumba_la_corrida(self):
        import estado_rapido

        estado_rapido.ruta_para("p").write_text("{roto", encoding="utf-8")
        self.assertEqual(estado_rapido.cargar("p"), (0, {}))

    def test_version_distinta_se_descarta(self):
        import json

        import estado_rapido

        estado_rapido.ruta_para("p").write_text(
            json.dumps({"version": 999, "disparo": 5, "cache": {"1": {}}}),
            encoding="utf-8",
        )
        self.assertEqual(estado_rapido.cargar("p"), (0, {}))

    def test_podar_descarta_entidades_fuera_de_la_watchlist(self):
        import estado_rapido

        cache = {1: {"huella": "a"}, 2: {"huella": "b"}, 3: {"huella": "c"}}
        self.assertEqual(set(estado_rapido.podar(cache, [1, 3])), {1, 3})


class TestEvaluar(unittest.TestCase):
    """Las dos puertas de la deteccion en la capa rapida."""

    def fila(self, **kw):
        f = {"baseline": 159900, "p10": 91900, "p90": 159900, "minimo": 91900}
        f.update(kw)
        return f

    def _con(self, **cfg):
        """Ejecuta _evaluar con una configuracion de perfil sustituida."""
        import run_capas

        original = run_capas._cfg
        run_capas._cfg = lambda n, d: cfg.get(n, d)
        try:
            return run_capas._evaluar
        finally:
            self.addCleanup(lambda: setattr(run_capas, "_cfg", original))

    def test_caida_real_pasa(self):
        # Bella Soleil: baseline 109.990, p10 76.990, precio 54.990, percentil 0,007.
        ev = self._con(ALERT_MAX_RATIO_RAPIDA=0.70, PUERTA_RAREZA="p10")
        h = ev(self.fila(baseline=109990, p10=76990, minimo=65990), 54990)
        self.assertIsNotNone(h)
        self.assertEqual(h["caida"], 50)
        self.assertTrue(h["bajo_p10"])
        self.assertTrue(h["bajo_minimo"])

    def test_ciclo_promocional_recurrente_no_alerta(self):
        # REGRESION. AMD Ryzen 5 4500: -43% contra la mediana cruza el umbral de
        # ratio, pero $91.900 es su p10 exacto y aparece en 17 de 61 muestras
        # (percentil 0,257). Es el ciclo, no un hallazgo.
        ev = self._con(ALERT_MAX_RATIO_RAPIDA=0.70, PUERTA_RAREZA="p10")
        self.assertIsNone(ev(self.fila(), 91900))

    def test_puerta_minimo_es_mas_estricta_que_p10(self):
        # El backtest mostro que en hardware p10 no discrimina (0% de utiles en
        # todo umbral) porque el ciclo baja tan seguido que su p10 cae dentro del
        # ciclo. Un precio bajo el p10 pero sobre el minimo pasa una puerta y no
        # la otra.
        fila = self.fila(p10=100000, minimo=80000)
        self.assertIsNotNone(
            self._con(ALERT_MAX_RATIO_RAPIDA=0.90, PUERTA_RAREZA="p10")(fila, 90000)
        )
        self.assertIsNone(
            self._con(ALERT_MAX_RATIO_RAPIDA=0.90, PUERTA_RAREZA="minimo")(fila, 90000)
        )

    def test_puerta_ninguna_deja_pasar_el_ciclo(self):
        ev = self._con(ALERT_MAX_RATIO_RAPIDA=0.70, PUERTA_RAREZA="ninguna")
        self.assertIsNotNone(ev(self.fila(), 91900))

    def test_puerta_desconocida_falla_ruidosamente(self):
        ev = self._con(ALERT_MAX_RATIO_RAPIDA=0.90, PUERTA_RAREZA="p11")
        with self.assertRaises(ValueError):
            ev(self.fila(), 40000)

    def test_ratio_rapida_no_hereda_el_de_run_py(self):
        # ALERT_MAX_RATIO=0.50 es de run.py, que mira la capa normalizada y
        # necesita un umbral duro. La rapida usa el suyo.
        fila = self.fila(baseline=100000, p10=80000, minimo=80000)
        self.assertIsNone(
            self._con(ALERT_MAX_RATIO=0.50, PUERTA_RAREZA="p10")(fila, 65000)
        )
        self.assertIsNotNone(
            self._con(ALERT_MAX_RATIO=0.50, ALERT_MAX_RATIO_RAPIDA=0.70,
                      PUERTA_RAREZA="p10")(fila, 65000)
        )

    def test_sobre_el_umbral_de_ratio_no_alerta(self):
        ev = self._con(ALERT_MAX_RATIO_RAPIDA=0.70, PUERTA_RAREZA="p10")
        self.assertIsNone(ev(self.fila(), 120000))

    def test_sin_referencia_de_rareza_no_alerta(self):
        ev = self._con(ALERT_MAX_RATIO_RAPIDA=0.70, PUERTA_RAREZA="p10")
        self.assertIsNone(ev(self.fila(p10=None), 40000))
        ev2 = self._con(ALERT_MAX_RATIO_RAPIDA=0.90, PUERTA_RAREZA="minimo")
        self.assertIsNone(ev2(self.fila(minimo=None), 40000))

    def test_sin_baseline_no_evalua(self):
        ev = self._con(ALERT_MAX_RATIO_RAPIDA=0.70, PUERTA_RAREZA="p10")
        self.assertIsNone(ev(self.fila(baseline=None), 40000))


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
