# -*- coding: utf-8 -*-
"""
Tests de plano_vectorial.py con fixtures sintéticos (PDFs generados con
matplotlib en un directorio temporal): sin datos de cliente, sin red.

Cubre: giro 0/90/180/270 con des-inversión de palabras, deduplicación de
texto sobreimpreso (negrita simulada), detección de plano vectorial,
separación de palabras por espacio y por hueco, hint para el LLM y
tolerancia a páginas sin caracteres.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import matplotlib
matplotlib.use("Agg")
from matplotlib import pyplot as plt

import plano_vectorial as pv


def _crear_pdf_plano(directorio: Path) -> Path:
    """PDF sintético tipo plano: 150 segmentos de dibujo + cotas y etiquetas
    giradas 0/90/180/270 + un texto sobreimpreso dos veces (negrita simulada)."""
    fig = plt.figure(figsize=(8.27, 5.83))
    ax = fig.add_axes([0.08, 0.08, 0.88, 0.88])
    ax.set_xticks([])
    ax.set_yticks([])
    for i in range(150):
        x = 0.05 + (i % 15) * 0.06
        y = 0.05 + (i // 15) * 0.25
        ax.plot([x, x + 0.04], [y, y + 0.18], color="black", lw=0.8)
    fig.text(0.30, 0.70, "930", rotation=90, fontsize=10)     # se lee hacia arriba
    fig.text(0.55, 0.45, "1150", rotation=0, fontsize=10)
    fig.text(0.20, 0.25, "COPETE", rotation=180, fontsize=10)
    fig.text(0.72, 0.25, "2635", rotation=270, fontsize=10)   # se lee hacia abajo
    fig.text(0.40, 0.85, "COPETE FRONTAL H.700", rotation=0, fontsize=9)
    # Negrita simulada: el mismo texto casi en la misma posición (sobreimpresión)
    fig.text(0.10, 0.50, "ENCIMERA", fontsize=10)
    fig.text(0.1005, 0.50, "ENCIMERA", fontsize=10)
    salida = directorio / "plano_sintetico.pdf"
    fig.savefig(salida)
    plt.close(fig)
    return salida


def _crear_pdf_solo_texto(directorio: Path) -> Path:
    """PDF con texto pero sin dibujo (menos de UMBRAL_VECTORES primitivas)."""
    fig = plt.figure(figsize=(8.27, 5.83))
    fig.text(0.3, 0.5, "SOLO TEXTO SIN PLANO", fontsize=12)
    salida = directorio / "solo_texto.pdf"
    fig.savefig(salida)
    plt.close(fig)
    return salida


def _crear_pdf_sin_texto(directorio: Path) -> Path:
    """PDF de dibujo puro: segmentos pero ningún carácter."""
    fig = plt.figure(figsize=(8.27, 5.83))
    ax = fig.add_axes([0.08, 0.08, 0.88, 0.88])
    ax.set_xticks([])
    ax.set_yticks([])
    for i in range(150):
        x = 0.05 + (i % 15) * 0.06
        y = 0.05 + (i // 15) * 0.25
        ax.plot([x, x + 0.04], [y, y + 0.18], color="black", lw=0.8)
    salida = directorio / "sin_texto.pdf"
    fig.savefig(salida)
    plt.close(fig)
    return salida


def _char_fake(texto: str, x0: float, top: float, angulo: int = 0,
               size: float = 5.0, avance: float = 3.0) -> dict:
    """Carácter sintético para pruebas unitarias de las reglas internas."""
    if angulo == 0:
        matriz, x1, bottom = (1, 0, 0, 1, 0, 0), x0 + avance, top + size
    elif angulo == 90:   # glifo vertical: el avance va hacia arriba (top menor)
        matriz = (0, size / 10.0, -size / 10.0, 0, 0, 0)
        x1, bottom = x0 + size, top + avance
    elif angulo == 180:
        matriz = (-1, 0, 0, -1, 0, 0)
        x1, bottom = x0 + avance, top + size
    else:                # 270: avance hacia abajo (top mayor)
        matriz = (0, -size / 10.0, size / 10.0, 0, 0, 0)
        x1, bottom = x0 + size, top + avance
    return {"text": texto, "matrix": matriz, "size": size,
            "x0": x0, "x1": x1, "top": top, "bottom": bottom}


class TestPlanoVectorial(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.directorio = Path(cls._tmp.name)
        cls.pdf_plano = _crear_pdf_plano(cls.directorio)
        cls.pdf_texto = _crear_pdf_solo_texto(cls.directorio)
        cls.pdf_sin_texto = _crear_pdf_sin_texto(cls.directorio)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    # ---------- es_plano_vectorial ----------

    def test_es_plano_vectorial_verdadero_con_dibujo_y_texto(self):
        import pdfplumber
        with pdfplumber.open(str(self.pdf_plano)) as pdf:
            self.assertTrue(pv.es_plano_vectorial(pdf.pages[0]))

    def test_es_plano_vectorial_falso_con_poco_dibujo(self):
        import pdfplumber
        with pdfplumber.open(str(self.pdf_texto)) as pdf:
            pagina = pdf.pages[0]
            n_vectores = len(pagina.lines) + len(pagina.rects) + len(pagina.curves)
            self.assertLess(n_vectores, pv.UMBRAL_VECTORES)
            self.assertFalse(pv.es_plano_vectorial(pagina))

    def test_es_plano_vectorial_falso_sin_caracteres(self):
        import pdfplumber
        with pdfplumber.open(str(self.pdf_sin_texto)) as pdf:
            self.assertFalse(pv.es_plano_vectorial(pdf.pages[0]))

    # ---------- textos_pagina: giros y des-inversión ----------

    def test_palabras_giradas_sin_invertir(self):
        import pdfplumber
        with pdfplumber.open(str(self.pdf_plano)) as pdf:
            textos = pv.textos_pagina(pdf.pages[0])
        por_texto = {}
        for t in textos:
            por_texto.setdefault(t["texto"], []).append(t)
        self.assertEqual([t["angulo"] for t in por_texto["930"]], [90])
        self.assertEqual(por_texto["930"][0]["numero"], 930)      # no "039" -> 39
        self.assertEqual([t["angulo"] for t in por_texto["2635"]], [270])
        self.assertEqual(por_texto["2635"][0]["numero"], 2635)    # no "5362"
        self.assertEqual([t["angulo"] for t in por_texto["1150"]], [0])
        self.assertEqual(por_texto["1150"][0]["numero"], 1150)
        self.assertIn(180, [t["angulo"] for t in por_texto["COPETE"]])
        todos = {t["texto"] for t in textos}
        for invertido in ("039", "5362", "0511", "ETEPOC"):
            self.assertNotIn(invertido, todos)

    def test_separacion_de_palabras_con_espacios(self):
        import pdfplumber
        with pdfplumber.open(str(self.pdf_plano)) as pdf:
            textos = pv.textos_pagina(pdf.pages[0])
        todos = {t["texto"] for t in textos}
        self.assertIn("COPETE", todos)
        self.assertIn("FRONTAL", todos)
        self.assertIn("H.700", todos)
        self.assertNotIn("COPETEFRONTAL", todos)
        self.assertNotIn("FRONTALH.700", todos)

    def test_dedupe_de_texto_sobreimpreso(self):
        import pdfplumber
        with pdfplumber.open(str(self.pdf_plano)) as pdf:
            textos = pv.textos_pagina(pdf.pages[0])
        encimeras = [t for t in textos if t["texto"] == "ENCIMERA" and t["angulo"] == 0]
        self.assertEqual(len(encimeras), 1)
        todos = {t["texto"] for t in textos}
        self.assertNotIn("EENNCCIIMMEERRAA", todos)
        self.assertNotIn("EENNCIIMMEERAA", todos)

    def test_cajas_y_centros_coherentes(self):
        import pdfplumber
        with pdfplumber.open(str(self.pdf_plano)) as pdf:
            textos = pv.textos_pagina(pdf.pages[0])
        self.assertTrue(textos)
        for t in textos:
            self.assertLessEqual(t["x0"], t["cx"])
            self.assertLessEqual(t["cx"], t["x1"])
            self.assertLessEqual(t["top"], t["cy"])
            self.assertLessEqual(t["cy"], t["bottom"])
            self.assertLess(t["x0"], t["x1"])
            self.assertLess(t["top"], t["bottom"])
            self.assertIn(t["angulo"], (0, 90, 180, 270))

    def test_pagina_sin_caracteres_devuelve_lista_vacia(self):
        import pdfplumber
        with pdfplumber.open(str(self.pdf_sin_texto)) as pdf:
            self.assertEqual(pv.textos_pagina(pdf.pages[0]), [])

    # ---------- extraer_textos_pdf ----------

    def test_extraer_textos_pdf_estructura(self):
        paginas = pv.extraer_textos_pdf(str(self.pdf_plano), max_pages=3)
        self.assertEqual(len(paginas), 1)
        pagina = paginas[0]
        self.assertEqual(pagina["pagina"], 1)
        self.assertIsInstance(pagina["ancho_pt"], float)
        self.assertIsInstance(pagina["alto_pt"], float)
        self.assertGreaterEqual(pagina["n_vectores"], pv.UMBRAL_VECTORES)
        self.assertTrue(pagina["es_plano"])
        self.assertTrue(pagina["textos"])
        # Debe ser serializable a JSON (para el CLI --json)
        json.dumps(paginas, ensure_ascii=False)
        numeros = {t["numero"] for t in pagina["textos"] if t["numero"] is not None}
        self.assertLessEqual({930, 1150, 2635}, numeros)

    def test_extraer_textos_pdf_pagina_de_texto(self):
        paginas = pv.extraer_textos_pdf(str(self.pdf_texto))
        self.assertFalse(paginas[0]["es_plano"])
        self.assertLess(paginas[0]["n_vectores"], pv.UMBRAL_VECTORES)

    # ---------- hint_plano_vectorial ----------

    def test_hint_none_si_no_hay_plano(self):
        self.assertIsNone(pv.hint_plano_vectorial(str(self.pdf_texto)))

    def test_hint_contenido_compacto(self):
        hint = pv.hint_plano_vectorial(str(self.pdf_plano))
        self.assertIsNotNone(hint)
        self.assertIn("3 cotas", hint)
        self.assertIn("930 @ (x=", hint)
        self.assertIn("giro 90°", hint)
        self.assertIn("2635 @ (x=", hint)
        self.assertIn("giro 270°", hint)
        self.assertIn("COPETE @ (x=", hint)
        # Las cotas van antes que las etiquetas
        self.assertLess(hint.index("930 @ ("), hint.index("COPETE @ ("))

    # ---------- reglas internas con caracteres sintéticos ----------

    def test_particion_por_hueco_sin_espacios(self):
        # "2635" con avance 3 y size 5 (hueco 0) es una sola palabra;
        # dos palabras separadas 4 pt (> 0.6x5) se parten aunque no haya espacio.
        chars = [
            _char_fake("2", 100, 50), _char_fake("6", 103, 50),
            _char_fake("3", 106, 50), _char_fake("5", 109, 50),
            _char_fake("C", 116, 50),  # hueco de 4 pt respecto de "5" (que acaba en 112)
            _char_fake("O", 119, 50),
        ]
        pagina = SimpleNamespace(chars=chars, lines=[], rects=[], curves=[])
        textos = pv.textos_pagina(pagina)
        self.assertEqual([t["texto"] for t in textos], ["2635", "CO"])
        self.assertEqual(textos[0]["numero"], 2635)

    def test_orden_lectura_giros_90_y_270(self):
        # 90: se lee hacia arriba (top decreciente); los chars entran desordenados
        # y consecutivos (hueco ~0.2 pt entre glifos contiguos).
        chars_90 = [
            _char_fake("3", 50, 116.8, angulo=90),
            _char_fake("9", 50, 120.0, angulo=90),
            _char_fake("0", 50, 113.6, angulo=90),
        ]
        # 270: se lee hacia abajo (top creciente)
        chars_270 = [
            _char_fake("0", 200, 96.4, angulo=270),
            _char_fake("9", 200, 90.0, angulo=270),
            _char_fake("3", 200, 93.2, angulo=270),
        ]
        pagina = SimpleNamespace(chars=chars_90 + chars_270, lines=[], rects=[], curves=[])
        textos = {t["angulo"]: t for t in pv.textos_pagina(pagina)}
        self.assertEqual(textos[90]["texto"], "930")
        self.assertEqual(textos[90]["numero"], 930)
        self.assertEqual(textos[270]["texto"], "930")
        self.assertEqual(textos[270]["numero"], 930)

    def test_umbral_vectores_constante(self):
        self.assertEqual(pv.UMBRAL_VECTORES, 100)


if __name__ == "__main__":
    unittest.main()
