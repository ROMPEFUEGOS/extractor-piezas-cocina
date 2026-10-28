"""Pruebas de plano_raster: lógica pura sin modelos + una integración opcional.

La integración real (motor RapidOCR sobre imagen sintética) solo corre si los
modelos PP-OCRv5 están descargados o si OCR_INTEGRACION=1, para que la suite
sea rápida sin conexión.
"""
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import plano_raster as pr


class TestLecturaGirada180(unittest.TestCase):
    """Lectura de un número simétrico girado 180° (invertir orden, 6<->9)."""

    def test_ejemplos_de_la_especificacion(self):
        self.assertEqual(pr.lectura_girada_180("009"), "600")
        self.assertEqual(pr.lectura_girada_180("0551"), "1550")
        self.assertEqual(pr.lectura_girada_180("1991"), "1661")

    def test_no_simetricos_devuelven_none(self):
        for token in ("1234", "2", "4", "3", "7", "2Z6", "600a", "1.650"):
            self.assertIsNone(pr.lectura_girada_180(token), token)

    def test_invariables_y_cambios(self):
        self.assertEqual(pr.lectura_girada_180("0"), "0")
        self.assertEqual(pr.lectura_girada_180("8"), "8")
        self.assertEqual(pr.lectura_girada_180("68"), "89")
        self.assertEqual(pr.lectura_girada_180("69"), "69")
        self.assertEqual(pr.lectura_girada_180("1000"), "0001")
        self.assertEqual(pr.lectura_girada_180(""), None)

    def test_conjunto_simetricos_coherente(self):
        # todo dígito del conjunto se mantiene legible girado; 2,3,4,7 quedan fuera
        self.assertEqual(pr.DIGITOS_SIMETRICOS, set("015689"))
        for d in pr.DIGITOS_SIMETRICOS:
            self.assertIsNotNone(pr.lectura_girada_180(d))
        for d in "2347":
            self.assertNotIn(d, pr.DIGITOS_SIMETRICOS)


class TestAnguloLinea(unittest.TestCase):
    """Dirección de lectura (0/90/180/270) según verticalidad de la caja y cls."""

    HORIZONTAL = [[10, 10], [110, 10], [110, 40], [10, 40]]      # ancho 101, alto 31
    VERTICAL = [[10, 10], [40, 10], [40, 210], [10, 210]]        # ancho 31, alto 201

    def test_horizontal_cls_0(self):
        self.assertEqual(pr.angulo_linea(self.HORIZONTAL, "0"), 0)

    def test_horizontal_cls_180(self):
        self.assertEqual(pr.angulo_linea(self.HORIZONTAL, "180"), 180)

    def test_vertical_cls_0_le_hacia_abajo(self):
        # RapidOCR rota el recorte vertical 90° antihorario: cls "0" => en la página
        # el texto va de arriba hacia abajo => 270
        self.assertEqual(pr.angulo_linea(self.VERTICAL, "0"), 270)

    def test_vertical_cls_180_le_hacia_arriba(self):
        # tras el rot90 antihorario la franja está boca abajo => en la página el
        # texto va de abajo hacia arriba => 90
        self.assertEqual(pr.angulo_linea(self.VERTICAL, "180"), 90)

    def test_sin_cls_valor_por_defecto(self):
        self.assertEqual(pr.angulo_linea(self.HORIZONTAL, None), 0)
        self.assertEqual(pr.angulo_linea(self.VERTICAL, None), 90)


def _linea(texto, conf, cx, cy, angulo=0, cls_label=None, cls_conf=None,
           numero=None, lectura=None, ambiguo=False, ancho=100, alto=100):
    return {
        "texto": texto, "conf": conf,
        "caja": [[cx - 40, cy - 10], [cx + 40, cy - 10], [cx + 40, cy + 10], [cx - 40, cy + 10]],
        "cx": cx, "cy": cy, "angulo": angulo, "cls_label": cls_label, "cls_conf": cls_conf,
        "numero": numero, "lectura_180": lectura, "ambiguo_180": ambiguo,
        "ancho": ancho, "alto": alto,
    }


def _resultado_manual():
    return {
        "ancho": 2480, "alto": 3508, "orientacion_pagina": 180, "n_lineas": 5,
        "segundos": 12.3, "motor": "rapidocr-3.10.0 det=ch_PP-OCRv5_det_server", "error": None,
        "lineas": [
            _linea("1165", 0.98, 1222, 1180, angulo=180, cls_label="0", cls_conf=0.9999,
                   numero=1165, lectura="9911", ambiguo=False),
            _linea("600", 0.67, 400, 200, angulo=90, cls_label="0", cls_conf=0.70,
                   numero=600, lectura="009", ambiguo=True),
            _linea("ENCIMERA + FRONTAL", 0.71, 800, 600, angulo=0, cls_label="0", cls_conf=0.99),
            _linea("x", 0.95, 300, 900),                        # 1 carácter: fuera
            _linea("basura", 0.10, 300, 950),                   # conf < 0.3: fuera
        ],
    }


class TestHintOCR(unittest.TestCase):
    def test_hint_completo(self):
        hint = pr.hint_ocr(_resultado_manual())
        lineas = hint.splitlines()
        self.assertEqual(
            lineas[0],
            "OCR local (PP-OCRv5) — página escaneada girada 180° (lecturas ya corregidas)",
        )
        self.assertIn("COTAS (número @ (x,y) px, giro, conf):", lineas)
        self.assertIn("TEXTOS:", lineas)
        # cotas ordenadas por (cy, cx): 600 (cy=200) antes que 1165 (cy=1180)
        i600 = lineas.index([l for l in lineas if l.startswith("600 @")][0])
        i1165 = lineas.index([l for l in lineas if l.startswith("1165 @")][0])
        self.assertLess(i600, i1165)
        self.assertIn("1165 @ (1222, 1180), giro 180°, conf 0.98", lineas)
        self.assertIn("— podría ser 009 si está girada", hint)
        self.assertIn("ENCIMERA + FRONTAL @ (800, 600), giro 0°, conf 0.71", lineas)
        # filtradas: 1 carácter y conf < 0.3
        self.assertNotIn("x @ (300, 900)", hint)
        self.assertNotIn("basura", hint)

    def test_hint_orientacion_normal(self):
        res = _resultado_manual()
        res["orientacion_pagina"] = 0
        hint = pr.hint_ocr(res)
        self.assertTrue(hint.startswith("OCR local (PP-OCRv5) — página escaneada con orientación normal"))

    def test_hint_error(self):
        self.assertEqual(pr.hint_ocr({"error": "boom", "lineas": []}), "[OCR local error: boom]")

    def test_hint_max_lineas(self):
        res = _resultado_manual()
        res["lineas"] = [
            _linea(str(n), 0.9, n * 10, 100, numero=n) for n in range(10)
        ] + [
            _linea(f"TXT{n}", 0.9, n * 10, 500) for n in range(10)
        ]
        hint = pr.hint_ocr(res, max_lineas=5)
        cuerpo = [l for l in hint.splitlines() if re.match(r".+ @ \(\d+, \d+\)", l)]
        self.assertEqual(len(cuerpo), 5)  # 5 cotas y 0 textos (las cotas van primero)
        self.assertIn("omitidas", hint)


class TestErroresYEntradas(unittest.TestCase):
    """ocr_imagen/ocr_hint_imagen nunca lanzan; devuelven error legible."""

    @classmethod
    def setUpClass(cls):
        import numpy as np

        cls.valida = np.full((60, 200, 3), 255, dtype=np.uint8)  # BGR en blanco

    def test_ocr_imagen_motor_no_disponible(self):
        with mock.patch.object(pr, "_construir_motor", side_effect=RuntimeError("sin modelos")):
            res = pr.ocr_imagen(self.valida)
        self.assertIsNotNone(res["error"])
        self.assertIn("sin modelos", res["error"])
        self.assertEqual(res["lineas"], [])

    def test_ocr_hint_imagen_motor_no_disponible(self):
        with mock.patch.object(pr, "_construir_motor", side_effect=RuntimeError("sin modelos")):
            hint = pr.ocr_hint_imagen(self.valida)
        self.assertTrue(hint.startswith("[OCR local error:"))
        self.assertIn("sin modelos", hint)

    def test_ocr_imagen_entrada_invalida(self):
        res = pr.ocr_imagen(12345)  # tipo no soportado
        self.assertIsNotNone(res["error"])
        res = pr.ocr_imagen("/tmp/definitivamente_no_existe_xyz.png")
        self.assertIsNotNone(res["error"])
        hint = pr.ocr_hint_imagen(12345)
        self.assertTrue(hint.startswith("[OCR local error:"))


def _modelos_presentes() -> bool:
    """¿Están los .onnx de PP-OCRv5 (det server, cls server, latin mobile) descargados?"""
    try:
        import rapidocr

        directorio = Path(rapidocr.__file__).resolve().parent / "models"
        necesarios = (
            "ch_PP-OCRv5_det_server.onnx",
            "ch_PP-LCNet_x1_0_textline_ori_cls_server.onnx",
            "latin_PP-OCRv5_rec_mobile.onnx",
        )
        return all((directorio / f).is_file() for f in necesarios)
    except Exception:
        return False


_INTEGRACION = _modelos_presentes() or os.environ.get("OCR_INTEGRACION") == "1"


@unittest.skipUnless(_INTEGRACION, "modelos PP-OCRv5 no descargados (OCR_INTEGRACION=1 para forzar)")
class TestIntegracionMotorReal(unittest.TestCase):
    """Motor real sobre una imagen sintética: 1550 derecha y COPETE girada 180°."""

    @classmethod
    def setUpClass(cls):
        from PIL import Image, ImageDraw, ImageFont

        try:
            fuente = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 56)
        except OSError:
            fuente = ImageFont.load_default(size=56)

        cls.imagen = Image.new("RGB", (1200, 800), "white")
        dibujo = ImageDraw.Draw(cls.imagen)
        dibujo.text((100, 100), "1550", font=fuente, fill="black")

        franja = Image.new("RGB", (500, 110), "white")
        ImageDraw.Draw(franja).text((10, 20), "COPETE", font=fuente, fill="black")
        cls.imagen.paste(franja.rotate(180, expand=True, fillcolor="white"), (650, 500))

    def test_encuentra_cota_y_etiqueta(self):
        res = pr.ocr_imagen(self.imagen)
        self.assertIsNone(res["error"], res["error"])
        self.assertEqual((res["ancho"], res["alto"]), (1200, 800))
        self.assertEqual(res["n_lineas"], len(res["lineas"]))
        self.assertGreaterEqual(res["n_lineas"], 2)
        numeros = {l["numero"] for l in res["lineas"] if l["numero"] is not None}
        self.assertIn(1550, numeros)
        self.assertTrue(
            any("COPETE" in l["texto"].upper() for l in res["lineas"]),
            [l["texto"] for l in res["lineas"]],
        )
        hint = pr.hint_ocr(res)
        self.assertIn("1550 @", hint)

    def test_ocr_hint_imagen_funciona(self):
        hint = pr.ocr_hint_imagen(self.imagen)
        self.assertFalse(hint.startswith("[OCR local error:"), hint)
        self.assertIn("1550", hint)


if __name__ == "__main__":
    unittest.main()
