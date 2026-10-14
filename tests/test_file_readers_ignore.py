"""Las salidas del propio extractor nunca deben entrar como fuente (contaminación)."""
import sys, unittest, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestShouldIgnore(unittest.TestCase):
    def test_salidas_del_pipeline_se_ignoran(self):
        from file_readers import should_ignore
        for n in ['J0030_extraccion.txt', 'J0030_extraccion.json', 'J0030_extraccion_cruda.txt',
                  'J0030_extraccion_cruda_fable5_2026-07.txt', 'J0030_extraccion_validada.json',
                  'batch_extraccion.json', 'divergencia.json', 'divergencia_fable5_2026-07.json',
                  'clasificacion.json', 'J0030_tablas.txt']:
            self.assertTrue(should_ignore(Path(n)), n)

    def test_notas_reales_no_se_ignoran(self):
        from file_readers import should_ignore
        for n in ['Info correo.txt', 'Diferencias.txt', 'Info segundas.txt', 'notas cliente.txt',
                  'PLANTILLA MARMOLISTA.pdf', 'PLANTA.png', 'J0030_Inga.xlsx']:
            self.assertFalse(should_ignore(Path(n)), n)

    def test_collect_files_excluye_extracciones_previas(self):
        from file_readers import collect_files
        with tempfile.TemporaryDirectory() as d:
            for n in ['Info correo.txt', 'J0001_extraccion_cruda.txt', 'J0001_extraccion.txt',
                      'divergencia.json', 'plano.png']:
                (Path(d) / n).write_text('x', encoding='utf-8')
            files = collect_files(Path(d))
            self.assertEqual([f.name for f in files['txts']], ['Info correo.txt'])
            self.assertEqual([f.name for f in files['images']], ['plano.png'])


if __name__ == '__main__':
    unittest.main()
