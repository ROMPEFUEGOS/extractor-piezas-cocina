#!/usr/bin/env python3
"""
anotador.py — UI web para anotar planos a mano alzada (highlighter).

Flujo:
    1. Selecciona un proyecto
    2. Recorre las páginas de PDF/imagen una por una
    3. Pinta con rotulador translúcido por color (encimera, frontal, etc.)
    4. Añade etiquetas de texto + notas globales
    5. Guarda imagen anotada + anotaciones.json en la carpeta del proyecto

El extractor luego ve anotaciones.json y manda a Claude las imágenes
anotadas + el texto contextual.

Uso:
    python anotador.py [--root /ruta/raiz] [--port 5050]
    Abre http://127.0.0.1:5050
"""

import argparse
import base64
import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    from flask import Flask, render_template_string, request, jsonify, send_file
except ImportError:
    print("Falta Flask. Instalar: pip install --user flask --break-system-packages", file=sys.stderr)
    sys.exit(1)

from PIL import Image
from file_readers import IMG_EXTENSIONS, pdf_pages_to_base64, image_to_base64

app = Flask(__name__)


@app.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

# Roots por defecto: las 3 carpetas conocidas
ROOTS = [
    Path("/home/kecojones/Documents/ProgramaDeAcotacionesDXF/Cocimoble2025"),
    Path("/home/kecojones/Documents/ProgramaDeAcotacionesDXF/Cocimoble2026"),
    Path("/home/kecojones/Documents/ProgramaDeAcotacionesDXF/ACyC Accesorios y cocinas"),
]

PALETA = [
    {"id": "encimera",       "color": "#FFD966", "label": "encimera", "tecla": "1"},
    {"id": "frontal",        "color": "#5B9BD5", "label": "frontal/chapeado", "tecla": "2"},
    {"id": "zocalo",         "color": "#9B7BC9", "label": "zócalo/rodapié", "tecla": "3"},
    {"id": "copete",         "color": "#F4A460", "label": "copete", "tecla": "4"},
    {"id": "costado",        "color": "#70AD47", "label": "costado/cascada", "tecla": "5"},
    {"id": "pilar",          "color": "#A0522D", "label": "pilar", "tecla": "6"},
    {"id": "hueco",          "color": "#E63946", "label": "hueco", "tecla": "7"},
    {"id": "pulido",         "color": "#00CED1", "label": "pulido (línea)", "tecla": "8"},
    {"id": "inglete",        "color": "#FF1493", "label": "inglete (línea)", "tecla": "9"},
    {"id": "pared",          "color": "#444444", "label": "pared (línea/área)", "tecla": "0"},
    {"id": "muebles_altos",  "color": "#B8B8B8", "label": "muebles altos (área)", "tecla": "Q"},
]


# ─── Helpers ─────────────────────────────────────────────────────────────────

def listar_proyectos() -> list[dict]:
    """Devuelve lista de proyectos detectados en los roots."""
    proyectos = []
    for root in ROOTS:
        if not root.exists():
            continue
        for d in sorted(root.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                anot_file = d / "anotaciones.json"
                proyectos.append({
                    "ruta": str(d),
                    "nombre": d.name,
                    "root": root.name,
                    "anotado": anot_file.exists(),
                })
    return proyectos


def listar_paginas(folder: Path) -> list[dict]:
    """Devuelve lista de todas las páginas (PDF pages + imágenes) del proyecto."""
    paginas = []
    # Imágenes directas
    for archivo in sorted(folder.iterdir()):
        if archivo.is_file() and archivo.suffix.lower() in IMG_EXTENSIONS:
            paginas.append({
                "id": f"img::{archivo.name}",
                "fuente": archivo.name,
                "tipo": "imagen",
                "pagina": 1,
                "size": archivo.stat().st_size,
            })
    # PDFs
    for archivo in sorted(folder.iterdir()):
        if archivo.is_file() and archivo.suffix.lower() == ".pdf":
            try:
                import pypdf
                reader = pypdf.PdfReader(str(archivo))
                n_paginas = len(reader.pages)
            except Exception:
                # Fallback: render barato a 72dpi para contar
                try:
                    pages = pdf_pages_to_base64(archivo, dpi=72, max_pages=30)
                    n_paginas = len(pages)
                except Exception:
                    n_paginas = 0
            for i in range(1, min(n_paginas, 30) + 1):
                paginas.append({
                    "id": f"pdf::{archivo.name}::{i}",
                    "fuente": archivo.name,
                    "tipo": "pdf",
                    "pagina": i,
                    "total_paginas": n_paginas,
                })
    return paginas


# Caché in-memory de páginas PDF renderizadas: { (path, mtime): [(bytes, mt), ...] }
_PDF_CACHE: dict = {}


def _pdf_pages_cached(archivo: Path, dpi: int = 140, max_pages: int = 30) -> list:
    """Caché LRU simple por (path, mtime). Evita re-renderizar PDFs en navegación."""
    key = (str(archivo.resolve()), archivo.stat().st_mtime)
    if key in _PDF_CACHE:
        return _PDF_CACHE[key]
    pages = pdf_pages_to_base64(archivo, dpi=dpi, max_pages=max_pages)
    # Convertir b64→bytes para no reencodear constantemente
    decoded = [(base64.b64decode(p[0]), p[1]) for p in pages]
    # Evitar memoria desbordada — limitar a 8 PDFs en caché
    if len(_PDF_CACHE) > 8:
        _PDF_CACHE.pop(next(iter(_PDF_CACHE)))
    _PDF_CACHE[key] = decoded
    return decoded


def render_pagina(folder: Path, pagina_id: str) -> tuple[bytes, str]:
    """Renderiza una página (PDF o imagen) a PNG. Devuelve (bytes, media_type)."""
    if pagina_id.startswith("img::"):
        nombre = pagina_id[len("img::"):]
        archivo = folder / nombre
        img = Image.open(archivo)
        if img.mode != "RGB":
            img = img.convert("RGB")
        # Limitar tamaño
        max_dim = 1600
        if max(img.size) > max_dim:
            ratio = max_dim / max(img.size)
            img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue(), "image/png"
    elif pagina_id.startswith("pdf::"):
        rest = pagina_id[len("pdf::"):]
        nombre, pag_str = rest.rsplit("::", 1)
        pagina_num = int(pag_str)
        archivo = folder / nombre
        if not archivo.exists():
            raise FileNotFoundError(f"PDF no existe: {archivo}")
        pages = _pdf_pages_cached(archivo, dpi=140, max_pages=30)
        if pagina_num <= len(pages):
            return pages[pagina_num - 1]
    raise ValueError(f"Página inválida: {pagina_id}")


def cargar_anotaciones(folder: Path) -> dict:
    f = folder / "anotaciones.json"
    if f.exists():
        try:
            datos = json.loads(f.read_text(encoding="utf-8"))
            # Validación de esquema mínimo — un JSON de otra versión o
            # corrupto NO debe pasar en silencio (perdería trazos del operador)
            if not isinstance(datos, dict) or \
                    not isinstance(datos.get("paginas_anotadas"), dict):
                print(f"  [WARN] anotaciones.json con esquema inesperado en "
                      f"{folder.name} — se ignora y se empieza de cero",
                      file=sys.stderr)
            else:
                return datos
        except Exception as e:
            print(f"  [WARN] anotaciones.json corrupto en {folder.name}: {e} "
                  f"— se ignora y se empieza de cero", file=sys.stderr)
    return {
        "proyecto": folder.name,
        "version": 1,
        "fecha": datetime.now().isoformat(),
        "paginas_anotadas": {},
        "notas_globales": [],
    }


def guardar_anotaciones(folder: Path, datos: dict):
    datos["fecha"] = datetime.now().isoformat()
    f = folder / "anotaciones.json"
    f.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")


def safe_pagina_filename(pagina_id: str) -> str:
    """Convierte un page-id en un filename seguro."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", pagina_id) + ".png"


# ─── Rutas Flask ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    proyectos = listar_proyectos()
    return render_template_string(INDEX_HTML, proyectos=proyectos)


def _es_ruta_segura(folder: Path) -> bool:
    """Verifica que la carpeta esté dentro de uno de los ROOTS configurados."""
    try:
        f_abs = folder.resolve()
        for r in ROOTS:
            try:
                f_abs.relative_to(r.resolve())
                return True
            except ValueError:
                continue
    except Exception:
        pass
    return False


@app.route("/proyecto/<path:ruta>")
def proyecto(ruta):
    folder = Path("/" + ruta)
    if not folder.exists() or not folder.is_dir():
        return "Proyecto no existe", 404
    if not _es_ruta_segura(folder):
        return "Ruta no permitida (debe estar bajo un root configurado)", 403
    paginas = listar_paginas(folder)
    anot = cargar_anotaciones(folder)
    # Marcar cuáles ya están anotadas
    for p in paginas:
        info = anot["paginas_anotadas"].get(p["id"])
        p["anotada"] = bool(info and info.get("tiene_trazos"))
    return render_template_string(PROYECTO_HTML,
        folder=folder, folder_str=str(folder),
        paginas=paginas, anot=anot, paleta=PALETA)


@app.route("/api/pagina_img")
def api_pagina_img():
    ruta = request.args.get("ruta")
    pagina_id = request.args.get("id")
    folder = Path(ruta)
    if not _es_ruta_segura(folder):
        return "Ruta no permitida", 403
    data, mt = render_pagina(folder, pagina_id)
    return send_file(io.BytesIO(data), mimetype=mt)


@app.route("/api/guardar_pagina", methods=["POST"])
def api_guardar_pagina():
    body = request.get_json()
    folder = Path(body["ruta"])
    if not _es_ruta_segura(folder):
        return jsonify({"ok": False, "error": "Ruta no permitida"}), 403
    pagina_id = body["pagina_id"]
    overlay_b64 = body.get("overlay_png_b64", "")  # canvas overlay (transparent)
    etiquetas = body.get("etiquetas", [])  # lista de {x,y,color,tipo,texto}
    tiene_trazos = body.get("tiene_trazos", False)

    anot = cargar_anotaciones(folder)

    # Guardar overlay como PNG en subcarpeta anotaciones/
    if tiene_trazos and overlay_b64:
        anot_dir = folder / "anotaciones"
        anot_dir.mkdir(exist_ok=True)
        # Decodificar overlay
        if "," in overlay_b64:
            overlay_b64 = overlay_b64.split(",", 1)[1]
        overlay_png = base64.b64decode(overlay_b64)
        # Cargar página original
        try:
            orig_data, orig_mt = render_pagina(folder, pagina_id)
            orig_img = Image.open(io.BytesIO(orig_data)).convert("RGBA")
            overlay_img = Image.open(io.BytesIO(overlay_png)).convert("RGBA")
            # Redimensionar overlay al tamaño original si difieren
            if overlay_img.size != orig_img.size:
                overlay_img = overlay_img.resize(orig_img.size, Image.LANCZOS)
            composite = Image.alpha_composite(orig_img, overlay_img)
            composite_rgb = composite.convert("RGB")
            fname = safe_pagina_filename(pagina_id)
            composite_rgb.save(anot_dir / fname, format="PNG", optimize=True)
            # También guardamos solo el overlay
            overlay_img.save(anot_dir / ("overlay_" + fname), format="PNG", optimize=True)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    anot["paginas_anotadas"][pagina_id] = {
        "tiene_trazos": tiene_trazos,
        "etiquetas": etiquetas,
        "canvas_w": body.get("canvas_w"),
        "canvas_h": body.get("canvas_h"),
        "fecha": datetime.now().isoformat(),
    }
    guardar_anotaciones(folder, anot)
    return jsonify({"ok": True})


@app.route("/api/notas_globales", methods=["POST"])
def api_notas_globales():
    body = request.get_json()
    folder = Path(body["ruta"])
    if not _es_ruta_segura(folder):
        return jsonify({"ok": False, "error": "Ruta no permitida"}), 403
    notas = body.get("notas", [])
    anot = cargar_anotaciones(folder)
    anot["notas_globales"] = [n for n in notas if n.strip()]
    guardar_anotaciones(folder, anot)
    return jsonify({"ok": True})


# ─── HTML embebido ───────────────────────────────────────────────────────────

INDEX_HTML = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Anotador de planos</title>
<style>
body { font-family: system-ui, sans-serif; margin: 0; padding: 0; background: #f5f5f5; }
header { background: #2c3e50; color: white; padding: 12px 24px; }
h1 { margin: 0; font-size: 18px; }
.container { padding: 16px 24px; }
.proyecto { background: white; padding: 10px 14px; margin: 6px 0; border-radius: 4px;
             display: flex; align-items: center; justify-content: space-between;
             border-left: 4px solid #ccc; }
.proyecto.anotado { border-left-color: #27ae60; }
.proyecto a { color: #2c3e50; text-decoration: none; font-weight: 500; }
.proyecto a:hover { text-decoration: underline; }
.tag { font-size: 11px; padding: 2px 6px; border-radius: 3px; background: #eee; color: #555; }
.tag.ok { background: #27ae60; color: white; }
.root-section { margin: 16px 0; }
.root-section h2 { font-size: 14px; color: #555; margin: 12px 0 6px; }
input[type=search] { width: 100%; padding: 8px 12px; border: 1px solid #ccc; border-radius: 4px;
                    font-size: 14px; box-sizing: border-box; margin-bottom: 12px; }
</style>
</head>
<body>
<header><h1>📝 Anotador de planos</h1></header>
<div class="container">
  <input type="search" id="filtro" placeholder="Filtrar por nombre..." autofocus>
  {% set roots_seen = [] %}
  {% for p in proyectos %}
    {% if p.root not in roots_seen %}
      {% if not roots_seen.append(p.root) %}{% endif %}
      <div class="root-section"><h2>{{ p.root }}</h2></div>
    {% endif %}
    <div class="proyecto {% if p.anotado %}anotado{% endif %}" data-nombre="{{ p.nombre|lower }}">
      <a href="/proyecto/{{ p.ruta.lstrip('/') }}">{{ p.nombre }}</a>
      {% if p.anotado %}<span class="tag ok">anotado</span>{% else %}<span class="tag">pendiente</span>{% endif %}
    </div>
  {% endfor %}
</div>
<script>
document.getElementById('filtro').oninput = e => {
  const q = e.target.value.toLowerCase();
  document.querySelectorAll('.proyecto').forEach(d => {
    d.style.display = d.dataset.nombre.includes(q) ? '' : 'none';
  });
};
</script>
</body>
</html>
"""

PROYECTO_HTML = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Anotar — {{ folder.name }}</title>
<style>
* { box-sizing: border-box; }
body { font-family: system-ui, sans-serif; margin: 0; padding: 0; background: #2a2a2a; color: #eee;
       overflow: hidden; height: 100vh; }
header { background: #1a1a1a; padding: 8px 16px; display: flex; align-items: center;
         justify-content: space-between; height: 48px; border-bottom: 1px solid #444; }
header h1 { margin: 0; font-size: 14px; font-weight: 500; }
header .nav-info { font-size: 13px; color: #aaa; }
.layout { display: flex; height: calc(100vh - 48px); }
.canvas-wrap { flex: 1; display: flex; align-items: center; justify-content: center;
                background: #2a2a2a; padding: 16px; overflow: auto; position: relative; }
.canvas-stack { position: relative; box-shadow: 0 4px 20px rgba(0,0,0,0.4); }
#imgPagina { display: block; max-width: 100%; height: auto; }
#canvasOverlay { position: absolute; top: 0; left: 0; cursor: crosshair; }
.sidebar { width: 280px; background: #1f1f1f; border-left: 1px solid #444;
           display: flex; flex-direction: column; overflow-y: auto; }
.section { padding: 12px 16px; border-bottom: 1px solid #333; }
.section h3 { margin: 0 0 8px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px;
              color: #888; font-weight: 600; }
.paleta { display: flex; flex-direction: column; gap: 3px; }
.color-btn { display: flex; align-items: center; gap: 8px;
              padding: 4px 8px; border: 2px solid transparent; border-radius: 4px;
              cursor: pointer; background: #2a2a2a; }
.color-btn:hover { background: #333; }
.color-btn.active { border-color: #fff; background: #3a3a3a; }
.color-btn .swatch { width: 22px; height: 22px; border-radius: 3px; flex-shrink: 0;
                      box-shadow: inset 0 0 0 1px rgba(0,0,0,0.3); }
.color-btn .lbl { flex: 1; font-size: 12px; color: #ddd; }
.color-btn .key { font-size: 10px; background: #000; color: #fff;
                   padding: 1px 5px; border-radius: 2px; font-family: monospace; }
.tool-btn { padding: 6px 10px; background: #333; color: #eee; border: 1px solid #555;
             border-radius: 3px; cursor: pointer; font-size: 12px; margin-right: 4px; }
.tool-btn.active { background: #4a90e2; border-color: #4a90e2; }
.tool-btn:hover { background: #444; }
.brush-size { width: 100%; }
textarea { width: 100%; background: #2a2a2a; color: #eee; border: 1px solid #555;
            padding: 6px; border-radius: 3px; font-family: inherit; font-size: 12px;
            min-height: 80px; resize: vertical; }
.nav-controls { padding: 16px; display: flex; gap: 8px; }
.btn { padding: 8px 16px; border-radius: 4px; cursor: pointer; font-size: 13px; border: none;
        font-weight: 500; }
.btn-skip { background: #555; color: white; flex: 1; }
.btn-save { background: #27ae60; color: white; flex: 2; }
.btn:hover { opacity: 0.9; }
.lista-paginas { font-size: 11px; max-height: 200px; overflow-y: auto;
                  background: #2a2a2a; padding: 4px; border-radius: 3px; }
.lista-paginas .pag-item { padding: 4px 6px; cursor: pointer; border-radius: 2px; }
.lista-paginas .pag-item:hover { background: #333; }
.lista-paginas .pag-item.actual { background: #4a90e2; }
.lista-paginas .pag-item.anotada::before { content: "● "; color: #27ae60; }
.fuente-tag { color: #888; font-size: 10px; }
.atajos { font-size: 11px; color: #888; line-height: 1.6; }
.atajos kbd { background: #333; padding: 1px 5px; border-radius: 2px; border: 1px solid #555;
               font-family: monospace; font-size: 10px; }
</style>
</head>
<body>
<header>
  <h1>📝 {{ folder.name }}</h1>
  <div class="nav-info"><a href="/" style="color:#aaa;">← volver</a></div>
</header>
<div class="layout">
  <div class="canvas-wrap">
    <div class="canvas-stack">
      <img id="imgPagina" alt="">
      <canvas id="canvasOverlay"></canvas>
    </div>
  </div>
  <div class="sidebar">
    <div class="section">
      <h3>Páginas ({{ paginas|length }})</h3>
      <div class="lista-paginas" id="listaPaginas">
        {% for p in paginas %}
          <div class="pag-item {% if p.anotada %}anotada{% endif %}" data-pid="{{ p.id }}">
            <span class="fuente-tag">{{ p.fuente[:30] }}{% if p.tipo=='pdf' %} pág {{ p.pagina }}{% endif %}</span>
          </div>
        {% endfor %}
      </div>
    </div>
    <div class="section">
      <h3>Color (rotulador)</h3>
      <div class="paleta">
        {% for c in paleta %}
        <div class="color-btn" data-tipo="{{ c.id }}" title="{{ c.label }} (tecla {{ c.tecla }})">
          <div class="swatch" style="background:{{ c.color }}"></div>
          <span class="lbl">{{ c.label }}</span>
          <span class="key">{{ c.tecla }}</span>
        </div>
        {% endfor %}
      </div>
    </div>
    <div class="section">
      <h3>Herramienta</h3>
      <button class="tool-btn active" data-tool="pen" title="(P)">Rotulador</button>
      <button class="tool-btn" data-tool="poly" title="(L) — click para añadir vértice, doble-click o Enter para cerrar, Esc cancela">Polilínea</button>
      <button class="tool-btn" data-tool="erase" title="(E)">Borrador</button>
      <button class="tool-btn" data-tool="undo" title="Cmd+Z">↶ Deshacer</button>
      <div style="margin-top:8px;">
        <label style="font-size:11px;color:#888;">Grosor: <span id="brushVal">22</span>px</label>
        <input type="range" min="6" max="60" value="22" id="brushSize" class="brush-size">
      </div>
    </div>
    <div class="section">
      <h3>Notas globales del proyecto</h3>
      <textarea id="notasGlobales" placeholder="Ej: cascada al suelo, alt 900mm × ancho isla 1200mm
Otra nota..."></textarea>
      <div style="font-size:10px;color:#888;margin-top:4px;">Una nota por línea. Se guardan al cambiar de página.</div>
    </div>
    <div class="section atajos">
      <h3>Atajos</h3>
      <kbd>1-9</kbd> <kbd>0</kbd> pared <kbd>Q</kbd> m.altos · <kbd>P</kbd> pluma · <kbd>E</kbd> borrador<br>
      <kbd>Cmd+Z</kbd> deshacer · <kbd>←/→</kbd> ant/sig<br>
      <kbd>Espacio</kbd> guardar+sig · <kbd>Backspace</kbd> limpiar
    </div>
    <div class="section" style="font-family:monospace;font-size:10px;color:#888;">
      <span id="debugPanel">capas: 0 · trazos: 0</span>
    </div>
    <div class="nav-controls">
      <button class="btn btn-skip" id="btnSkip">Saltar →</button>
      <button class="btn btn-save" id="btnSave">Guardar y →</button>
    </div>
  </div>
</div>

<script>
const RUTA_PROYECTO = {{ folder_str|tojson }};
const PAGINAS = {{ paginas|tojson }};
const PALETA = {{ paleta|tojson }};
const ANOT = {{ anot|tojson }};

// === Estado global ===
let idxActual = 0;
let trazos = [];           // historial: [{tipo:'pen'|'erase', tipo_pieza, color, grosor, puntos:[[x,y]]}]
let trazoActual = null;
let dibujando = false;
let tipoActual = "encimera";
let colorActual = PALETA[0].color;
let toolActual = "pen";
let grosor = 22;

const img = document.getElementById('imgPagina');
const canvas = document.getElementById('canvasOverlay');
const ctx = canvas.getContext('2d', { willReadFrequently: false });

// === Capas: un offscreen canvas opaco por cada tipo de pieza ===
// Razón: el documento original puede tener colores propios (rojos en planos,
// líneas azules CAD, etc.). Si pintamos con alpha sobre el documento,
// los colores del operador y del documento se mezclan y Claude no distingue
// quién pintó qué. Solución: cada color del operador en su propia capa.
// La VISIBLE queda como composite final con alpha fijo, sin acumulación.
const ALPHA = 0.55;
const capas = Object.create(null);   // { tipo: HTMLCanvasElement }

function inicCapas() {
  for (const k of Object.keys(capas)) delete capas[k];
  for (const p of PALETA) {
    const c = document.createElement('canvas');
    c.width = canvas.width;
    c.height = canvas.height;
    // Forzar contexto inmediatamente con willReadFrequently=true para que getImageData
    // y operaciones internas usen backing store en CPU (evita bugs sync GPU).
    c.getContext('2d', { willReadFrequently: true });
    capas[p.id] = c;
  }
  actualizarDebug();
}

function ctxDe(tipo) {
  if (!capas[tipo]) return null;
  return capas[tipo].getContext('2d', { willReadFrequently: true });
}

const _cuentaPaint = {};
function pintarSegmentoEnCapa(tipo_pieza, color, p1, p2, ancho) {
  const cap = capas[tipo_pieza];
  if (!cap) { console.error('NO CAPA:', tipo_pieza); return; }
  if (cap.width === 0 || cap.height === 0) { console.error('CAPA 0x0:', tipo_pieza); return; }
  // Usar SIEMPRE las mismas options para que el browser devuelva el mismo contexto cacheado
  const c = cap.getContext('2d', { willReadFrequently: true });
  c.lineCap = 'round'; c.lineJoin = 'round';
  c.lineWidth = ancho;
  c.globalCompositeOperation = 'source-over';
  c.globalAlpha = 1;       // por si algo lo dejó en otro valor
  c.strokeStyle = color;
  c.beginPath();
  c.moveTo(p1[0], p1[1]);
  c.lineTo(p2[0], p2[1]);
  c.stroke();
  _cuentaPaint[tipo_pieza] = (_cuentaPaint[tipo_pieza] || 0) + 1;
}

function pintarTrazoEnCapa(tr) {
  if (tr.tipo === 'erase') {
    // Borrador: borra de TODAS las capas
    for (const id of Object.keys(capas)) {
      const c = capas[id].getContext('2d', { willReadFrequently: true });
      c.lineCap = 'round'; c.lineJoin = 'round';
      c.lineWidth = tr.grosor;
      c.globalCompositeOperation = 'destination-out';
      c.strokeStyle = 'rgba(0,0,0,1)';
      c.beginPath();
      for (let i = 0; i < tr.puntos.length; i++) {
        const [x, y] = tr.puntos[i];
        if (i === 0) c.moveTo(x, y); else c.lineTo(x, y);
      }
      c.stroke();
    }
    return;
  }
  const c = ctxDe(tr.tipo_pieza);
  if (!c) return;
  c.lineCap = 'round'; c.lineJoin = 'round';
  c.lineWidth = tr.grosor;
  c.globalCompositeOperation = 'source-over';
  c.strokeStyle = tr.color;
  c.beginPath();
  for (let i = 0; i < tr.puntos.length; i++) {
    const [x, y] = tr.puntos[i];
    if (i === 0) c.moveTo(x, y); else c.lineTo(x, y);
  }
  if (tr.tipo === 'poly' && tr.cerrado) c.closePath();
  c.stroke();
}

function recomponerVista() {
  ctx.save();
  ctx.setTransform(1,0,0,1,0,0);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.globalCompositeOperation = 'source-over';
  ctx.globalAlpha = ALPHA;
  for (const p of PALETA) {
    const cap = capas[p.id];
    if (cap) ctx.drawImage(cap, 0, 0);
  }
  ctx.globalAlpha = 1;
  ctx.restore();
  actualizarDebug();
}

function limpiarCapas() {
  for (const id of Object.keys(capas)) {
    const c = capas[id].getContext('2d', { willReadFrequently: true });
    c.clearRect(0, 0, capas[id].width, capas[id].height);
  }
}

function redibujar() {
  limpiarCapas();
  for (const tr of trazos) pintarTrazoEnCapa(tr);
  recomponerVista();
  actualizarDebug();
}

function actualizarDebug() {
  const dbg = document.getElementById('debugPanel');
  if (!dbg) return;
  const counts = {};
  for (const id of Object.keys(capas)) {
    counts[id] = trazos.filter(t => t.tipo === 'pen' && t.tipo_pieza === id).length;
  }
  dbg.textContent = 'capas: ' + Object.keys(capas).length +
    ' · trazos: ' + trazos.length +
    ' · activo: ' + tipoActual +
    ' (' + (counts[tipoActual] || 0) + ')';
}

// Cache en memoria de trazos por pagina_id (sobrevive a navegación entre páginas
// dentro de la misma sesión; solo se persiste a disco con Guardar/Espacio).
const cacheTrazos = {};

function cargarPagina(idx) {
  // Antes de cambiar de página: guardar trazos actuales en cache
  if (typeof idxActual === 'number' && idxActual >= 0 && idxActual < PAGINAS.length) {
    const idActual = PAGINAS[idxActual] && PAGINAS[idxActual].id;
    if (idActual) cacheTrazos[idActual] = trazos.slice();
  }

  idxActual = idx;
  if (idx < 0 || idx >= PAGINAS.length) {
    alert('Última página. Vuelve al listado de proyectos.');
    return;
  }
  const p = PAGINAS[idx];
  document.querySelectorAll('.pag-item').forEach((el, i) => {
    el.classList.toggle('actual', i === idx);
  });
  // Restaurar trazos de la cache si la página ya se visitó en esta sesión
  trazos = (cacheTrazos[p.id] || []).slice();
  trazoActual = null;
  img.onload = () => {
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    canvas.style.width = img.clientWidth + 'px';
    canvas.style.height = img.clientHeight + 'px';
    inicCapas();
    redibujar();
  };
  img.src = `/api/pagina_img?ruta=${encodeURIComponent(RUTA_PROYECTO)}&id=${encodeURIComponent(p.id)}`;
}

function ptCanvas(e) {
  const rect = canvas.getBoundingClientRect();
  const sx = canvas.width / rect.width;
  const sy = canvas.height / rect.height;
  return [(e.clientX - rect.left) * sx, (e.clientY - rect.top) * sy];
}

// === Modo polilínea (vértices exactos por click) ===
let polyEnConstruccion = null;  // {tipo_pieza,color,grosor,puntos:[[x,y]]}
let polyCursor = null;           // posición actual del cursor para preview

function pintarPolilineaEnCapa(tipo_pieza, color, puntos, ancho, cerrar=true) {
  const cap = capas[tipo_pieza];
  if (!cap || puntos.length < 2) return;
  const c = cap.getContext('2d', { willReadFrequently: true });
  c.lineCap = 'round'; c.lineJoin = 'round';
  c.lineWidth = ancho;
  c.globalCompositeOperation = 'source-over';
  c.globalAlpha = 1;
  c.strokeStyle = color;
  c.beginPath();
  c.moveTo(puntos[0][0], puntos[0][1]);
  for (let i = 1; i < puntos.length; i++) c.lineTo(puntos[i][0], puntos[i][1]);
  if (cerrar) c.closePath();
  c.stroke();
}

function dibujarPreviewPolilinea() {
  if (!polyEnConstruccion) return;
  const pts = polyEnConstruccion.puntos;
  ctx.lineCap = 'round'; ctx.lineJoin = 'round';
  ctx.lineWidth = polyEnConstruccion.grosor;
  ctx.globalAlpha = ALPHA;
  ctx.strokeStyle = polyEnConstruccion.color;
  // Líneas confirmadas entre vértices
  if (pts.length >= 2) {
    ctx.beginPath();
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
    ctx.stroke();
  }
  // Línea preview desde último vértice al cursor
  if (pts.length >= 1 && polyCursor) {
    ctx.setLineDash([10, 10]);
    ctx.beginPath();
    ctx.moveTo(pts[pts.length-1][0], pts[pts.length-1][1]);
    ctx.lineTo(polyCursor[0], polyCursor[1]);
    ctx.stroke();
    ctx.setLineDash([]);
  }
  // Vértices como puntos rojos
  ctx.globalAlpha = 1;
  ctx.fillStyle = '#000';
  for (const p of pts) {
    ctx.beginPath();
    ctx.arc(p[0], p[1], 5, 0, 2*Math.PI);
    ctx.fill();
  }
}

function cerrarPolilinea(cerrar=true) {
  if (!polyEnConstruccion || polyEnConstruccion.puntos.length < 2) {
    polyEnConstruccion = null;
    polyCursor = null;
    recomponerVista();
    return;
  }
  // Persistir
  pintarPolilineaEnCapa(
    polyEnConstruccion.tipo_pieza,
    polyEnConstruccion.color,
    polyEnConstruccion.puntos,
    polyEnConstruccion.grosor,
    cerrar
  );
  trazos.push({
    tipo: 'poly',
    color: polyEnConstruccion.color,
    tipo_pieza: polyEnConstruccion.tipo_pieza,
    grosor: polyEnConstruccion.grosor,
    puntos: polyEnConstruccion.puntos.slice(),
    cerrado: cerrar,
  });
  polyEnConstruccion = null;
  polyCursor = null;
  recomponerVista();
}

canvas.onpointerdown = e => {
  e.preventDefault();
  if (toolActual === 'poly') {
    const punto = ptCanvas(e);
    if (!polyEnConstruccion) {
      polyEnConstruccion = {
        tipo_pieza: tipoActual,
        color: colorActual,
        grosor: grosor,
        puntos: [punto],
      };
    } else {
      // Si click cerca del primer vértice (≤15px), cerrar
      const p0 = polyEnConstruccion.puntos[0];
      const dx = punto[0] - p0[0], dy = punto[1] - p0[1];
      if (polyEnConstruccion.puntos.length >= 3 && Math.sqrt(dx*dx + dy*dy) < 15) {
        cerrarPolilinea(true);
        return;
      }
      polyEnConstruccion.puntos.push(punto);
    }
    polyCursor = punto;
    recomponerVista();
    dibujarPreviewPolilinea();
    return;
  }
  canvas.setPointerCapture(e.pointerId);
  dibujando = true;
  trazoActual = {
    tipo: toolActual,
    color: colorActual,
    tipo_pieza: tipoActual,
    grosor: grosor,
    puntos: [ptCanvas(e)],
  };
};

canvas.ondblclick = e => {
  if (toolActual === 'poly' && polyEnConstruccion) {
    e.preventDefault();
    cerrarPolilinea(true);
  }
};

canvas.onpointermove = e => {
  if (toolActual === 'poly' && polyEnConstruccion) {
    polyCursor = ptCanvas(e);
    recomponerVista();
    dibujarPreviewPolilinea();
    return;
  }
  if (!dibujando) return;
  const punto = ptCanvas(e);
  trazoActual.puntos.push(punto);
  const n = trazoActual.puntos.length;
  if (n < 2) return;
  const prev = trazoActual.puntos[n-2];
  if (trazoActual.tipo === 'pen') {
    // 1. Pintar en la capa offscreen (registro real)
    pintarSegmentoEnCapa(trazoActual.tipo_pieza, trazoActual.color, prev, punto, trazoActual.grosor);
    // 2. Pintar DIRECTAMENTE en la canvas visible para feedback instantáneo
    //    (sin recomposición completa — mucho más rápido).
    ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    ctx.lineWidth = trazoActual.grosor;
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = ALPHA;
    ctx.strokeStyle = trazoActual.color;
    ctx.beginPath();
    ctx.moveTo(prev[0], prev[1]);
    ctx.lineTo(punto[0], punto[1]);
    ctx.stroke();
    ctx.globalAlpha = 1;
  } else {
    // Borrador: borrar en todas las capas + en visible
    for (const id of Object.keys(capas)) {
      const c = capas[id].getContext('2d', { willReadFrequently: true });
      c.lineCap = 'round'; c.lineJoin = 'round';
      c.lineWidth = trazoActual.grosor;
      c.globalCompositeOperation = 'destination-out';
      c.strokeStyle = 'rgba(0,0,0,1)';
      c.beginPath(); c.moveTo(prev[0], prev[1]); c.lineTo(punto[0], punto[1]); c.stroke();
    }
    ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    ctx.lineWidth = trazoActual.grosor;
    ctx.globalCompositeOperation = 'destination-out';
    ctx.strokeStyle = 'rgba(0,0,0,1)';
    ctx.beginPath(); ctx.moveTo(prev[0], prev[1]); ctx.lineTo(punto[0], punto[1]); ctx.stroke();
    ctx.globalCompositeOperation = 'source-over';
  }
};
canvas.onpointerup = e => {
  if (!dibujando) return;
  dibujando = false;
  if (trazoActual && trazoActual.puntos.length > 1) {
    trazos.push(trazoActual);
    // Diagnóstico: cuentas reales por tipo + pixeles por capa
    const stats = {};
    for (const p of PALETA) {
      const cap = capas[p.id];
      if (!cap) { stats[p.id] = 'NO_CAP'; continue; }
      try {
        const data = cap.getContext('2d', { willReadFrequently: true }).getImageData(0, 0, cap.width, cap.height).data;
        let n = 0;
        for (let i = 3; i < data.length; i += 4) if (data[i] > 0) n++;
        stats[p.id] = n;
      } catch (e) { stats[p.id] = 'ERR'; }
    }
    console.log('[upStats] último='+trazoActual.tipo_pieza,
      'paints/tipo:', JSON.stringify(_cuentaPaint),
      'pixeles:', JSON.stringify(stats));
    recomponerVista();
  }
  trazoActual = null;
};

// Paleta — usar event delegation para robustez contra clicks en inner elements
const paletaEl = document.querySelector('.paleta');
paletaEl.addEventListener('click', (ev) => {
  const btn = ev.target.closest('.color-btn');
  if (!btn) return;
  const tipo = btn.dataset.tipo;
  if (!tipo) {
    console.warn('color-btn sin data-tipo', btn);
    return;
  }
  const p = PALETA.find(x => x.id === tipo);
  if (!p) {
    console.warn('PALETA no encuentra tipo:', tipo);
    return;
  }
  document.querySelectorAll('.color-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  tipoActual = tipo;
  colorActual = p.color;
  toolActual = 'pen';
  document.querySelectorAll('.tool-btn').forEach(b => b.classList.remove('active'));
  const penBtn = document.querySelector('.tool-btn[data-tool=pen]');
  if (penBtn) penBtn.classList.add('active');
  // Log visible
  console.log('Color cambiado:', {tipo, color: p.color});
  actualizarDebug();
});
document.querySelector('.color-btn').classList.add('active');

// Herramientas
document.querySelectorAll('.tool-btn').forEach(btn => {
  btn.onclick = () => {
    const tool = btn.dataset.tool;
    if (tool === 'undo') {
      if (trazos.length) trazos.pop();
      redibujar();
      return;
    }
    document.querySelectorAll('.tool-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    toolActual = tool;
  };
});

document.getElementById('brushSize').oninput = e => {
  grosor = parseInt(e.target.value);
  document.getElementById('brushVal').textContent = grosor;
};

// Lista de páginas — click para saltar
document.querySelectorAll('.pag-item').forEach((el, i) => {
  el.onclick = () => cargarPagina(i);
});

// Atajos teclado
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'TEXTAREA') return;
  // Color por tecla (case-insensitive para letras)
  for (let i = 0; i < PALETA.length; i++) {
    if (e.key === PALETA[i].tecla
        || e.key.toLowerCase() === PALETA[i].tecla.toLowerCase()) {
      document.querySelectorAll('.color-btn')[i].click();
      e.preventDefault(); return;
    }
  }
  if (e.key === 'p' || e.key === 'P') {
    document.querySelector('.tool-btn[data-tool=pen]').click(); e.preventDefault();
  } else if (e.key === 'l' || e.key === 'L') {
    document.querySelector('.tool-btn[data-tool=poly]').click(); e.preventDefault();
  } else if (e.key === 'Enter' && polyEnConstruccion) {
    cerrarPolilinea(true); e.preventDefault();
  } else if (e.key === 'Escape' && polyEnConstruccion) {
    polyEnConstruccion = null; polyCursor = null; recomponerVista(); e.preventDefault();
  } else if (e.key === 'e' || e.key === 'E') {
    document.querySelector('.tool-btn[data-tool=erase]').click(); e.preventDefault();
  } else if (e.key === 'z' && (e.metaKey || e.ctrlKey)) {
    if (trazos.length) trazos.pop();
    redibujar();
    e.preventDefault();
  } else if (e.key === 'ArrowRight') {
    document.getElementById('btnSkip').click(); e.preventDefault();
  } else if (e.key === 'ArrowLeft') {
    cargarPagina(Math.max(0, idxActual - 1)); e.preventDefault();
  } else if (e.key === ' ') {
    document.getElementById('btnSave').click(); e.preventDefault();
  } else if (e.key === 'Backspace') {
    if (confirm('Limpiar todos los trazos?')) {
      trazos = [];
      limpiarCapas();
      recomponerVista();
      actualizarDebug();
    }
    e.preventDefault();
  }
});

async function guardarPaginaActual(tieneTrazos) {
  // Capturar overlay como PNG (canvas con transparencia)
  const overlay_b64 = canvas.toDataURL('image/png');
  const p = PAGINAS[idxActual];
  const etiquetas = trazos.map((t, i) => {
    const meta = {
      idx: i, tipo: t.tipo_pieza, color: t.color,
      n_puntos: t.puntos.length,
      modo: t.tipo,  // 'pen' o 'poly'
    };
    // Persistimos puntos crudos en polilíneas (vértices exactos) y en
    // anotaciones que el postprocesador necesita para clasificar aristas
    // y emitir cantos/piezas: pared / muebles_altos / copete / frontal /
    // zocalo (descartan pulido y generan piezas si Claude las omite),
    // pulido / inglete (autoritativos para emitir cantos) y pilar
    // (verificación de muesca en la encimera).
    const tipos_con_puntos = new Set(
        ['pared', 'muebles_altos', 'copete', 'pulido', 'inglete',
         'frontal', 'zocalo', 'pilar', 'hueco', 'costado']);
    const persistir_puntos = t.tipo === 'poly'
        || tipos_con_puntos.has(t.tipo_pieza);
    if (persistir_puntos) {
      // Submuestreo: si son demasiados puntos (mano alzada larga), uno cada N
      const todos = t.puntos.map(p => [Math.round(p[0]), Math.round(p[1])]);
      const max_puntos = 400;
      let pts = todos;
      if (todos.length > max_puntos) {
        const step = Math.ceil(todos.length / max_puntos);
        pts = todos.filter((_, k) => k % step === 0);
        if (pts[pts.length - 1] !== todos[todos.length - 1]) {
          pts.push(todos[todos.length - 1]);
        }
      }
      meta.puntos = pts;
      if (t.tipo === 'poly') meta.cerrado = !!t.cerrado;
    }
    return meta;
  });
  let r;
  try {
    r = await fetch('/api/guardar_pagina', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        ruta: RUTA_PROYECTO,
        pagina_id: p.id,
        overlay_png_b64: tieneTrazos ? overlay_b64 : '',
        etiquetas,
        tiene_trazos: tieneTrazos,
        canvas_w: canvas.width,
        canvas_h: canvas.height,
      }),
    });
  } catch (err) {
    alert('ERROR al guardar la página (servidor caído o red): ' + err +
          '\nLas anotaciones de esta página NO se han guardado.');
    return false;
  }
  if (!r.ok) {
    let detalle = '';
    try { detalle = (await r.json()).error || ''; } catch (_) {}
    alert('ERROR al guardar la página (HTTP ' + r.status + '): ' + detalle +
          '\nLas anotaciones de esta página NO se han guardado.');
    return false;
  }
  return true;
}

async function guardarNotas() {
  const notas = document.getElementById('notasGlobales').value.split('\n').map(s => s.trim()).filter(Boolean);
  await fetch('/api/notas_globales', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ ruta: RUTA_PROYECTO, notas }),
  });
}

document.getElementById('btnSkip').onclick = async () => {
  await guardarNotas();
  const ok = await guardarPaginaActual(false);
  if (!ok) return;  // no avanzar si falló el guardado
  cargarPagina(idxActual + 1);
};

document.getElementById('btnSave').onclick = async () => {
  const tiene = trazos.length > 0;
  await guardarNotas();
  if (tiene) {
    const ok = await guardarPaginaActual(true);
    if (!ok) return;  // no avanzar si falló el guardado
    document.querySelectorAll('.pag-item')[idxActual].classList.add('anotada');
  }
  cargarPagina(idxActual + 1);
};

// Notas globales iniciales
document.getElementById('notasGlobales').value = (ANOT.notas_globales || []).join('\n');

// Cargar primera página
cargarPagina(0);
</script>
</body>
</html>
"""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=5050)
    p.add_argument("--host", default="127.0.0.1")
    args = p.parse_args()
    print(f"Anotador de planos: http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
