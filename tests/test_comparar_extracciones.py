# -*- coding: utf-8 -*-
"""
Tests de comparar_extracciones.py con datos sintéticos (offline).
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from comparar_extracciones import (comparar, informe_texto, main,
                                   _norm_campo, _num)


def _pieza(tipo, largo, ancho, altura=None, zona=None, area=None,
           vertices=None):
    p = {"tipo": tipo, "material_rol": "encimera",
         "largo_mm": largo, "ancho_mm": ancho}
    if altura is not None:
        p["altura_mm"] = altura
    if zona is not None:
        p["zona"] = zona
    if area is not None:
        p["area_m2"] = area
    if vertices is not None:
        p["vertices_mm"] = vertices
    return p


def _validada_base():
    return {
        "job_id": "J0001_TEST",
        "cliente": "Cliente Ficticio",
        "vendedor": "Vendedor Ficticio",
        "direccion": "Calle Falsa 123",
        "ciudad": "Narnia",
        "materiales": [
            {"rol": "encimera", "marca": "Silestone", "color": "Blanco Zeus",
             "grosor_cm": 2.0, "acabado": "Pulido"},
        ],
        "piezas": [
            _pieza("encimera", 2400, 600, zona="A", area=1.44,
                   vertices=[[0, 0], [2400, 0], [2400, 600], [0, 600]]),
            _pieza("frontal", 2400, 600, altura=60, zona="A"),
        ],
        "huecos": [
            {"tipo": "placa", "cantidad": 2, "pieza_zona": "A",
             "centro_x_mm": 400, "centro_y_mm": 300,
             "largo_mm": 300, "ancho_mm": 200},
            {"tipo": "fregadero", "cantidad": 1},
        ],
        "cantos": [{"tipo": "recto_pulido", "longitud_ml": 6.0}],
        "advertencias": ["aviso1"],
        "confianza": "alta",
    }


def _cruda_base():
    return {
        "job_id": "J0001_TEST",
        "cliente": "Cliente Ficticio",
        "piezas": [
            _pieza("encimera", 2400, 600, zona="A", area=1.44,
                   vertices=[[0, 0], [2400, 0], [2400, 600], [0, 600]]),
            _pieza("frontal", 2400, 600, altura=60, zona="A"),
        ],
        "huecos": [
            {"tipo": "placa", "cantidad": 2, "pieza_zona": "A",
             "centro_x_mm": 405, "centro_y_mm": 300,
             "largo_mm": 300, "ancho_mm": 200},
            {"tipo": "fregadero", "cantidad": 1},
        ],
        "cantos": [{"tipo": "recto_pulido", "longitud_ml": 6.0}],
        "advertencias": [],
        "confianza": "media",
    }


class TestPiezas(unittest.TestCase):
    def test_match_exacto(self):
        r = comparar(_validada_base(), _cruda_base())
        self.assertEqual(len(r["piezas"]["exactas"]), 2)
        self.assertEqual(r["piezas"]["aproximadas"], [])
        self.assertEqual(r["piezas"]["faltantes"], [])
        self.assertEqual(r["piezas"]["sobrantes"], [])
        self.assertAlmostEqual(r["score"]["piezas"], 1.0)

    def test_dims_sin_orden_600x2400(self):
        """Una 600x2400 debe casar con una 2400x600 (par sin orden)."""
        v = {"piezas": [_pieza("encimera", 600, 2400)]}
        c = {"piezas": [_pieza("encimera", 2400, 600)]}
        r = comparar(v, c)
        self.assertEqual(len(r["piezas"]["exactas"]), 1)
        self.assertEqual(len(r["piezas"]["aproximadas"]), 0)

    def test_pieza_faltante_y_sobrante(self):
        v = {"piezas": [_pieza("encimera", 2000, 600),
                        _pieza("copete", 2000, 80, altura=6)]}
        c = {"piezas": [_pieza("encimera", 2000, 600),
                        _pieza("zocalo", 2000, 120, altura=12)]}
        r = comparar(v, c)
        self.assertEqual(len(r["piezas"]["exactas"]), 1)
        self.assertEqual([p["tipo"] for p in r["piezas"]["faltantes"]],
                         ["copete"])
        self.assertEqual([p["tipo"] for p in r["piezas"]["sobrantes"]],
                         ["zocalo"])
        cats = {d["categoria"] for d in r["discrepancias"]}
        self.assertIn("pieza_faltante", cats)
        self.assertIn("pieza_sobrante", cats)
        self.assertLess(r["score"]["piezas"], 1.0)

    def test_tolerancia_limite(self):
        """Con tol_mm=20 y ref 500 (3%=15<20): delta 20 → aproximada;
        delta 21 → no match."""
        v = {"piezas": [_pieza("encimera", 500, 600)]}
        dentro = comparar(v, {"piezas": [_pieza("encimera", 520, 600)]})
        self.assertEqual(len(dentro["piezas"]["aproximadas"]), 1)
        self.assertEqual(dentro["piezas"]["aproximadas"][0]["deltas"],
                         {"largo_mm": 20.0})
        fuera = comparar(v, {"piezas": [_pieza("encimera", 521, 600)]})
        self.assertEqual(fuera["piezas"]["aproximadas"], [])
        self.assertEqual(len(fuera["piezas"]["faltantes"]), 1)
        self.assertEqual(len(fuera["piezas"]["sobrantes"]), 1)

    def test_tolerancia_pct_aplica(self):
        """En cotas grandes manda el 3%: 60 mm de 2000 siguen dentro."""
        v = {"piezas": [_pieza("encimera", 2000, 600)]}
        r = comparar(v, {"piezas": [_pieza("encimera", 2060, 600)]})
        self.assertEqual(len(r["piezas"]["aproximadas"]), 1)
        r2 = comparar(v, {"piezas": [_pieza("encimera", 2061, 600)]})
        self.assertEqual(r2["piezas"]["aproximadas"], [])

    def test_area_y_vertices_por_tipo(self):
        r = comparar(_validada_base(), _cruda_base())
        e = r["piezas"]["por_tipo"]["encimera"]
        self.assertAlmostEqual(e["area_m2_validada"], 1.44)
        self.assertEqual(e["vertices_validada"], 4)
        self.assertEqual(e["vertices_cruda"], 4)


class TestHuecosCantos(unittest.TestCase):
    def test_huecos_conteo_por_tipo(self):
        v = _validada_base()
        c = _cruda_base()
        c["huecos"] = [{"tipo": "placa", "cantidad": 1},
                       {"tipo": "fregadero", "cantidad": 1},
                       {"tipo": "grifo", "cantidad": 1}]
        r = comparar(v, c)
        self.assertEqual(r["huecos"]["placa"]["delta"], -1)
        self.assertEqual(r["huecos"]["grifo"]["delta"], 1)
        self.assertEqual(r["huecos"]["fregadero"]["delta"], 0)
        cats = [d["categoria"] for d in r["discrepancias"]]
        self.assertIn("hueco_cantidad", cats)

    def test_hueco_posicion_y_dims(self):
        v = {"huecos": [{"tipo": "placa", "cantidad": 1, "centro_x_mm": 400,
                         "centro_y_mm": 300, "largo_mm": 300,
                         "ancho_mm": 200}]}
        c = {"huecos": [{"tipo": "placa", "cantidad": 1, "centro_x_mm": 500,
                         "centro_y_mm": 300, "largo_mm": 325,
                         "ancho_mm": 200}]}
        r = comparar(v, c)
        par = r["huecos"]["placa"]["emparejados"][0]
        self.assertEqual(par["delta_centro_x_mm"], 100.0)
        self.assertEqual(par["delta_largo_mm"], 25.0)
        cats = {d["categoria"] for d in r["discrepancias"]}
        self.assertIn("hueco_posicion", cats)
        self.assertIn("hueco_dim", cats)

    def test_cantos_delta(self):
        v = {"cantos": [{"tipo": "recto_pulido", "longitud_ml": 6.0}]}
        c = {"cantos": [{"tipo": "recto_pulido", "longitud_ml": 4.5}]}
        r = comparar(v, c)
        e = r["cantos"]["recto_pulido"]
        self.assertAlmostEqual(e["delta_ml"], -1.5)
        self.assertAlmostEqual(e["delta_rel"], 0.25)
        self.assertIn("canto_ml",
                      {d["categoria"] for d in r["discrepancias"]})
        self.assertLess(r["score"]["cantos"], 1.0)

    def test_tipo_desconocido_va_a_otros(self):
        v = {"huecos": [{"tipo": "dosificador", "cantidad": 2}]}
        c = {"huecos": [{"tipo": "dosificador", "cantidad": 1}]}
        r = comparar(v, c)
        self.assertEqual(r["huecos"]["otros"]["delta"], -1)


class TestMaterialesYRobustez(unittest.TestCase):
    def test_normalizacion_numerica(self):
        self.assertEqual(_norm_campo("1,2"), 1.2)
        self.assertEqual(_norm_campo("2,0"), 2.0)
        self.assertEqual(_num("1,2"), 1.2)

    def test_normalizacion_texto(self):
        self.assertEqual(_norm_campo("Pulido "), "pulido")
        self.assertEqual(_norm_campo("Bocciárdato"), "bocciardato")
        self.assertIsNone(_norm_campo(None))

    def test_materiales_campos(self):
        v = _validada_base()
        c = _cruda_base()
        c["materiales"] = [
            {"rol": "encimera", "marca": "silestone ", "color": "blanco zeus",
             "grosor_cm": "2,0", "acabado": "pulido"}]
        r = comparar(v, c)
        self.assertEqual(r["materiales"]["encimera"]["campos_iguales"], 4)
        self.assertEqual(r["score"]["materiales"], 1.0)

    def test_material_rol_ausente(self):
        v = _validada_base()
        v["materiales"].append({"rol": "frontal", "marca": "Compac"})
        r = comparar(v, _cruda_base())
        self.assertIn("ausente_en", r["materiales"]["frontal"])
        self.assertIn(
            "material_campo",
            {d["categoria"] for d in r["discrepancias"]})

    def test_campos_nulos_robusto(self):
        """Missing keys / None no deben reventar nada."""
        r = comparar({}, {"piezas": [None, {}], "huecos": None,
                          "cantos": [{}], "materiales": [None]})
        self.assertIsInstance(r["discrepancias"], list)
        r2 = comparar({"piezas": [{"tipo": "encimera"}]}, {})
        self.assertEqual(len(r2["piezas"]["faltantes"]), 1)


class TestScoreYPrivacidad(unittest.TestCase):
    def test_score_bounds_identicos(self):
        r = comparar(_validada_base(), _cruda_base())
        for k in ("piezas", "huecos", "cantos", "materiales", "total"):
            self.assertGreaterEqual(r["score"][k], 0.0, k)
            self.assertLessEqual(r["score"][k], 1.0, k)
        self.assertAlmostEqual(r["score"]["total"], 1.0)

    def test_score_bounds_disparejos(self):
        v = _validada_base()
        c = {"piezas": [_pieza("isla", 900, 900)],
             "huecos": [{"tipo": "placa", "cantidad": 9}],
             "cantos": [{"tipo": "boleado", "longitud_ml": 99.0}],
             "materiales": [{"rol": "zocalo", "marca": "X"}]}
        r = comparar(v, c)
        for k in ("piezas", "huecos", "cantos", "materiales", "total"):
            self.assertGreaterEqual(r["score"][k], 0.0, k)
            self.assertLessEqual(r["score"][k], 1.0, k)

    def test_formula_total(self):
        r = comparar(_validada_base(), _cruda_base())
        s = r["score"]
        esperado = (0.5 * s["piezas"] + 0.2 * s["huecos"]
                    + 0.2 * s["cantos"] + 0.1 * s["materiales"])
        self.assertAlmostEqual(s["total"], round(esperado, 4))

    def test_sin_datos_personales(self):
        """Ni el resultado ni el informe pueden llevar datos personales."""
        r = comparar(_validada_base(), _cruda_base())
        plano = json.dumps(r, ensure_ascii=False) + informe_texto(r)
        for secreto in ("Cliente Ficticio", "Vendedor Ficticio",
                        "Calle Falsa", "Narnia", "cliente", "vendedor",
                        "direccion", "ciudad"):
            self.assertNotIn(secreto, plano)

    def test_advertencias_solo_conteo(self):
        r = comparar(_validada_base(), _cruda_base())
        self.assertEqual(r["advertencias"], {"validada": 1, "cruda": 0})


class TestTirasLongitudMl(unittest.TestCase):
    """Defecto 1: las tiras (copete/zócalo/frontal/costado/pilastra)
    deben emparejar por longitud efectiva (largo_mm o longitud_ml*1000)
    y segunda cota (altura_mm o ancho_mm), no a ciegas por tipo."""

    def test_copete_ml_igual_no_faltante_ni_sobrante(self):
        v = {"piezas": [{"tipo": "copete", "material_rol": "copete",
                         "longitud_ml": 0.62, "altura_mm": 50}]}
        c = {"piezas": [{"tipo": "copete", "material_rol": "copete",
                         "longitud_ml": 0.62, "altura_mm": 50}]}
        r = comparar(v, c)
        self.assertEqual(len(r["piezas"]["exactas"]), 1)
        self.assertEqual(r["piezas"]["faltantes"], [])
        self.assertEqual(r["piezas"]["sobrantes"], [])

    def test_copete_ml_distinto_no_casa_a_ciegas(self):
        v = {"piezas": [{"tipo": "copete", "longitud_ml": 0.62,
                         "altura_mm": 50, "zona": "A"}]}
        c = {"piezas": [{"tipo": "copete", "longitud_ml": 1.2,
                         "altura_mm": 50, "zona": "A"}]}
        r = comparar(v, c)
        self.assertEqual(r["piezas"]["exactas"], [])
        self.assertEqual(r["piezas"]["aproximadas"], [])
        self.assertEqual([p["tipo"] for p in r["piezas"]["faltantes"]],
                         ["copete"])
        self.assertEqual([p["tipo"] for p in r["piezas"]["sobrantes"]],
                         ["copete"])
        det = [d["detalle"] for d in r["discrepancias"]
               if d["categoria"] == "pieza_faltante"][0]
        self.assertIn("0.62ml", det)
        self.assertIn("H50", det)
        self.assertNotIn("None", det)

    def test_altura_distinta_no_casa(self):
        v = {"piezas": [{"tipo": "zocalo", "longitud_ml": 3.0,
                         "altura_mm": 120}]}
        c = {"piezas": [{"tipo": "zocalo", "longitud_ml": 3.0,
                         "altura_mm": 60}]}
        r = comparar(v, c)
        self.assertEqual(r["piezas"]["exactas"], [])
        self.assertEqual(len(r["piezas"]["faltantes"]), 1)
        self.assertEqual(len(r["piezas"]["sobrantes"]), 1)

    def test_delta_reportado_en_ml_y_altura(self):
        v = {"piezas": [{"tipo": "copete", "longitud_ml": 0.62,
                         "altura_mm": 50}]}
        c = {"piezas": [{"tipo": "copete", "longitud_ml": 0.64,
                         "altura_mm": 53}]}
        r = comparar(v, c)
        self.assertEqual(len(r["piezas"]["aproximadas"]), 1)
        self.assertEqual(r["piezas"]["aproximadas"][0]["deltas"],
                         {"longitud_ml": 0.02, "altura_mm": 3.0})
        por_campo = {d["campo"]: d for d in r["discrepancias"]
                     if d["categoria"] == "pieza_dim"}
        self.assertIn("longitud_ml", por_campo)
        self.assertIn("altura_mm", por_campo)
        self.assertEqual(por_campo["longitud_ml"]["validada"], 0.62)
        self.assertEqual(por_campo["longitud_ml"]["cruda"], 0.64)
        # el delta siempre es |validada - cruda| de los valores mostrados
        for d in por_campo.values():
            self.assertAlmostEqual(d["delta"], abs(d["validada"] - d["cruda"]))

    def test_frontal_largo_mm_equivale_a_longitud_ml(self):
        v = {"piezas": [{"tipo": "frontal", "largo_mm": 2400,
                         "altura_mm": 60}]}
        c = {"piezas": [{"tipo": "frontal", "longitud_ml": 2.4,
                         "altura_mm": 60}]}
        r = comparar(v, c)
        self.assertEqual(len(r["piezas"]["exactas"]), 1)
        self.assertEqual(r["piezas"]["faltantes"], [])
        self.assertEqual(r["piezas"]["sobrantes"], [])


class TestHuecosDimSinOrden(unittest.TestCase):
    """Defecto 2: las dimensiones de un hueco se comparan como par SIN
    orden (400x500 == 500x400)."""

    def _job(self, lv, av, lc, ac):
        base = {"tipo": "placa", "cantidad": 1,
                "centro_x_mm": 400, "centro_y_mm": 300}
        return ({"huecos": [dict(base, largo_mm=lv, ancho_mm=av)]},
                {"huecos": [dict(base, largo_mm=lc, ancho_mm=ac)]})

    def test_400x500_igual_500x400(self):
        r = comparar(*self._job(400, 500, 500, 400))
        cats = {d["categoria"] for d in r["discrepancias"]}
        self.assertNotIn("hueco_dim", cats)

    def test_cruzado_con_delta_se_etiqueta_bien(self):
        r = comparar(*self._job(400, 500, 500, 460))
        dims = [d for d in r["discrepancias"]
                if d["categoria"] == "hueco_dim"]
        self.assertEqual(len(dims), 1)
        d = dims[0]
        self.assertEqual(d["campo"], "largo_mm/ancho_mm")
        self.assertEqual(d["validada"], 400)   # validada.largo_mm
        self.assertEqual(d["cruda"], 460)      # cruda.ancho_mm
        self.assertAlmostEqual(d["delta"], abs(d["validada"] - d["cruda"]))

    def test_igual_orden_sigue_reportando(self):
        r = comparar(*self._job(400, 500, 460, 500))
        dims = [d for d in r["discrepancias"]
                if d["categoria"] == "hueco_dim"]
        self.assertEqual(len(dims), 1)
        self.assertEqual(dims[0]["campo"], "largo_mm")


class TestHuecosMarcosDistintos(unittest.TestCase):
    """Defecto 3: los centros solo son comparables si las encimeras de
    ambos lados están en el mismo marco (caja NO cruzada)."""

    def _job(self, lv, av, lc, ac, cx_v=400, cy_v=300, cx_c=1500,
             cy_c=700):
        return (
            {"piezas": [_pieza("encimera", lv, av, zona="A")],
             "huecos": [{"tipo": "placa", "cantidad": 1, "pieza_zona": "A",
                         "centro_x_mm": cx_v, "centro_y_mm": cy_v,
                         "largo_mm": 300, "ancho_mm": 200}]},
            {"piezas": [_pieza("encimera", lc, ac, zona="A")],
             "huecos": [{"tipo": "placa", "cantidad": 1, "pieza_zona": "A",
                         "centro_x_mm": cx_c, "centro_y_mm": cy_c,
                         "largo_mm": 300, "ancho_mm": 200}]})

    def test_marcos_distintos_una_discrepancia_por_hueco(self):
        r = comparar(*self._job(1400, 3070, 3070, 1395))
        no_comp = [d for d in r["discrepancias"]
                   if d["categoria"] == "hueco_posicion_no_comparable"]
        self.assertEqual(len(no_comp), 1)
        self.assertIn("marcos distintos", no_comp[0]["detalle"])
        self.assertIn("1400x3070", no_comp[0]["detalle"])
        self.assertIn("3070x1395", no_comp[0]["detalle"])
        cats = {d["categoria"] for d in r["discrepancias"]}
        self.assertNotIn("hueco_posicion", cats)
        self.assertNotIn("hueco_dim", cats)  # dims iguales → nada que decir
        # NO cuenta en el score de huecos (solo cantidades)
        self.assertEqual(r["score"]["huecos"], 1.0)

    def test_marcos_iguales_si_compara_posicion(self):
        r = comparar(*self._job(1400, 600, 1400, 600, cx_c=500, cy_c=300))
        pos = [d for d in r["discrepancias"]
               if d["categoria"] == "hueco_posicion"]
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos[0]["campo"], "centro_x_mm")
        self.assertEqual(pos[0]["delta"], 100.0)
        self.assertNotIn("hueco_posicion_no_comparable",
                         {d["categoria"] for d in r["discrepancias"]})

    def test_suelo_de_50mm_en_posicion(self):
        """Con marcos iguales, un delta de 30 mm (< max(50, tol_mm)) no
        se reporta como hueco_posicion."""
        r = comparar(*self._job(1400, 600, 1400, 600, cx_c=430, cy_c=300))
        cats = {d["categoria"] for d in r["discrepancias"]}
        self.assertNotIn("hueco_posicion", cats)
        self.assertNotIn("hueco_posicion_no_comparable", cats)

    def test_hueco_sin_centros_no_genera_no_comparable(self):
        v, c = self._job(1400, 3070, 3070, 1395)
        v["huecos"][0].pop("centro_x_mm")
        v["huecos"][0].pop("centro_y_mm")
        r = comparar(v, c)
        self.assertNotIn("hueco_posicion_no_comparable",
                         {d["categoria"] for d in r["discrepancias"]})


class TestPiezaDimCruzada(unittest.TestCase):
    """Defecto 4: en un match cruzado, la pieza_dim debe etiquetar el par
    comparado (validada.largo vs cruda.ancho) y el delta debe ser
    |validada - cruda| de los valores mostrados."""

    def test_cruzada_1400x3070_vs_3070x1395(self):
        v = {"piezas": [_pieza("encimera", 1400, 3070)]}
        c = {"piezas": [_pieza("encimera", 3070, 1395)]}
        r = comparar(v, c)
        self.assertEqual(len(r["piezas"]["aproximadas"]), 1)
        dims = [d for d in r["discrepancias"] if d["categoria"] == "pieza_dim"]
        self.assertEqual(len(dims), 1)
        d = dims[0]
        self.assertEqual(d["campo"], "largo_mm/ancho_mm")
        self.assertEqual(d["validada"], 1400)  # validada.largo_mm
        self.assertEqual(d["cruda"], 1395)     # cruda.ancho_mm
        self.assertAlmostEqual(d["delta"], 5.0)
        self.assertIn("validada.largo_mm", d["detalle"])
        self.assertIn("cruda.ancho_mm", d["detalle"])

    def test_delta_siempre_coherente(self):
        v = {"piezas": [_pieza("encimera", 600, 2400)]}
        c = {"piezas": [_pieza("encimera", 2400, 620)]}
        r = comparar(v, c)
        dims = [d for d in r["discrepancias"] if d["categoria"] == "pieza_dim"]
        self.assertTrue(dims)
        for d in dims:
            self.assertAlmostEqual(d["delta"], abs(d["validada"] - d["cruda"]))

    def test_match_identico_mantiene_etiqueta_simple(self):
        v = {"piezas": [_pieza("encimera", 500, 600)]}
        c = {"piezas": [_pieza("encimera", 520, 600)]}
        r = comparar(v, c)
        dims = [d for d in r["discrepancias"] if d["categoria"] == "pieza_dim"]
        self.assertEqual(len(dims), 1)
        self.assertEqual(dims[0]["campo"], "largo_mm")
        self.assertEqual(dims[0]["validada"], 500)
        self.assertEqual(dims[0]["cruda"], 520)
        self.assertAlmostEqual(dims[0]["delta"], 20.0)


class TestAcabadoTokens(unittest.TestCase):
    """El acabado se compara como conjunto de tokens alfabéticos:
    'Pulido (Polish)' == 'Polish (pulido)'."""

    def test_mismo_conjunto_de_tokens(self):
        v = {"materiales": [{"rol": "encimera",
                             "acabado": "Pulido (Polish)"}]}
        c = {"materiales": [{"rol": "encimera",
                             "acabado": "Polish (pulido)"}]}
        r = comparar(v, c)
        self.assertEqual(r["materiales"]["encimera"]["campos_iguales"], 1)
        self.assertEqual(r["score"]["materiales"], 1.0)
        self.assertNotIn("material_campo",
                         {d["categoria"] for d in r["discrepancias"]})

    def test_acabado_realmente_distinto(self):
        v = {"materiales": [{"rol": "encimera", "acabado": "Pulido"}]}
        c = {"materiales": [{"rol": "encimera", "acabado": "Mate"}]}
        r = comparar(v, c)
        self.assertEqual(r["materiales"]["encimera"]["campos_distintos"], 1)
        self.assertIn("material_campo",
                      {d["categoria"] for d in r["discrepancias"]})


class TestCLI(unittest.TestCase):
    def test_cli_json_salida(self):
        with tempfile.TemporaryDirectory() as tmp:
            vp = Path(tmp) / "v.json"
            cp = Path(tmp) / "c.json"
            out = Path(tmp) / "out.json"
            vp.write_text(json.dumps(_validada_base()), encoding="utf-8")
            cp.write_text(json.dumps(_cruda_base()), encoding="utf-8")
            rc = main([str(vp), str(cp), "--json", str(out)])
            self.assertEqual(rc, 0)
            guardado = json.loads(out.read_text(encoding="utf-8"))
            self.assertIn("score", guardado)
            self.assertIn("discrepancias", guardado)

    def test_subprocess_help(self):
        raiz = Path(__file__).resolve().parents[1]
        for script in ("comparar_extracciones.py", "bench_autonomo.py"):
            res = subprocess.run(
                [sys.executable, str(raiz / script), "--help"],
                capture_output=True, text=True,
                cwd=raiz, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            self.assertEqual(res.returncode, 0, script)


if __name__ == "__main__":
    unittest.main()
