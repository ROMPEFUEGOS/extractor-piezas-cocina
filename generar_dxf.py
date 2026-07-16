#!/usr/bin/env python3
"""
Generador de DXF a partir del JSON de extracción de piezas.
Produce un DXF con todas las piezas dibujadas como rectángulos,
listas para ser acotadas por dxf_auto_dim_v1.3.py.

Uso:
    python generar_dxf.py <extraccion.json>
    python generar_dxf.py <extraccion.json> -o salida.dxf
"""
import argparse
import json
import math
import sys
from pathlib import Path

import ezdxf
from ezdxf.enums import TextEntityAlignment

# ── CONFIGURACIÓN ────────────────────────────────────────────────────────────
GAP = 300          # separación entre piezas (mm)
MARGEN = 200       # margen exterior del dibujo (mm)
ALTURA_TEXTO = 60  # altura del texto de etiqueta (mm)
ALTURA_TEXTO_DIM = 40  # altura de texto de dimensiones secundarias

# Tamaños estándar de huecos (mm) — si no se especifican en el JSON
HUECOS_STD = {
    'placa':       (590, 520),   # placa inducción 4 zonas
    'placa2':      (290, 520),   # placa 2 zonas
    'fregadero':   (400, 400),   # fregadero estándar 1 seno
    'fregadero2':  (780, 480),   # fregadero 2 senos
    'grifo':       (35, 35),     # taladro grifo
    'enchufe':     (68, 68),     # hueco enchufe
    'dosificador': (35, 35),     # taladro dosificador
}

# Capas DXF y sus colores (índice de color AutoCAD)
CAPAS = {
    'ENCIMERA':  {'color': 3},   # verde
    'FRONTAL':   {'color': 4},   # cian
    'COSTADO':   {'color': 5},   # azul
    'COPETE':    {'color': 6},   # magenta
    'ZOCALO':    {'color': 2},   # amarillo
    'PILASTRA':  {'color': 30},  # naranja
    'HUECOS':    {'color': 1},   # rojo
    'PULIDO':    {'color': 4},   # cian (canto pulido visible)
    'INGLETE':   {'color': 6},   # magenta (unión inglete)
    'TEXTO':     {'color': 7},   # blanco/negro
    'COTAS':     {'color': 3},   # verde (para auto-dim)
    'MARCO':     {'color': 8},   # gris
}

TIPO_A_CAPA = {
    'encimera': 'ENCIMERA',
    'frontal':  'FRONTAL',
    'costado':  'COSTADO',
    'copete':   'COPETE',
    'zocalo':   'ZOCALO',
    'pilastra': 'PILASTRA',
}


def crear_doc_dxf() -> ezdxf.document.Drawing:
    doc = ezdxf.new('R2010')
    doc.header['$INSUNITS'] = 4  # mm
    doc.header['$MEASUREMENT'] = 1  # métrico
    for nombre, attrs in CAPAS.items():
        if nombre not in doc.layers:
            doc.layers.new(nombre, dxfattribs=attrs)
    return doc


def añadir_rect(msp, x: float, y: float, w: float, h: float, capa: str):
    """Dibuja un rectángulo cerrado en la capa indicada."""
    pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    msp.add_lwpolyline(pts, close=True, dxfattribs={'layer': capa})


def añadir_poligono(msp, vertices: list, x_off: float, y_off: float, capa: str,
                    h_total: float = 0.0):
    """Dibuja el polígono trasladado por (x_off, y_off) en la capa indicada.

    Invierte Y (h_total - vy) para que la orientación del DXF coincida con la
    orientación cenital del plano original (Y crece hacia arriba en CAD pero
    los planos suelen leerse con la pared 'arriba' y el frente 'abajo').
    """
    pts = [(v[0] + x_off, (h_total - v[1]) + y_off) for v in vertices]
    msp.add_lwpolyline(pts, close=True, dxfattribs={'layer': capa})


def añadir_etiqueta(msp, cx: float, cy: float, lineas: list[str],
                    altura: float = ALTURA_TEXTO, capa: str = 'TEXTO'):
    """Escribe varias líneas de texto centradas en (cx, cy)."""
    total_h = len(lineas) * altura * 1.4
    y_start = cy + total_h / 2 - altura * 0.7
    for i, linea in enumerate(lineas):
        y = y_start - i * altura * 1.4
        txt = msp.add_text(linea, dxfattribs={'layer': capa, 'height': altura})
        txt.set_placement((cx, y), align=TextEntityAlignment.MIDDLE_CENTER)


def dimensiones_pieza(pieza: dict) -> tuple[float, float]:
    """Extrae (largo, ancho/alto) de una pieza en mm."""
    largo = pieza.get('largo_mm') or 0.0
    ancho = pieza.get('ancho_mm') or pieza.get('altura_mm') or 0.0
    if not largo and pieza.get('longitud_ml'):
        largo = pieza['longitud_ml'] * 1000
        ancho = pieza.get('altura_mm') or 100.0
    return float(largo), float(ancho)


def agrupar_piezas_por_material(datos: dict) -> dict:
    """
    Agrupa las piezas por material_rol.
    Si hay opciones alternativas (opcion1, opcion2…) con la misma geometría,
    las fusiona en un único grupo con etiqueta combinada para evitar duplicados en el DXF.
    Devuelve {nombre_grupo: [piezas]}.
    """
    materiales = {m['rol']: m for m in datos.get('materiales', [])}

    # 1. Agrupar piezas por rol
    por_rol: dict[str, list] = {}
    for pieza in datos.get('piezas', []):
        rol = pieza.get('material_rol', '')
        por_rol.setdefault(rol, []).append(pieza)

    # 2. Detectar grupos de opciones alternativas (misma geometría, distinto material)
    #    Identificamos roles con sufijo _opcionN o _opcion_N
    import re as _re
    base_a_roles: dict[str, list[str]] = {}
    for rol in por_rol:
        base = _re.sub(r'_opcion\w*$', '', rol, flags=_re.IGNORECASE)
        base_a_roles.setdefault(base, []).append(rol)

    grupos_finales: dict[str, list] = {}

    for base, roles in base_a_roles.items():
        if len(roles) == 1:
            # Sin alternativas — grupo normal
            rol = roles[0]
            mat = materiales.get(rol, {})
            marca = mat.get('marca', '?')
            color = mat.get('color', rol)
            grosor = mat.get('grosor_cm', '')
            clave = f"{marca} {color} {grosor}cm"
            grupos_finales.setdefault(clave, []).extend(por_rol[rol])
        else:
            # Múltiples opciones — verificar si tienen la misma geometría
            piezas_ref = por_rol[roles[0]]
            geometria_igual = all(
                _misma_geometria(por_rol[roles[0]], por_rol[r]) for r in roles[1:]
            )
            if geometria_igual:
                # Fusionar: usar piezas del primer rol, etiquetar con todas las opciones
                # Clave = material principal de la opción 1 (no de cada sub-rol)
                mat0 = materiales.get(roles[0], {})
                # Buscar el material raíz de la opción (marca/color del encimera_opcionN)
                marca0 = mat0.get('marca') or materiales.get(
                    mat0.get('es_igual_a', ''), {}).get('marca', '?')
                color0 = mat0.get('color') or materiales.get(
                    mat0.get('es_igual_a', ''), {}).get('color', roles[0])
                # Construir etiqueta combinada de todos los materiales de las opciones
                nombres_opciones = []
                for r in roles:
                    mat = materiales.get(r, {})
                    m = mat.get('marca') or '?'
                    c = mat.get('color') or r
                    g = mat.get('grosor_cm', '')
                    nombres_opciones.append(f"{m} {c} {g}cm")
                # Quitar duplicados manteniendo orden
                seen = set(); uniq = []
                for n in nombres_opciones:
                    if n not in seen:
                        seen.add(n); uniq.append(n)
                clave = ' / '.join(uniq)
                piezas_fusionadas = []
                for p in piezas_ref:
                    p2 = dict(p)
                    p2['_opciones'] = uniq
                    piezas_fusionadas.append(p2)
                grupos_finales.setdefault(clave, []).extend(piezas_fusionadas)
            else:
                # Geometría diferente entre opciones — dibujar por separado
                for rol in roles:
                    mat = materiales.get(rol, {})
                    clave = f"{mat.get('marca','?')} {mat.get('color',rol)} {mat.get('grosor_cm','')}cm"
                    grupos_finales.setdefault(clave, []).extend(por_rol[rol])

    return grupos_finales


def _misma_geometria(piezas_a: list, piezas_b: list) -> bool:
    """Comprueba si dos listas de piezas tienen la misma geometría (largo × ancho)."""
    if len(piezas_a) != len(piezas_b):
        return False
    dims_a = sorted(dimensiones_pieza(p) for p in piezas_a)
    dims_b = sorted(dimensiones_pieza(p) for p in piezas_b)
    return all(
        abs(a[0] - b[0]) < 1 and abs(a[1] - b[1]) < 1
        for a, b in zip(dims_a, dims_b)
    )


def colocar_piezas(piezas: list[dict], x_origen: float, y_origen: float
                   ) -> list[dict]:
    """
    Coloca las piezas en filas ordenadas por largo (descendente).
    Devuelve lista de dicts con 'pieza', 'x', 'y', 'w', 'h'.
    """
    # Ordenar por área descendente (piezas grandes primero)
    piezas_dims = []
    for p in piezas:
        w, h = dimensiones_pieza(p)
        if w > 0 and h > 0:
            piezas_dims.append({'pieza': p, 'w': w, 'h': h})
        else:
            print(f"  [WARN] Pieza SIN dimensiones, no se dibuja en el DXF: "
                  f"{p.get('tipo', '?')} · {p.get('zona', '?')}", file=sys.stderr)
    piezas_dims.sort(key=lambda x: x['w'] * x['h'], reverse=True)

    colocadas = []
    x, y = x_origen, y_origen
    fila_h = 0  # altura máxima de la fila actual
    MAX_ANCHO_FILA = 8000  # max mm antes de saltar de fila

    for item in piezas_dims:
        w, h = item['w'], item['h']
        if x > x_origen and (x + w - x_origen) > MAX_ANCHO_FILA:
            # Nueva fila
            y += fila_h + GAP
            x = x_origen
            fila_h = 0
        colocadas.append({**item, 'x': x, 'y': y})
        fila_h = max(fila_h, h)
        x += w + GAP

    return colocadas


HUECOS_DEFAULT = {
    'placa':     (562, 492),
    'fregadero': (500, 400),
    'grifo':     (35, 35),
    'enchufe':   (68, 68),
    'dosificador': (35, 35),
}


def _zona_key(zona: str) -> str:
    """Prefijo significativo de la zona usado como clase de equivalencia para
    asignar cantos/huecos a piezas (las opciones de material con la misma
    geometría comparten clave y reciben los mismos cantos)."""
    return (zona or '').lower().split('(')[0].strip()[:35]


def _asignar_cantos_y_huecos(datos: dict) -> tuple:
    """Asigna cada canto y cada hueco a UNA sola zona de pieza.

    Para cada canto, entre todas las piezas encimera/isla cuya zona-clave
    aparece en sus notas, gana la clave MÁS LARGA (la más específica): así
    "encimera" no absorbe los cantos de "encimera derecha". Huecos: ídem por
    `pieza_zona`; si viene vacío se asignan a la encimera de mayor área con
    aviso. Devuelve (canto_owner, hueco_owner) mapeando id(obj) → zona_key.
    """
    piezas_cand = [p for p in datos.get('piezas', [])
                   if p.get('tipo') in ('encimera', 'isla')
                   and p.get('vertices_mm') and len(p['vertices_mm']) >= 3]
    claves = {_zona_key(p.get('zona')) for p in piezas_cand if _zona_key(p.get('zona'))}

    canto_owner = {}
    for c in datos.get('cantos', []) or []:
        notas = (c.get('notas') or '').lower()
        if not notas:
            continue
        match = [k for k in claves if k in notas]
        if match:
            canto_owner[id(c)] = max(match, key=len)
        else:
            print(f"  [WARN] Canto sin pieza asignable (zona no reconocida): "
                  f"{c.get('tipo')} {c.get('longitud_ml')}ml · {c.get('notas', '')[:60]}",
                  file=sys.stderr)

    def _area_pieza(p):
        vs = p['vertices_mm']
        return abs(_signed_area(vs))

    mayor = max(piezas_cand, key=_area_pieza) if piezas_cand else None
    hueco_owner = {}
    for h in datos.get('huecos', []) or []:
        if h.get('centro_x_mm') is None or h.get('centro_y_mm') is None:
            continue
        pza_z = (h.get('pieza_zona') or '').lower().strip()
        if pza_z:
            match = [k for k in claves if k in pza_z or pza_z in k]
            if match:
                hueco_owner[id(h)] = max(match, key=len)
            else:
                print(f"  [WARN] Hueco '{h.get('tipo')}' con pieza_zona "
                      f"'{pza_z}' que no matchea ninguna pieza — no se dibuja",
                      file=sys.stderr)
        elif mayor is not None:
            hueco_owner[id(h)] = _zona_key(mayor.get('zona'))
            print(f"  [WARN] Hueco '{h.get('tipo')}' sin pieza_zona — asignado "
                  f"a la encimera mayor ({mayor.get('zona', '?')})", file=sys.stderr)
    return canto_owner, hueco_owner


def _dibujar_huecos_pieza(msp, datos: dict, pieza: dict, x_off: float, y_off: float,
                          h_total: float, hueco_owner: dict = None):
    """Dibuja los huecos de una pieza usando centro_x_mm/centro_y_mm reales,
    con la misma inversión Y que el polígono. Cada hueco se dibuja SOLO en la
    pieza a la que fue asignado por _asignar_cantos_y_huecos."""
    zona_pza = _zona_key(pieza.get('zona'))
    for h in datos.get('huecos', []):
        cx = h.get('centro_x_mm')
        cy = h.get('centro_y_mm')
        if cx is None or cy is None:
            continue
        if hueco_owner is not None and hueco_owner.get(id(h)) != zona_pza:
            continue
        tipo = h.get('tipo', 'hueco')
        if tipo == 'enchufe':
            # Los enchufes van en el chapeado (pared), nunca cortados en la
            # encimera — se listan en el cajetín pero no se dibujan aquí
            continue
        hw = h.get('largo_mm') or HUECOS_DEFAULT.get(tipo, (100, 100))[0]
        hh = h.get('ancho_mm') or HUECOS_DEFAULT.get(tipo, (100, 100))[1]
        # Coords con inversión Y (igual que polígono)
        cx_dxf = cx + x_off
        cy_dxf = (h_total - cy) + y_off
        x0 = cx_dxf - hw / 2
        y0 = cy_dxf - hh / 2
        añadir_rect(msp, x0, y0, hw, hh, 'HUECOS')
        añadir_etiqueta(msp, cx_dxf, cy_dxf,
                        [tipo.upper(), f'{int(hw)}×{int(hh)}'],
                        max(20, ALTURA_TEXTO * 0.6))


def _signed_area(verts: list) -> float:
    s = 0.0
    n = len(verts)
    for i in range(n):
        x1, y1 = verts[i]
        x2, y2 = verts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def _aristas_poligono(verts: list, x_off: float, y_off: float, h_total: float) -> list:
    """Devuelve la lista de aristas (segmentos) del polígono ya transformados
    al sistema de coordenadas del DXF. La clasificación pared/frontal la hace
    el postprocesador del extractor (con las anotaciones del operador); aquí
    solo se necesita la geometría para dibujar los cantos por idx/longitud."""
    n = len(verts)
    if n < 3:
        return []
    aristas = []
    for i in range(n):
        x1, y1 = verts[i]
        x2, y2 = verts[(i + 1) % n]
        p1 = (x1 + x_off, (h_total - y1) + y_off)
        p2 = (x2 + x_off, (h_total - y2) + y_off)
        L = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        mid = ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)
        aristas.append({'p1': p1, 'p2': p2, 'len': L, 'mid': mid, 'idx': i,
                        'v1': i, 'v2': (i + 1) % n})
    return aristas


def _dibujar_cantos_pieza(msp, datos: dict, pieza: dict, x_off: float, y_off: float,
                          h_total: float, canto_owner: dict = None):
    """Dibuja recto_pulido (cian) e ingletado (magenta) sobre las aristas del
    polígono. Cada canto se dibuja SOLO en la pieza a la que fue asignado por
    _asignar_cantos_y_huecos (el match de zona más específico gana). Para los
    pulidos, intenta extraer el `idx` exacto de las notas; si no, cae a
    matching por longitud entre las aristas no usadas. Los ingletes se
    distribuyen en las esquinas adyacentes a aristas pulidas, una a una.
    """
    import re as _re
    verts = pieza.get('vertices_mm')
    if not verts or len(verts) < 3:
        return
    aristas = _aristas_poligono(verts, x_off, y_off, h_total)
    if pieza.get('tipo') not in ('encimera', 'isla'):
        return
    n = len(aristas)
    cantos = datos.get('cantos', []) or []

    zona_pza = _zona_key(pieza.get('zona'))
    def _del_pieza(c):
        if canto_owner is None:
            return False
        return canto_owner.get(id(c)) == zona_pza

    pulidos = [c for c in cantos if c.get('tipo', '').startswith('recto_pulido') and _del_pieza(c)]
    ingletes = [c for c in cantos if c.get('tipo') == 'ingletado' and _del_pieza(c)]

    # 1) Pulidos: si el canto trae "arista idx=N" en sus notas, usa esa arista
    # directamente (la fuente más fiable, ya que el postproc emite así). Si
    # no, cae a matching por longitud entre las aristas no usadas.
    aristas_pulido = []
    libres = list(range(n))
    re_idx = _re.compile(r'arista\s+idx\s*=\s*(\d+)', _re.IGNORECASE)
    for c in pulidos:
        m = re_idx.search(c.get('notas') or '')
        idx = None
        if m:
            try:
                ii = int(m.group(1))
                if 0 <= ii < n and ii in libres:
                    idx = ii
                elif ii >= n:
                    print(f"  [WARN] Canto con arista idx={ii} pero el polígono "
                          f"de '{pieza.get('zona', '?')}' tiene {n} aristas",
                          file=sys.stderr)
            except ValueError:
                pass
        if idx is None:
            # Fallback por longitud
            L_target = (c.get('longitud_ml') or 0) * 1000.0
            if L_target <= 0 or not libres:
                print(f"  [WARN] Canto NO dibujado en '{pieza.get('zona', '?')}' "
                      f"(sin arista libre ni longitud): {c.get('tipo')} "
                      f"{c.get('longitud_ml')}ml · {(c.get('notas') or '')[:60]}",
                      file=sys.stderr)
                continue
            idx = min(libres, key=lambda i: abs(aristas[i]['len'] - L_target))
        a = aristas[idx]
        msp.add_line(a['p1'], a['p2'],
                     dxfattribs={'layer': 'PULIDO', 'lineweight': 50})
        añadir_etiqueta(msp, a['mid'][0], a['mid'][1] - ALTURA_TEXTO * 0.6,
                        [f"PULIDO {c.get('longitud_ml'):.3g}m"],
                        max(20, ALTURA_TEXTO * 0.5), 'PULIDO')
        if idx in libres:
            libres.remove(idx)
        aristas_pulido.append(idx)

    # 2) Ingletes: trazo corto a 45° en una esquina adyacente a un pulido.
    # Solo emitimos en esquinas reales (entre dos aristas pulidas) cuando hay
    # ingletes para esta pieza.
    usados_corner = set()
    for c in ingletes:
        if not aristas_pulido:
            break
        # Preferimos esquinas entre dos aristas pulidas (= unión típica
        # cabeza-frontal); si no hay, cualquier esquina de pulido libre.
        esquinas_pulido_pulido = []
        esquinas_simples = []
        for i in aristas_pulido:
            v_left = aristas[i]['idx']
            v_right = (aristas[i]['idx'] + 1) % n
            for v_idx in (v_left, v_right):
                if v_idx in usados_corner:
                    continue
                # Las dos aristas que tocan v_idx
                a_prev = (v_idx - 1) % n
                a_next = v_idx
                if a_prev in aristas_pulido and a_next in aristas_pulido:
                    esquinas_pulido_pulido.append(v_idx)
                else:
                    esquinas_simples.append(v_idx)
        candidatos = esquinas_pulido_pulido or esquinas_simples
        if not candidatos:
            break
        v_idx = candidatos[0]
        usados_corner.add(v_idx)
        x_v, y_v = verts[v_idx]
        cx = x_v + x_off
        cy = (h_total - y_v) + y_off
        L_seg = max(60, min(200, (c.get('longitud_ml') or 0.3) * 1000.0 * 0.3))
        xs = [v[0] for v in verts]
        ys = [v[1] for v in verts]
        cx_poly = sum(xs) / len(xs) + x_off
        cy_poly = (h_total - sum(ys) / len(ys)) + y_off
        dx_in = cx_poly - cx
        dy_in = cy_poly - cy
        norm = math.hypot(dx_in, dy_in) or 1
        ux, uy = dx_in / norm, dy_in / norm
        msp.add_line((cx, cy), (cx + ux * L_seg, cy + uy * L_seg),
                     dxfattribs={'layer': 'INGLETE', 'lineweight': 50})
        añadir_etiqueta(msp, cx + ux * L_seg * 1.3, cy + uy * L_seg * 1.3,
                        [f"INGLETE {c.get('longitud_ml'):.3g}m"],
                        max(20, ALTURA_TEXTO * 0.5), 'INGLETE')


def añadir_huecos_isla(msp, datos: dict, x_isla: float, y_isla: float,
                       w_isla: float, h_isla: float):
    """
    Añade los huecos de placa, fregadero y grifo en la encimera de isla.
    Los posiciona de forma estándar si no hay coordenadas exactas.
    """
    huecos = datos.get('huecos', [])
    placa_añadida = False
    freg_añadida = False

    # Zona trasera (mitad del fondo) y delantera para los huecos
    margen_lat = 100
    margen_prof_trasero = 60
    margen_prof_delantero = 60

    x_cursor = x_isla + margen_lat

    for h in huecos:
        tipo = h.get('tipo', '')
        cant = h.get('cantidad', 1)

        if tipo == 'placa' and not placa_añadida:
            hw, hh = HUECOS_STD['placa']
            # Placa centrada en largo, en la mitad trasera
            hx = x_isla + (w_isla - hw) / 2
            hy = y_isla + h_isla - hh - margen_prof_trasero
            añadir_rect(msp, hx, hy, hw, hh, 'HUECOS')
            añadir_etiqueta(msp, hx + hw / 2, hy + hh / 2,
                            ['PLACA', f'{hw}×{hh}'], ALTURA_TEXTO * 0.7)
            placa_añadida = True

        elif tipo == 'fregadero' and not freg_añadida:
            fw, fh = HUECOS_STD['fregadero']
            subtipo = h.get('subtipo', 'sobre_encimera')
            # Fregadero en la parte izquierda, lado delantero
            fx = x_isla + margen_lat
            fy = y_isla + margen_prof_delantero
            añadir_rect(msp, fx, fy, fw, fh, 'HUECOS')
            lbl = 'FREG.SOBRE' if 'sobre' in subtipo else 'FREG.BAJO'
            añadir_etiqueta(msp, fx + fw / 2, fy + fh / 2,
                            [lbl, f'{fw}×{fh}'], ALTURA_TEXTO * 0.7)
            freg_añadida = True

            # Grifo: taladro junto al fregadero
            gx = fx + fw + 80
            gy = fy + fh / 2 - 17
            añadir_rect(msp, gx, gy, 35, 35, 'HUECOS')
            añadir_etiqueta(msp, gx + 17, gy + 17, ['G'], ALTURA_TEXTO * 0.5)


def generar_dxf(json_path: Path, output_path: Path):
    with open(json_path, encoding='utf-8') as f:
        datos = json.load(f)

    job_id = datos.get('job_id', 'J????')
    cliente = datos.get('cliente', '')
    tienda = datos.get('tienda', '')
    vendedor = datos.get('vendedor', '')
    fecha = datos.get('fecha', '')
    materiales_info = {m['rol']: m for m in datos.get('materiales', [])}

    doc = crear_doc_dxf()
    msp = doc.modelspace()

    # ── Agrupar piezas por material ──────────────────────────────────────
    grupos = agrupar_piezas_por_material(datos)

    # Asignación única canto→pieza y hueco→pieza (evita duplicados entre
    # piezas con zonas que son prefijo unas de otras)
    canto_owner, hueco_owner = _asignar_cantos_y_huecos(datos)

    y_grupo = MARGEN
    isla_info = None  # guardar info de la isla para huecos

    for nombre_grupo, piezas_grupo in grupos.items():
        # Cabecera de grupo
        añadir_etiqueta(msp, MARGEN, y_grupo + 30,
                        [f'▶ {nombre_grupo}'],
                        ALTURA_TEXTO * 1.2, 'MARCO')

        y_grupo += ALTURA_TEXTO * 2
        colocadas = colocar_piezas(piezas_grupo, MARGEN, y_grupo)

        for item in colocadas:
            p = item['pieza']
            x, y, w, h = item['x'], item['y'], item['w'], item['h']
            tipo = p.get('tipo', 'encimera')
            capa = TIPO_A_CAPA.get(tipo, 'ENCIMERA')

            verts = p.get('vertices_mm')
            if verts and len(verts) >= 3:
                xs = [v[0] for v in verts]
                ys = [v[1] for v in verts]
                x_min, y_min, y_max = min(xs), min(ys), max(ys)
                h_poly = y_max - y_min
                añadir_poligono(msp, verts, x - x_min, y - y_min, capa, h_total=h_poly + 2 * y_min)
                # Dibujar huecos y cantos pertenecientes a esta pieza
                if tipo in ('encimera', 'isla'):
                    _dibujar_huecos_pieza(msp, datos, p, x - x_min, y - y_min,
                                          h_poly + 2 * y_min, hueco_owner)
                    _dibujar_cantos_pieza(msp, datos, p, x - x_min, y - y_min,
                                          h_poly + 2 * y_min, canto_owner)
            else:
                añadir_rect(msp, x, y, w, h, capa)

            # Etiqueta interior
            zona = p.get('zona', '')
            lineas_etq = [
                tipo.upper(),
                f'{w:.0f} x {h:.0f} mm',
            ]
            if zona:
                zona_corta = zona[:35] + '...' if len(zona) > 35 else zona
                lineas_etq.append(zona_corta)
            if p.get('notas'):
                nota_corta = p['notas'][:40] + '...' if len(p['notas']) > 40 else p['notas']
                lineas_etq.append(nota_corta)
            # Si es pieza fusionada de varias opciones, indicarlo
            if p.get('_opciones'):
                lineas_etq.append('(valido para ambas opciones)')

            alt_texto = min(ALTURA_TEXTO, h * 0.12, w * 0.04)
            alt_texto = max(alt_texto, 25)
            añadir_etiqueta(msp, x + w / 2, y + h / 2, lineas_etq, alt_texto)

            # (legacy isla_info: solo si NINGÚN hueco tiene centro_mm; en ese
            # caso el código antiguo `añadir_huecos_isla` coloca placa+fregadero
            # con posiciones estándar. Si hay centros reales, no lo usamos.)
            tiene_centros = any(h.get('centro_x_mm') is not None
                                and h.get('centro_y_mm') is not None
                                for h in datos.get('huecos', []))
            if (tipo in ('encimera', 'isla') and 'isla' in (zona or '').lower()
                    and not tiene_centros):
                if isla_info is None or w > isla_info['w_isla']:
                    isla_info = {'x_isla': x, 'y_isla': y, 'w_isla': w, 'h_isla': h}

        # Calcular altura máxima de este grupo de piezas
        if colocadas:
            max_y = max(item['y'] + item['h'] for item in colocadas)
            y_grupo = max_y + GAP * 2
        else:
            y_grupo += GAP * 2

    # ── Añadir huecos en la isla ─────────────────────────────────────────
    if isla_info:
        añadir_huecos_isla(msp, datos, **isla_info)

    # ── Cajetín / carátula — solo TEXT, sin rectángulo (evita que auto-dim lo detecte) ──
    cajetin_x = MARGEN
    cajetin_y = y_grupo + MARGEN

    # Huecos/elaboraciones resumen
    huecos_txt = []
    for h in datos.get('huecos', []):
        s = f"{h['cantidad']}x {h['tipo'].upper()}"
        if h.get('subtipo'):
            s += f"({h['subtipo']})"
        huecos_txt.append(s)

    # Cantos resumen
    cantos_txt = []
    for c in datos.get('cantos', []):
        if c.get('longitud_ml'):
            cantos_txt.append(f"{c['tipo']}: {c['longitud_ml']:.2f}ml")

    lineas_cajetin = [
        f'TRABAJO: {job_id} -- {cliente}  |  {tienda} / {vendedor}  |  Fecha: {fecha}',
        f'Materiales: {", ".join(grupos.keys())}',
        'Huecos: ' + '  |  '.join(huecos_txt) if huecos_txt else '',
        'Cantos: ' + '  |  '.join(cantos_txt) if cantos_txt else '',
    ]

    for i, linea in enumerate(l for l in lineas_cajetin if l):
        y_txt = cajetin_y + (len(lineas_cajetin) - i) * 80
        txt = msp.add_text(linea, dxfattribs={'layer': 'MARCO', 'height': 55})
        txt.set_placement((cajetin_x, y_txt), align=TextEntityAlignment.LEFT)

    # ── Guardar ──────────────────────────────────────────────────────────
    doc.saveas(str(output_path))
    print(f"  DXF generado: {output_path}")

    # ── Resumen de piezas en consola ─────────────────────────────────────
    print(f"\n  PIEZAS GENERADAS ({job_id} — {cliente}):")
    for nombre_grupo, piezas_grupo in grupos.items():
        print(f"\n  [{nombre_grupo}]")
        for p in piezas_grupo:
            w, h = dimensiones_pieza(p)
            tipo = p.get('tipo', '?')
            zona = p.get('zona', '')
            print(f"    {tipo.upper():10s}  {w:.0f} × {h:.0f} mm   {zona}")


def main():
    parser = argparse.ArgumentParser(description='Genera DXF desde JSON de extracción')
    parser.add_argument('json', help='Ruta al archivo _extraccion.json')
    parser.add_argument('-o', '--output', help='Ruta de salida del .dxf (opcional)')
    args = parser.parse_args()

    json_path = Path(args.json)
    if not json_path.exists():
        print(f"[ERROR] No existe: {json_path}")
        sys.exit(1)

    if args.output:
        output_path = Path(args.output)
    else:
        output_path = json_path.parent / (json_path.stem.replace('_extraccion', '') + '.dxf')

    generar_dxf(json_path, output_path)


if __name__ == '__main__':
    main()
