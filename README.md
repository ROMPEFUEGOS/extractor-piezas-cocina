# Extractor de Piezas — Cocimoble / ACyC

Sistema de extracción automática de datos de proyectos de encimeras de piedra usando Claude Vision (Anthropic API).

Dado una carpeta de trabajo con PDFs, Excel, imágenes y planos, extrae automáticamente:
- Materiales (marca, color, grosor, acabado, canto)
- Piezas con dimensiones (encimera, frontal/chapeado, copete, zócalo, costado, pilastra)
- Huecos y elaboraciones (placa, fregadero, grifo, enchufe)
- Cantos (ml ingletado, ml pulido)
- Advertencias sobre discrepancias o datos dudosos

## Estructura

```
ExtractorPiezas/
├── main.py              # Entry point — procesa una carpeta o batch
├── claude_extractor.py  # System prompt + función de extracción Claude Vision
├── file_readers.py      # Lectores PDF (texto y imagen), Excel, TXT, imágenes
├── models.py            # Modelos de datos (TrabajoExtraido, Pieza, etc.)
└── generar_dxf.py       # Generador de DXF desde JSON de extracción
```

## Requisitos

```bash
pip install anthropic pdf2image easyocr openpyxl pdfplumber ezdxf python-docx
# También necesario: poppler-utils (para pdf2image)
sudo apt install poppler-utils
```

## Uso

```bash
export ANTHROPIC_API_KEY=sk-ant-...

# Procesar un trabajo
python main.py "/ruta/J0297_Cliente_Cocimoble-Vendedor_Ciudad_Material" --guardar

# Procesar todos los trabajos de una carpeta
python main.py "/ruta/Cocimoble2025" --batch --guardar

# Generar DXF desde extracción JSON
python generar_dxf.py "/ruta/J0297_extraccion.json"
```

## Tipos de documentos procesados

| Documento | Método | Notas |
|-----------|--------|-------|
| Plantilla marmolista (PDF manuscrito) | Imagen + EasyOCR | DPI 250 para mejor lectura |
| Presupuesto MGR (PDF digital) | Texto (pdfplumber) | ~80% menos tokens que imagen |
| Plano 2020 (PDF digital) | Imagen | Contiene cotas y geometría |
| Renders 3D (JPG/PNG) | Imagen | Confirma disposición y materiales |
| Excel presupuesto | openpyxl texto | Estructurado como texto |

## Empresas soportadas

- **Cocimoble**: plantilla manuscrita estándar, presupuestos MGR (PR####)
- **ACyC**: sin plantilla, notas TXT, facturas MGR (F######) con mayor autoridad que presupuestos

## Flujo de trabajo previsto

```
Carpeta trabajo → ExtractorPiezas → JSON extracción
                                  → TXT resumen legible
                                  → DXF piezas (generar_dxf.py)
                                  → PDF acotado (dxf_auto_dim_v1.3.py)
```

## Notas de dominio

- Presupuesto más reciente = mayor número PR **y** fecha más reciente (la fecha tiene prioridad)
- Copete ≤9cm → tipo copete; ≥10cm → tipo frontal
- "1,2" en campo copete de plantilla = espesor 1.2cm, no altura (altura siempre 5cm por defecto)
- Zócalo altura por defecto: 10cm
- Ingletado en pilares porcelánicos: altura_frontal × nº_cantos (típico pilar = × 4)
- Materiales porcelánicos (Dekton, Coverlam, Neolith, Laminam…): fregadero sobre encimera por defecto
- Subcarpetas "Segundas/Terceras": medidas revisadas, siempre prevalecen sobre las primeras
