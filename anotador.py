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

# Roots por defecto: las 3 carpetas conocidas
ROOTS = [
    Path("/home/kecojones/Documents/ProgramaDeAcotacionesDXF/Cocimoble2025"),
    Path("/home/kecojones/Documents/ProgramaDeAcotacionesDXF/Cocimoble2026"),
    Path("/home/kecojones/Documents/ProgramaDeAcotacionesDXF/ACyC Accesorios y cocinas"),
]

PALETA = [
    {"id": "encimera", "color": "#FFD966", "label": "encimera", "tecla": "1"},
    {"id": "frontal",  "color": "#5B9BD5", "label": "frontal/chapeado", "tecla": "2"},
    {"id": "zocalo",   "color": "#9B7BC9", "label": "zócalo/rodapié", "tecla": "3"},
    {"id": "copete",   "color": "#F4A460", "label": "copete", "tecla": "4"},
    {"id": "costado",  "color": "#70AD47", "label": "costado/cascada", "tecla": "5"},
    {"id": "pilar",    "color": "#A0522D", "label": "pilar", "tecla": "6"},
    {"id": "hueco",    "color": "#E63946", "label": "hueco", "tecla": "7"},
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
                from pdf2image import convert_from_path  # noqa
                # Solo contamos páginas (rapida)
                import pypdf
                reader = pypdf.PdfReader(str(archivo))
                n_paginas = len(reader.pages)
            except Exception:
                # Fallback: pdf2image
                try:
                    pages = pdf_pages_to_base64(archivo, dpi=72, max_pages=20)
                    n_paginas = len(pages)
                except Exception:
                    n_paginas = 0
            for i in range(1, min(n_paginas, 20) + 1):
                paginas.append({
                    "id": f"pdf::{archivo.name}::{i}",
                    "fuente": archivo.name,
                    "tipo": "pdf",
                    "pagina": i,
                    "total_paginas": n_paginas,
                })
    return paginas


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
        # Renderizar la página específica con dpi razonable
        pages = pdf_pages_to_base64(archivo, dpi=140, max_pages=20)
        if pagina_num <= len(pages):
            data_b64, mt = pages[pagina_num - 1][0], pages[pagina_num - 1][1]
            return base64.b64decode(data_b64), mt
    raise ValueError(f"Página inválida: {pagina_id}")


def cargar_anotaciones(folder: Path) -> dict:
    f = folder / "anotaciones.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
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


@app.route("/proyecto/<path:ruta>")
def proyecto(ruta):
    folder = Path("/" + ruta)
    if not folder.exists() or not folder.is_dir():
        return "Proyecto no existe", 404
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
    data, mt = render_pagina(folder, pagina_id)
    return send_file(io.BytesIO(data), mimetype=mt)


@app.route("/api/guardar_pagina", methods=["POST"])
def api_guardar_pagina():
    body = request.get_json()
    folder = Path(body["ruta"])
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
        "fecha": datetime.now().isoformat(),
    }
    guardar_anotaciones(folder, anot)
    return jsonify({"ok": True})


@app.route("/api/notas_globales", methods=["POST"])
def api_notas_globales():
    body = request.get_json()
    folder = Path(body["ruta"])
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
.paleta { display: flex; flex-wrap: wrap; gap: 4px; }
.color-btn { width: 32px; height: 32px; border: 2px solid #555; border-radius: 4px;
              cursor: pointer; position: relative; }
.color-btn.active { border-color: #fff; transform: scale(1.1); }
.color-btn .key { position: absolute; bottom: -2px; right: -2px; font-size: 9px;
                   background: #000; color: #fff; padding: 0 3px; border-radius: 2px; }
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
        <div class="color-btn" style="background:{{ c.color }}" data-tipo="{{ c.id }}" title="{{ c.label }} (tecla {{ c.tecla }})">
          <span class="key">{{ c.tecla }}</span>
        </div>
        {% endfor %}
      </div>
    </div>
    <div class="section">
      <h3>Herramienta</h3>
      <button class="tool-btn active" data-tool="pen" title="(P)">Rotulador</button>
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
      <kbd>1-7</kbd> color · <kbd>P</kbd> pluma · <kbd>E</kbd> borrador<br>
      <kbd>Cmd+Z</kbd> deshacer · <kbd>←/→</kbd> ant/sig<br>
      <kbd>Espacio</kbd> guardar+sig · <kbd>Backspace</kbd> limpiar
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

let idxActual = 0;
let trazos = [];      // historial para deshacer: [{tipo:'pen'|'erase', puntos:[[x,y]...], color, grosor}]
let trazoActual = null;
let dibujando = false;
let tipoActual = "encimera";
let colorActual = PALETA[0].color;
let toolActual = "pen";
let grosor = 22;

const img = document.getElementById('imgPagina');
const canvas = document.getElementById('canvasOverlay');
const ctx = canvas.getContext('2d');

function dibujarTrazo(tr) {
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.lineWidth = tr.grosor;
  if (tr.tipo === 'pen') {
    ctx.globalCompositeOperation = 'source-over';
    ctx.strokeStyle = tr.color + '5A';   // alpha ~0.35 (5A hex)
  } else {
    ctx.globalCompositeOperation = 'destination-out';
    ctx.strokeStyle = 'rgba(0,0,0,1)';
  }
  ctx.beginPath();
  for (let i = 0; i < tr.puntos.length; i++) {
    const [x, y] = tr.puntos[i];
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.stroke();
}

function redibujar() {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  for (const tr of trazos) dibujarTrazo(tr);
}

function cargarPagina(idx) {
  idxActual = idx;
  if (idx < 0 || idx >= PAGINAS.length) {
    alert('Última página. Vuelve al listado de proyectos.');
    return;
  }
  const p = PAGINAS[idx];
  // Marcar item actual
  document.querySelectorAll('.pag-item').forEach((el, i) => {
    el.classList.toggle('actual', i === idx);
  });
  // Cargar imagen
  trazos = [];
  trazoActual = null;
  img.onload = () => {
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    canvas.style.width = img.clientWidth + 'px';
    canvas.style.height = img.clientHeight + 'px';
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

canvas.onpointerdown = e => {
  e.preventDefault();
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
canvas.onpointermove = e => {
  if (!dibujando) return;
  trazoActual.puntos.push(ptCanvas(e));
  // dibujar incremental
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.lineWidth = trazoActual.grosor;
  if (trazoActual.tipo === 'pen') {
    ctx.globalCompositeOperation = 'source-over';
    ctx.strokeStyle = trazoActual.color + '5A';
  } else {
    ctx.globalCompositeOperation = 'destination-out';
    ctx.strokeStyle = 'rgba(0,0,0,1)';
  }
  const n = trazoActual.puntos.length;
  ctx.beginPath();
  ctx.moveTo(trazoActual.puntos[n-2][0], trazoActual.puntos[n-2][1]);
  ctx.lineTo(trazoActual.puntos[n-1][0], trazoActual.puntos[n-1][1]);
  ctx.stroke();
};
canvas.onpointerup = e => {
  if (!dibujando) return;
  dibujando = false;
  if (trazoActual && trazoActual.puntos.length > 1) {
    trazos.push(trazoActual);
  }
  trazoActual = null;
};

// Paleta
document.querySelectorAll('.color-btn').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('.color-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    tipoActual = btn.dataset.tipo;
    const p = PALETA.find(p => p.id === tipoActual);
    colorActual = p.color;
    toolActual = 'pen';
    document.querySelectorAll('.tool-btn').forEach(b => b.classList.remove('active'));
    document.querySelector('.tool-btn[data-tool=pen]').classList.add('active');
  };
});
document.querySelector('.color-btn').classList.add('active');

// Herramientas
document.querySelectorAll('.tool-btn').forEach(btn => {
  btn.onclick = () => {
    const tool = btn.dataset.tool;
    if (tool === 'undo') { trazos.pop(); redibujar(); return; }
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
  // Color por número
  for (let i = 0; i < PALETA.length; i++) {
    if (e.key === PALETA[i].tecla) {
      document.querySelectorAll('.color-btn')[i].click();
      e.preventDefault(); return;
    }
  }
  if (e.key === 'p' || e.key === 'P') {
    document.querySelector('.tool-btn[data-tool=pen]').click(); e.preventDefault();
  } else if (e.key === 'e' || e.key === 'E') {
    document.querySelector('.tool-btn[data-tool=erase]').click(); e.preventDefault();
  } else if (e.key === 'z' && (e.metaKey || e.ctrlKey)) {
    trazos.pop(); redibujar(); e.preventDefault();
  } else if (e.key === 'ArrowRight') {
    document.getElementById('btnSkip').click(); e.preventDefault();
  } else if (e.key === 'ArrowLeft') {
    cargarPagina(Math.max(0, idxActual - 1)); e.preventDefault();
  } else if (e.key === ' ') {
    document.getElementById('btnSave').click(); e.preventDefault();
  } else if (e.key === 'Backspace') {
    if (confirm('Limpiar todos los trazos?')) { trazos = []; redibujar(); }
    e.preventDefault();
  }
});

async function guardarPaginaActual(tieneTrazos) {
  // Capturar overlay como PNG (canvas con transparencia)
  const overlay_b64 = canvas.toDataURL('image/png');
  const p = PAGINAS[idxActual];
  const etiquetas = trazos.map((t, i) => ({
    idx: i, tipo: t.tipo_pieza, color: t.color,
    n_puntos: t.puntos.length,
  }));
  const r = await fetch('/api/guardar_pagina', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      ruta: RUTA_PROYECTO,
      pagina_id: p.id,
      overlay_png_b64: tieneTrazos ? overlay_b64 : '',
      etiquetas,
      tiene_trazos: tieneTrazos,
    }),
  });
  return r.ok;
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
  await guardarPaginaActual(false);
  cargarPagina(idxActual + 1);
};

document.getElementById('btnSave').onclick = async () => {
  const tiene = trazos.length > 0;
  await guardarNotas();
  if (tiene) {
    await guardarPaginaActual(true);
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
