"""
Lectura y conversión de archivos: PDF → imágenes, XLSX, TXT.
"""
import base64
import os
import re
import tempfile
from pathlib import Path
from typing import Optional
import openpyxl

# Extensiones soportadas
IMG_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp', '.tiff', '.tif'}
PDF_EXTENSIONS = {'.pdf'}
EXCEL_EXTENSIONS = {'.xlsx', '.xls'}
TXT_EXTENSIONS = {'.txt'}

# Archivos a ignorar
IGNORE_PATTERNS = {'.tmp', '.lnk', '.dat', 'winmail', '.log'}


def should_ignore(path: Path) -> bool:
    name_lower = path.name.lower()
    for pat in IGNORE_PATTERNS:
        if pat in name_lower:
            return True
    return False


def image_to_base64(path: Path) -> tuple[str, str]:
    """Devuelve (base64_data, media_type)."""
    ext = path.suffix.lower()
    media_map = {
        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.png': 'image/png', '.gif': 'image/gif',
        '.webp': 'image/webp', '.bmp': 'image/png',
    }
    media_type = media_map.get(ext, 'image/jpeg')
    with open(path, 'rb') as f:
        data = base64.standard_b64encode(f.read()).decode('utf-8')
    return data, media_type


_easyocr_reader = None

def _get_easyocr_reader():
    """Inicializa EasyOCR una sola vez (carga el modelo ~1-2s)."""
    global _easyocr_reader
    if _easyocr_reader is None:
        import easyocr
        _easyocr_reader = easyocr.Reader(['es', 'en'], verbose=False)
    return _easyocr_reader


def run_easyocr_on_image(img) -> str:
    """
    Ejecuta EasyOCR sobre una imagen PIL y devuelve el texto reconocido
    como string, con cada detección en una línea junto a su confianza.
    """
    try:
        import numpy as np
        reader = _get_easyocr_reader()
        img_np = np.array(img.convert('RGB'))
        results = reader.readtext(img_np, detail=1, paragraph=False)
        lines = []
        for (_, text, conf) in results:
            if conf > 0.1:  # filtrar detecciones muy inciertas
                lines.append(f"{text} [{conf:.0%}]")
        return '\n'.join(lines)
    except Exception as e:
        return f"[EasyOCR error: {e}]"


def pdf_extract_text(path: Path, max_pages: int = 5, min_chars_per_page: int = 80) -> Optional[str]:
    """
    Intenta extraer texto de un PDF con capa de texto (PDFs digitales).
    Devuelve el texto extraído si el PDF tiene contenido textual suficiente,
    o None si el PDF es una imagen escaneada (sin texto).

    El umbral min_chars_per_page evita falsos positivos en PDFs con poco texto
    (ej: solo cabeceras) que en realidad necesitan visión.
    """
    try:
        import pdfplumber
        with pdfplumber.open(str(path)) as pdf:
            pages = pdf.pages[:max_pages]
            all_text = []
            total_chars = 0
            for page in pages:
                text = page.extract_text() or ''
                all_text.append(text)
                total_chars += len(text)
            # Verificar que hay texto suficiente (no es un escaneado)
            avg_chars = total_chars / max(len(pages), 1)
            if avg_chars < min_chars_per_page:
                return None
            return '\n\n--- PÁGINA SIGUIENTE ---\n\n'.join(t for t in all_text if t)
    except Exception:
        return None


def pdf_pages_to_base64(path: Path, dpi: int = 200, max_pages: int = 5, return_pil: bool = False) -> list:
    """
    Convierte páginas de un PDF a lista de (base64, media_type).
    Si return_pil=True, devuelve (base64, media_type, pil_image) para usar con EasyOCR.
    """
    try:
        from pdf2image import convert_from_path
        images = convert_from_path(str(path), dpi=dpi, first_page=1, last_page=max_pages)
        result = []
        for img in images:
            with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
                img.save(tmp.name, 'JPEG', quality=85)
                tmp_path = Path(tmp.name)
            # Verificar tamaño — API de Claude limita a 5MB por imagen
            size = tmp_path.stat().st_size
            if size > 4_500_000:
                # Reescalar al 75% y recomprimir con menor calidad
                img_small = img.resize(
                    (int(img.width * 0.75), int(img.height * 0.75))
                )
                img_small.save(tmp.name, 'JPEG', quality=75)
                size2 = tmp_path.stat().st_size
                if size2 > 4_500_000:
                    # Segundo intento: 50% del original
                    img_small2 = img.resize(
                        (int(img.width * 0.5), int(img.height * 0.5))
                    )
                    img_small2.save(tmp.name, 'JPEG', quality=70)
            with open(tmp_path, 'rb') as f:
                data = base64.standard_b64encode(f.read()).decode('utf-8')
            tmp_path.unlink(missing_ok=True)
            if return_pil:
                result.append((data, 'image/jpeg', img))
            else:
                result.append((data, 'image/jpeg'))
        return result
    except Exception as e:
        print(f"  [!] Error convirtiendo PDF {path.name}: {e}")
        return []


def read_excel_as_text(path: Path) -> str:
    """Lee un XLSX y devuelve su contenido como texto estructurado."""
    try:
        wb = openpyxl.load_workbook(str(path), data_only=True)
        lines = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            lines.append(f"=== HOJA: {sheet_name} ===")
            for row in ws.iter_rows(values_only=True):
                if any(c is not None for c in row):
                    # Filtrar filas completamente vacías o de errores
                    vals = [str(c) if c is not None else '' for c in row]
                    # Solo incluir si hay algo útil
                    non_empty = [v for v in vals if v.strip() and v != '#N/A' and v != 'None']
                    if non_empty:
                        lines.append('\t'.join(vals).rstrip())
        return '\n'.join(lines)
    except Exception as e:
        return f"[Error leyendo Excel: {e}]"


def read_txt(path: Path) -> str:
    """Lee un TXT con detección de encoding."""
    for enc in ['utf-8', 'latin-1', 'cp1252']:
        try:
            return path.read_text(encoding=enc)
        except Exception:
            continue
    return "[Error leyendo TXT]"


def _score_pdf(path: Path) -> int:
    """
    Puntúa un PDF para priorizar cuáles enviar a Claude.
    Mayor puntuación = más importante.
    """
    name = path.name.lower()
    score = 0
    # Plantillas de presupuesto marmolista — MÁS IMPORTANTES
    if 'plantilla' in name or 'presupuesto' in name:
        score += 100
    # Planos del programa de diseño
    if 'planta' in name or 'encimera' in name or 'diseño' in name or 'cocina' in name:
        score += 80
    # Presupuestos del marmolista — ordenar por número (mayor = más reciente)
    if name.startswith('pr') or name.startswith('f'):
        # Extraer número del presupuesto para priorizar el más reciente
        nums = re.findall(r'\d{4}', name)
        if nums:
            score += 50 + int(nums[0]) // 100  # más reciente = número más alto
    # PDFs escaneados (fechas en nombre)
    if re.search(r'\d{14}', name):
        score += 60
    return score


def collect_files(folder: Path, max_pdfs: int = 5) -> dict:
    """
    Recopila y prioriza los archivos de una carpeta de trabajo.
    Incluye archivos de subcarpetas "Segundas", "Terceras", etc. (medidas revisadas).
    Devuelve dict con listas por tipo + info de subcarpetas.
    """
    # Subcarpetas de medidas revisadas, en orden cronológico
    MEDIDAS_SUBFOLDERS = ['segundas', 'terceras', 'cuartas', 'segundas medidas', 'terceras medidas']

    files = {
        'images': [],
        'pdfs': [],           # lista de (Path, etiqueta) donde etiqueta indica origen
        'excels': [],
        'txts': [],
        'ignored': [],
        'subfolders': [],
        'pdfs_omitidos': [],
    }

    all_pdfs = []  # lista de (Path, etiqueta)

    def scan_dir(directory: Path, label: str):
        for f in sorted(directory.iterdir()):
            if f.is_dir():
                subfolder_name = f.name.lower()
                if any(s in subfolder_name for s in MEDIDAS_SUBFOLDERS):
                    # Es una subcarpeta de medidas revisadas → procesarla
                    scan_dir(f, f.name)
                elif not should_ignore(f):
                    files['subfolders'].append(f)
                continue
            if not f.is_file() or should_ignore(f):
                continue
            ext = f.suffix.lower()
            if ext in IMG_EXTENSIONS:
                files['images'].append(f)
            elif ext in PDF_EXTENSIONS:
                all_pdfs.append((f, label))
            elif ext in EXCEL_EXTENSIONS:
                files['excels'].append(f)
            elif ext in TXT_EXTENSIONS:
                files['txts'].append(f)

    scan_dir(folder, 'Primeras')

    # Ordenar PDFs por prioridad (más importante primero), luego limitar
    all_pdfs.sort(key=lambda x: _score_pdf(x[0]), reverse=True)
    files['pdfs'] = all_pdfs[:max_pdfs]
    files['pdfs_omitidos'] = all_pdfs[max_pdfs:]

    return files


def build_claude_content(folder: Path, verbose: bool = True, max_pdfs: int = 5) -> tuple[list, list[str]]:
    """
    Construye el contenido para enviar a Claude (lista de bloques de contenido).
    Devuelve (content_blocks, archivos_procesados).
    """
    files = collect_files(folder, max_pdfs=max_pdfs)
    content = []
    archivos = []

    segundas = [p for p, lbl in files['pdfs'] if lbl != 'Primeras']
    if verbose:
        omit_info = f", PDFs omitidos: {len(files['pdfs_omitidos'])}" if files['pdfs_omitidos'] else ""
        sub_info = f", Subcarpetas: {[s.name for s in files['subfolders']]}" if files['subfolders'] else ""
        seg_info = f", PDFs revisados (Segundas/Terceras): {len(segundas)}" if segundas else ""
        print(f"  Imágenes: {len(files['images'])}, PDFs: {len(files['pdfs'])}, "
              f"Excels: {len(files['excels'])}, TXTs: {len(files['txts'])}"
              f"{omit_info}{sub_info}{seg_info}")

    # 1. Texto inicial de contexto
    ctx = f"Carpeta de trabajo: {folder.name}\n\nA continuación tienes los archivos del trabajo:"
    if files['pdfs_omitidos']:
        ctx += f"\n\nNOTA: Por límite de contexto se han omitido {len(files['pdfs_omitidos'])} PDFs menos prioritarios: "
        ctx += ", ".join(f.name for f, _ in files['pdfs_omitidos'])
    if files['subfolders']:
        ctx += f"\n\nNOTA: Esta carpeta tiene subcarpetas con versiones adicionales: "
        ctx += ", ".join(s.name for s in files['subfolders'])
        ctx += ". Procesa los archivos de la carpeta raíz como la versión más definitiva."
    content.append({"type": "text", "text": ctx})

    # 2. TXTs
    for txt_path in files['txts']:
        texto = read_txt(txt_path)
        content.append({
            "type": "text",
            "text": f"\n--- ARCHIVO TXT: {txt_path.name} ---\n{texto}"
        })
        archivos.append(txt_path.name)

    # 3. Excels
    for xl_path in files['excels']:
        texto = read_excel_as_text(xl_path)
        content.append({
            "type": "text",
            "text": f"\n--- ARCHIVO EXCEL: {xl_path.name} ---\n{texto}"
        })
        archivos.append(xl_path.name)

    # 4. PDFs → texto (si digital) o imágenes (si escaneado)
    for pdf_path, pdf_label in files['pdfs']:
        label_str = f" [{pdf_label}]" if pdf_label != 'Primeras' else ""
        label_header = f" — MEDIDAS REVISADAS ({pdf_label})" if pdf_label != 'Primeras' else ""
        name_lower = pdf_path.name.lower()

        # Plantillas manuscritas: siempre usar imágenes (escritura a mano, no digital)
        is_handwritten = any(w in name_lower for w in ('plantilla', 'presupuesto encimera', 'encimera '))

        # Intentar extracción de texto para PDFs no manuscritos
        pdf_text = None
        if not is_handwritten:
            pdf_text = pdf_extract_text(pdf_path)

        if pdf_text:
            # PDF digital con texto — enviar como texto (mucho más barato)
            if verbose:
                print(f"  Texto PDF: {pdf_path.name}{label_str} [{len(pdf_text)} chars]")
            content.append({
                "type": "text",
                "text": f"\n--- PDF (TEXTO): {pdf_path.name}{label_header} ---\n{pdf_text}"
            })
            archivos.append(pdf_path.name)
        else:
            # PDF escaneado o con elementos gráficos — usar imágenes
            if verbose:
                print(f"  Convirtiendo PDF: {pdf_path.name}{label_str}")
            dpi = 250 if is_handwritten else 200
            pages = pdf_pages_to_base64(pdf_path, dpi=dpi, return_pil=is_handwritten)
            if pages:
                content.append({
                    "type": "text",
                    "text": f"\n--- PDF: {pdf_path.name} ({len(pages)} páginas){label_header} ---"
                })
                for i, page_data in enumerate(pages):
                    if is_handwritten and isinstance(page_data, tuple) and len(page_data) == 3:
                        data, media_type, pil_img = page_data
                        # Pre-OCR con EasyOCR para ayudar a Claude con la letra manuscrita
                        if verbose:
                            print(f"    EasyOCR pág {i+1}...")
                        ocr_text = run_easyocr_on_image(pil_img)
                        if ocr_text:
                            content.append({
                                "type": "text",
                                "text": f"[EasyOCR pág {i+1} — texto detectado, úsalo como pista para leer la letra manuscrita]:\n{ocr_text}"
                            })
                    else:
                        data, media_type = page_data[0], page_data[1]
                    content.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": data,
                        }
                    })
                archivos.append(pdf_path.name)

    # 5. Imágenes directas
    for img_path in files['images']:
        try:
            data, media_type = image_to_base64(img_path)
            content.append({
                "type": "text",
                "text": f"\n--- IMAGEN: {img_path.name} ---"
            })
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": data,
                }
            })
            archivos.append(img_path.name)
        except Exception as e:
            print(f"  [!] Error con imagen {img_path.name}: {e}")

    return content, archivos
