# -*- coding: utf-8 -*-
"""
Tests del enrutado de PDFs por CONTENIDO en file_readers.py (clasificar_pdf,
es_plantilla y build_claude_content) con fixtures sintéticos generados en un
directorio temporal: sin archivos de cliente, sin red y sin OCR real (la
pista de OCR se parchea con unittest.mock para devolver "PISTA-OCR").
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import matplotlib
matplotlib.use("Agg")
from matplotlib import pyplot as plt
from PIL import Image, ImageDraw

import file_readers


# --------------------------------------------------------------------------
# Fixtures sintéticas
# --------------------------------------------------------------------------

def _crear_pdf_plano(directorio: Path) -> Path:
    """Plano CAD vectorial: 150 segmentos de dibujo + cotas/etiquetas giradas
    ("930" a 90°, "COPETE" a 180°) + ~100 caracteres de texto corrido."""
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
    # ~100 caracteres de otro texto (rotación 0)
    fig.text(0.10, 0.90, "MEMORIA DESCRIPTIVA DE LA COCINA CON PARED DE AZULEJO E ISLA", fontsize=9)
    fig.text(0.10, 0.85, "CENTRAL PARA PRUEBAS DEL EXTRACTOR SIN DATOS REALES DE CLIENTE", fontsize=9)
    salida = directorio / "plano.pdf"
    fig.savefig(salida)
    plt.close(fig)
    return salida


def _crear_pdf_texto(directorio: Path) -> Path:
    """PDF digital de solo texto (>= 200 caracteres, < 10 vectores)."""
    fig = plt.figure(figsize=(8.27, 5.83))
    fig.text(0.05, 0.70, "PRESUPUESTO MGR DEL MARMOLISTA CON LINEAS DE TEXTO CORRIDO", fontsize=10)
    fig.text(0.05, 0.55, "REF 001 LAMINA PORCELANICA BLANCO CALACATTA PULIDO BRILLANTE", fontsize=10)
    fig.text(0.05, 0.40, "OBSERVACIONES DEL TRABAJO SIN NINGUN DIBUJO NI COTA ALGUNA", fontsize=10)
    fig.text(0.05, 0.25, "MAS TEXTO DE RELLENO PARA SUPERAR LOS DOSCIENTOS CARACTERES", fontsize=10)
    salida = directorio / "texto.pdf"
    fig.savefig(salida)
    plt.close(fig)
    return salida


def _crear_pdf_escaneado(directorio: Path) -> Path:
    """Escaneo: imagen RGB 1000x1400 guardada como PDF (sin capa de texto)."""
    img = Image.new("RGB", (1000, 1400), "white")
    draw = ImageDraw.Draw(img)
    draw.line([(100, 100), (900, 1300)], fill="black", width=3)
    draw.text((100, 200), "930 COPETE", fill="black")
    salida = directorio / "escaneo.pdf"
    img.save(salida, "PDF")
    return salida


def _crear_pdf_escaneado_con_ocr(directorio: Path) -> Path:
    """Escaneo CON capa de texto OCR (imagen a página completa + >= 80 chars):
    debe seguir clasificándose como escaneado (cobertura de imagen >= 50%)."""
    import numpy as np
    fig = plt.figure(figsize=(8.27, 11.69))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.imshow(np.full((200, 140), 200, dtype=np.uint8), cmap="gray", aspect="auto")
    fig.text(0.05, 0.95, "TEXTO OCR AÑADIDO POR EL ESCANER CON MAS DE OCHENTA CARACTERES POR PAGINA", fontsize=9)
    fig.text(0.05, 0.90, "SEGUNDA LINEA DE TEXTO RECONOCIDO PARA SUPERAR EL UMBRAL DE CARACTERES", fontsize=9)
    salida = directorio / "escaneo_ocr.pdf"
    fig.savefig(salida)
    plt.close(fig)
    return salida


# --------------------------------------------------------------------------
# es_plantilla
# --------------------------------------------------------------------------

class TestEsPlantilla(unittest.TestCase):
    def test_plano_cad_encimera_no_es_plantilla(self):
        self.assertFalse(file_readers.es_plantilla('ENCIMERA 1 NOMBRE APELLIDO.pdf'))

    def test_plantilla_y_presupuesto_encimera_si(self):
        self.assertTrue(file_readers.es_plantilla('PLANTILLA 1 X.pdf'))
        self.assertTrue(file_readers.es_plantilla('Presupuesto Encimera.pdf'))

    def test_encimera_sola_ya_no_cuenta(self):
        # 'encimera ' era criterio antes; ya no debe marcar nada por sí solo
        self.assertFalse(file_readers.es_plantilla('Encimera 2 cocina.pdf'))


# --------------------------------------------------------------------------
# clasificar_pdf
# --------------------------------------------------------------------------

class TestClasificarPdf(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.directorio = Path(cls._tmp.name)
        cls.pdf_plano = _crear_pdf_plano(cls.directorio)
        cls.pdf_texto = _crear_pdf_texto(cls.directorio)
        cls.pdf_escaneado = _crear_pdf_escaneado(cls.directorio)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_plano_vectorial(self):
        self.assertEqual(file_readers.clasificar_pdf(self.pdf_plano), 'vectorial_plano')

    def test_digital_texto(self):
        self.assertEqual(file_readers.clasificar_pdf(self.pdf_texto), 'digital_texto')

    def test_escaneado(self):
        self.assertEqual(file_readers.clasificar_pdf(self.pdf_escaneado), 'escaneado')

    def test_escaneado_con_capa_texto_ocr_sigue_escaneado(self):
        # Un escaneo CON capa de texto OCR (texto suficiente + imagen que
        # cubre la página) debe ir por la vía de imagen, no por la de texto.
        con_ocr = _crear_pdf_escaneado_con_ocr(self.directorio)
        self.assertIsNotNone(file_readers.pdf_extract_text(con_ocr))
        self.assertEqual(file_readers.clasificar_pdf(con_ocr), 'escaneado')

    def test_error_da_escaneado(self):
        self.assertEqual(
            file_readers.clasificar_pdf(self.directorio / "no_existe.pdf"), 'escaneado')


# --------------------------------------------------------------------------
# build_claude_content: enrutado de las tres rutas
# --------------------------------------------------------------------------

class TestBuildClaudeContentRutas(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.directorio = Path(cls._tmp.name)
        cls.pdf_plano = _crear_pdf_plano(cls.directorio)
        cls.pdf_texto = _crear_pdf_texto(cls.directorio)
        cls.pdf_escaneado = _crear_pdf_escaneado(cls.directorio)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _bloques_de_texto(self, content):
        return [b["text"] for b in content if b.get("type") == "text"]

    def test_rutas_por_contenido(self):
        files = file_readers.collect_files(self.directorio)
        self.assertEqual({f.name for f, _ in files['pdfs']},
                         {"plano.pdf", "texto.pdf", "escaneo.pdf"})
        with mock.patch.object(file_readers, "_ocr_hint",
                               return_value="PISTA-OCR") as m:
            content, archivos = file_readers.build_claude_content(
                self.directorio, verbose=False)
            self.assertTrue(m.called)

        # --- plano.pdf: texto VECTORIAL des-girado + imagen después ---
        idx_vectorial = None
        for i, bloque in enumerate(content):
            if bloque.get("type") == "text" and "PDF VECTORIAL" in bloque["text"] \
                    and "plano.pdf" in bloque["text"]:
                idx_vectorial = i
                break
        self.assertIsNotNone(idx_vectorial, "falta el bloque PDF VECTORIAL de plano.pdf")
        texto_vec = content[idx_vectorial]["text"]
        self.assertIn("930", texto_vec)
        self.assertIn("COPETE", texto_vec)
        self.assertNotIn("039", texto_vec)      # pdfplumber plano los invierte
        self.assertNotIn("ETEPOC", texto_vec)
        imagenes_despues = [b for b in content[idx_vectorial + 1:]
                            if b.get("type") == "image"]
        self.assertTrue(imagenes_despues, "el plano vectorial debe llevar imagen")

        # --- texto.pdf: bloque PDF (TEXTO) y sin vía de imagen ---
        textos = self._bloques_de_texto(content)
        con_texto = [t for t in textos if "--- PDF (TEXTO): texto.pdf" in t]
        self.assertTrue(con_texto, "falta el bloque PDF (TEXTO) de texto.pdf")
        self.assertIn("PRESUPUESTO MGR", con_texto[0])
        self.assertFalse([t for t in textos if "--- PDF: texto.pdf" in t],
                         "texto.pdf no debe pasar por la vía de imágenes")

        # --- escaneo.pdf: pista OCR antes de la imagen ---
        idx_pista = None
        for i, bloque in enumerate(content):
            if bloque.get("type") == "text" and "PISTA-OCR" in bloque["text"] \
                    and "escaneo.pdf" in bloque["text"]:
                idx_pista = i
                break
        self.assertIsNotNone(idx_pista, "falta la pista OCR de escaneo.pdf")
        self.assertIn("[OCR local pág 1 de 'escaneo.pdf'", content[idx_pista]["text"])
        imagenes_despues = [b for b in content[idx_pista + 1:]
                            if b.get("type") == "image"]
        self.assertTrue(imagenes_despues, "la pista OCR debe preceder a la imagen")

        # los tres quedan registrados como fuentes
        self.assertIn("plano.pdf", archivos)
        self.assertIn("texto.pdf", archivos)
        self.assertIn("escaneo.pdf", archivos)

    def test_extractor_ocr_0_quita_las_pistas(self):
        entorno = dict(os.environ)
        entorno["EXTRACTOR_OCR"] = "0"
        with mock.patch.dict(os.environ, entorno, clear=True):
            with mock.patch.object(file_readers, "_ocr_hint",
                                   return_value="PISTA-OCR") as m:
                content, _ = file_readers.build_claude_content(
                    self.directorio, verbose=False)
                self.assertFalse(m.called)
        textos = self._bloques_de_texto(content)
        self.assertFalse([t for t in textos if "PISTA-OCR" in t])
        self.assertFalse([t for t in textos if "[OCR local" in t])
        # ...pero las imágenes de las páginas escaneadas siguen
        self.assertTrue([b for b in content if b.get("type") == "image"])


class TestOcrHint(unittest.TestCase):
    """_ocr_hint: vía plano_raster (parcheada) y fallback a EasyOCR (parcheado)."""

    def _imagen(self):
        return Image.new("RGB", (50, 30), "white")

    def test_usa_plano_raster_por_el_modulo(self):
        # parchear plano_raster.ocr_hint_imagen (módulo importado de forma
        # perezosa dentro de _ocr_hint) verifica que la pista venga del módulo
        with mock.patch("plano_raster.ocr_hint_imagen",
                        return_value="PISTA-VIA-MODULO") as m:
            self.assertEqual(file_readers._ocr_hint(self._imagen()),
                             "PISTA-VIA-MODULO")
            self.assertTrue(m.called)

    def test_fallback_a_easyocr_si_falla_el_local(self):
        with mock.patch("plano_raster.ocr_hint_imagen",
                        return_value="[OCR local error: boom]"):
            with mock.patch.object(file_readers, "run_easyocr_on_image",
                                   return_value="TEXTO-EASY") as m:
                resultado = file_readers._ocr_hint(self._imagen())
        self.assertIn("[EasyOCR", resultado)
        self.assertIn("TEXTO-EASY", resultado)
        self.assertTrue(m.called)


class TestImagenesDirectas(unittest.TestCase):
    """Las imágenes directas grandes llevan pista OCR; las pequeñas y
    EXTRACTOR_OCR=0 no."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.directorio = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_imagen_grande_lleva_pista(self):
        Image.new("RGB", (500, 400), "white").save(self.directorio / "grande.png")
        with mock.patch.object(file_readers, "_ocr_hint",
                               return_value="PISTA-OCR") as m:
            content, archivos = file_readers.build_claude_content(
                self.directorio, verbose=False)
            self.assertTrue(m.called)
        idx_pista = None
        for i, bloque in enumerate(content):
            if bloque.get("type") == "text" and "PISTA-OCR" in bloque["text"]:
                idx_pista = i
                break
        self.assertIsNotNone(idx_pista)
        self.assertIn("[OCR local imagen 'grande.png'", content[idx_pista]["text"])
        self.assertTrue([b for b in content[idx_pista + 1:]
                         if b.get("type") == "image"])

    def test_imagen_pequena_sin_pista(self):
        Image.new("RGB", (200, 100), "white").save(self.directorio / "peque.png")
        with mock.patch.object(file_readers, "_ocr_hint",
                               return_value="PISTA-OCR") as m:
            content, _ = file_readers.build_claude_content(
                self.directorio, verbose=False)
            self.assertFalse(m.called)
        self.assertFalse([b for b in content if b.get("type") == "text"
                          and "PISTA-OCR" in b["text"]])

    def test_extractor_ocr_0_sin_pista(self):
        Image.new("RGB", (500, 400), "white").save(self.directorio / "grande.png")
        entorno = dict(os.environ)
        entorno["EXTRACTOR_OCR"] = "0"
        with mock.patch.dict(os.environ, entorno, clear=True):
            with mock.patch.object(file_readers, "_ocr_hint",
                                   return_value="PISTA-OCR") as m:
                content, _ = file_readers.build_claude_content(
                    self.directorio, verbose=False)
                self.assertFalse(m.called)
        self.assertTrue([b for b in content if b.get("type") == "image"])


if __name__ == "__main__":
    unittest.main()
