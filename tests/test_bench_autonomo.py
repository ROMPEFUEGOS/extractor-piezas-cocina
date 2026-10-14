# -*- coding: utf-8 -*-
"""
Tests de bench_autonomo.py con datos sintéticos (offline, sin API).
"""
import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bench_autonomo as bench


def _validada():
    return {
        "job_id": "J0009_TEST",
        "cliente": "Nadie Importante",
        "piezas": [{"tipo": "encimera", "material_rol": "encimera",
                    "largo_mm": 2400, "ancho_mm": 600}],
        "huecos": [{"tipo": "placa", "cantidad": 2}],
        "cantos": [{"tipo": "recto_pulido", "longitud_ml": 6.0}],
        "materiales": [{"rol": "encimera", "grosor_cm": 2.0}],
        "advertencias": [],
    }


def _cruda():
    return {
        "job_id": "J0009_TEST",
        "piezas": [{"tipo": "encimera", "material_rol": "encimera",
                    "largo_mm": 2400, "ancho_mm": 600}],
        "huecos": [{"tipo": "placa", "cantidad": 2}],
        "cantos": [{"tipo": "recto_pulido", "longitud_ml": 5.0}],
        "materiales": [{"rol": "encimera", "grosor_cm": "2,0"}],
        "advertencias": [],
    }


class _TrabajoFalso:
    """Doble de TrabajoExtraido para los tests (sin anthropic)."""
    job_id = "J0009_TEST"

    def to_json(self, indent=2):
        return json.dumps(_cruda(), ensure_ascii=False)

    def resumen_texto(self):
        return "=== TRABAJO J0009_TEST (falso) ==="


def _crear_job(tmp: Path, con_cruda=False, con_anotaciones=True,
               job_id="J0009_TEST"):
    job = tmp / ("%s_Cliente_Falso" % job_id)
    job.mkdir()
    (job / ("%s_extraccion_validada.json" % job_id)).write_text(
        json.dumps(_validada(), ensure_ascii=False), encoding="utf-8")
    # Debe permanecer intocado por el bench
    (job / ("%s_extraccion.json" % job_id)).write_text(
        '{"job_id": "J0009_TEST", "protegido": true}', encoding="utf-8")
    if con_cruda:
        (job / ("%s_extraccion_cruda.json" % job_id)).write_text(
            json.dumps(_cruda(), ensure_ascii=False), encoding="utf-8")
    if con_anotaciones:
        (job / "anotaciones.json").write_text('{"paginas_anotadas": {}}',
                                              encoding="utf-8")
        anot_dir = job / "anotaciones"
        anot_dir.mkdir()
        (anot_dir / "pagina_1.png").write_bytes(b"png-falso")
    return job


def _args(**kwargs):
    base = {"solo_comparar": False, "model": "modelo-test", "tol_mm": 20,
            "tol_pct": 3}
    base.update(kwargs)
    return argparse.Namespace(**base)


class TestOcultarRestaurar(unittest.TestCase):
    def test_ocultar_y_restaurar(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = _crear_job(Path(tmp))
            renombrados = bench._ocultar_anotaciones(job)
            self.assertEqual(len(renombrados), 2)
            self.assertFalse((job / "anotaciones.json").exists())
            self.assertFalse((job / "anotaciones").is_dir())
            self.assertTrue((job / "anotaciones.json.off").exists())
            self.assertTrue((job / "anotaciones.off").is_dir())
            bench._restaurar_anotaciones(renombrados)
            self.assertTrue((job / "anotaciones.json").exists())
            self.assertTrue((job / "anotaciones").is_dir())
            self.assertEqual(
                (job / "anotaciones" / "pagina_1.png").read_bytes(),
                b"png-falso")

    def test_ocultar_sin_anotaciones_no_rompe(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = _crear_job(Path(tmp), con_anotaciones=False)
            self.assertEqual(bench._ocultar_anotaciones(job), [])


class TestRestaurarConExcepcion(unittest.TestCase):
    """El restore-in-finally debe cubrirse con un extract falso que lanza."""

    def test_extraccion_fallida_restaura_anotaciones(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = _crear_job(Path(tmp))
            salida = Path(tmp) / "out"
            salida.mkdir()

            def _explota(job_dir, api_key, model):
                # En el momento de la extracción las anotaciones deben
                # estar ocultas.
                self.assertFalse((job_dir / "anotaciones.json").exists())
                self.assertFalse((job_dir / "anotaciones").is_dir())
                raise RuntimeError("fallo simulado de la API")

            with mock.patch.object(bench, "ejecutar_extraccion", _explota), \
                 mock.patch.dict("os.environ",
                                 {"ANTHROPIC_API_KEY": "sk-falsa"}):
                entrada = bench.procesar_job(job, _args(), salida)

            # El error queda registrado en la entrada del scoreboard
            self.assertIn("RuntimeError", entrada["error"])
            self.assertIsNone(entrada["score"])
            # ...y las anotaciones quedan restauradas pese al fallo
            self.assertTrue((job / "anotaciones.json").exists())
            self.assertTrue((job / "anotaciones").is_dir())
            self.assertFalse((job / "anotaciones.json.off").exists())
            self.assertFalse((job / "anotaciones.off").is_dir())
            # No se escribió cruda
            self.assertFalse(
                (job / "J0009_TEST_extraccion_cruda.json").exists())

    def test_keyboard_interrupt_restaura_y_propaga(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = _crear_job(Path(tmp))

            def _interrumpe(job_dir, api_key, model):
                raise KeyboardInterrupt()

            with mock.patch.object(bench, "ejecutar_extraccion",
                                   _interrumpe), \
                 mock.patch.dict("os.environ",
                                 {"ANTHROPIC_API_KEY": "sk-falsa"}):
                with self.assertRaises(KeyboardInterrupt):
                    bench.procesar_job(job, _args(), Path(tmp) / "out")
            self.assertTrue((job / "anotaciones.json").exists())
            self.assertTrue((job / "anotaciones").is_dir())


class TestExtraccionFeliz(unittest.TestCase):
    def test_procesar_job_completo(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = _crear_job(Path(tmp))
            salida = Path(tmp) / "bench"
            estados = []

            def _falsa(job_dir, api_key, model):
                estados.append((job_dir / "anotaciones.json").exists())
                return _TrabajoFalso()

            with mock.patch.object(bench, "ejecutar_extraccion", _falsa), \
                 mock.patch.dict("os.environ",
                                 {"ANTHROPIC_API_KEY": "sk-falsa"}):
                entrada = bench.procesar_job(job, _args(), salida)

            self.assertEqual(estados, [False])  # ocultas durante extracción
            self.assertIsNone(entrada["error"])
            self.assertIsInstance(entrada["score"], float)
            self.assertGreaterEqual(entrada["score"], 0.0)
            self.assertLessEqual(entrada["score"], 1.0)
            # Ficheros generados
            self.assertTrue(
                (job / "J0009_TEST_extraccion_cruda.json").exists())
            self.assertTrue(
                (job / "J0009_TEST_extraccion_cruda.txt").exists())
            self.assertTrue(
                (salida / "J0009_TEST_comparacion.json").exists())
            # Anotaciones restauradas y validada/extraccion intocados
            self.assertTrue((job / "anotaciones.json").exists())
            protegio = json.loads(
                (job / "J0009_TEST_extraccion.json").read_text("utf-8"))
            self.assertTrue(protegio["protegido"])
            self.assertTrue((job / "J0009_TEST_extraccion_validada.json")
                            .exists())


class TestSoloComparar(unittest.TestCase):
    def test_main_solo_comparar(self):
        """--solo-comparar sobre un dir temporal, sin anthropic ni API."""
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            job = _crear_job(raiz, con_cruda=True)
            salida = raiz / "salida_bench"
            rc = bench.main(["--solo-comparar", str(job),
                             "--salida", str(salida)])
            self.assertEqual(rc, 0)
            comp = salida / "J0009_TEST_comparacion.json"
            self.assertTrue(comp.exists())
            resultado = json.loads(comp.read_text("utf-8"))
            self.assertEqual(resultado["score"]["piezas"], 1.0)
            self.assertEqual(resultado["score"]["materiales"], 1.0)
            sb = json.loads(
                (salida / "scoreboard.json").read_text("utf-8"))
            self.assertEqual(len(sb["jobs"]), 1)
            self.assertIsNone(sb["jobs"][0]["error"])
            self.assertIn("media_total", sb)
            self.assertIn("fecha", sb)
            self.assertIn("model", sb)
            # Anotaciones intactas (nunca se tocaron)
            self.assertTrue((job / "anotaciones.json").exists())

    def test_main_solo_comparar_sin_cruda_da_error_pero_sigue(self):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            _crear_job(raiz, con_cruda=False, job_id="J0010_TEST")
            rc = bench.main(["--solo-comparar", str(raiz / "J0010_TEST_Cliente_Falso"),
                             "--salida", str(raiz / "out")])
            self.assertEqual(rc, 0)
            sb = json.loads(
                (raiz / "out" / "scoreboard.json").read_text("utf-8"))
            self.assertIsNotNone(sb["jobs"][0]["error"])
            self.assertEqual(sb["media_total"], 0.0)


class TestDescubrirJobs(unittest.TestCase):
    def test_lote_descubre_subcarpetas_con_validada(self):
        with tempfile.TemporaryDirectory() as tmp:
            lote = Path(tmp)
            con = _crear_job(lote, job_id="J0100_TEST")
            sin = lote / "J0101_SinValidad"
            sin.mkdir()
            (sin / "otro.txt").write_text("x", encoding="utf-8")
            args = _args(lote=str(lote), carpetas=[])
            jobs = bench.descubrir_jobs(args)
            self.assertEqual(jobs, [con])

    def test_sin_validada_se_rechaza(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = _crear_job(Path(tmp))
            (job / "J0009_TEST_extraccion_validada.json").unlink()
            self.assertEqual(
                bench.descubrir_jobs(_args(lote=None, carpetas=[str(job)])),
                [])


if __name__ == "__main__":
    unittest.main()
