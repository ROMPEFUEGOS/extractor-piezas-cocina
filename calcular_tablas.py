"""
calcular_tablas.py — Calcula el número de tablas (slabs) necesarias para fabricar
las piezas de un trabajo de encimeras de cocina.

Algoritmo: shelf-packing (guillotina horizontal) con kerf de 5mm entre piezas.
Produce un informe legible y una representación ASCII del layout de cada tabla.

Uso:
    python calcular_tablas.py "/ruta/J0297_extraccion.json"
    python calcular_tablas.py "/ruta/carpeta_trabajo"  # busca el JSON automáticamente
"""

import json
import sys
import math
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Medidas estándar de tablas por fabricante (largo × alto en mm)
# ---------------------------------------------------------------------------
TABLAS_ESTANDAR = {
    # Porcelánico / Dekton / Ultra-compact
    "dekton":       [(3200, 1440)],
    "neolith":      [(3200, 1500), (3600, 1500)],
    "coverlam":     [(3200, 1000), (3200, 1600)],
    "laminam":      [(3000, 1000), (3000, 1500)],
    "ceratop":      [(3200, 1440)],
    "lapitec":      [(3000, 1440)],
    # Cuarzo engineered
    "silestone":    [(3040, 1440)],
    "compac":       [(3050, 1440)],
    "caesarstone":  [(3040, 1440)],
    "diresco":      [(3030, 1440)],
    "cosentino":    [(3040, 1440)],  # igual Silestone (misma empresa)
    "samsung":      [(3040, 1440)],
    # Piedra natural (granito, mármol, pizarra…) — variable; usamos media habitual
    "piedra_natural": [(3000, 1800), (2800, 1800), (2500, 1600)],
    # Guidoni (granito importado habitual)
    "guidoni":      [(3000, 1800)],
    # Por defecto para marcas desconocidas
    "default":      [(3000, 1800)],
}

# Marcas que se pueden rotar (engineered / porcelánico)
ROTAR_OK = {
    "dekton", "neolith", "coverlam", "laminam", "ceratop", "lapitec",
    "silestone", "compac", "caesarstone", "diresco", "cosentino", "samsung",
}

# Kerf (sierra): espacio mínimo entre piezas dentro de la misma tabla
KERF_MM = 5


# ---------------------------------------------------------------------------
# Normalización: obtener (largo_mm, alto_mm) de cualquier pieza del JSON
# ---------------------------------------------------------------------------
def dimensiones_pieza(pieza: dict) -> Optional[tuple[float, float]]:
    """
    Devuelve (largo, alto) en mm a partir de un dict de pieza.
    Devuelve None si no hay dimensiones suficientes.
    """
    tipo = pieza.get("tipo", "")
    l = pieza.get("largo_mm")
    a = pieza.get("ancho_mm")
    h = pieza.get("altura_mm")
    long_ml = pieza.get("longitud_ml")

    # Piezas horizontales (encimera, isla, costado, paso, tabica, otro)
    if l and a:
        return (float(l), float(a))
    # Piezas verticales (frontal/chapeado, pilastra)
    if l and h:
        return (float(l), float(h))
    # Zócalo / copete — dato en ml + altura
    if long_ml and h:
        return (float(long_ml) * 1000, float(h))
    # Sólo largo (caso raro — asumimos grosor mínimo)
    if l:
        return (float(l), 0)
    return None


# ---------------------------------------------------------------------------
# Lookup de medidas de tabla para un material
# ---------------------------------------------------------------------------
def tabla_para_material(marca: Optional[str]) -> list[tuple[int, int]]:
    if not marca:
        return TABLAS_ESTANDAR["default"]
    key = marca.lower().strip()
    # Búsqueda directa
    if key in TABLAS_ESTANDAR:
        return TABLAS_ESTANDAR[key]
    # Búsqueda parcial
    for k in TABLAS_ESTANDAR:
        if k in key or key in k:
            return TABLAS_ESTANDAR[k]
    return TABLAS_ESTANDAR["default"]


def puede_rotar(marca: Optional[str]) -> bool:
    if not marca:
        return False
    key = marca.lower().strip()
    for m in ROTAR_OK:
        if m in key or key in m:
            return True
    return False


# ---------------------------------------------------------------------------
# Algoritmo de packing: shelf (estantería horizontal)
# Guillotina simple: coloca piezas en filas horizontales.
# Dentro de cada fila va de izquierda a derecha hasta que no quepan más.
# ---------------------------------------------------------------------------

class Shelf:
    """Una fila horizontal de piezas dentro de una tabla."""
    def __init__(self, y_inicio: float, tabla_ancho: float):
        self.y = y_inicio          # posición Y dentro de la tabla
        self.altura = 0.0          # altura de la pieza más alta en la fila
        self.x_usado = 0.0         # cursor X
        self.tabla_ancho = tabla_ancho
        self.piezas_colocadas: list[dict] = []  # {label, w, h, x, y}

    def cabe(self, w: float, h: float) -> bool:
        espacio_x = self.tabla_ancho - self.x_usado - (KERF_MM if self.x_usado > 0 else 0)
        return w <= espacio_x

    def añadir(self, w: float, h: float, label: str) -> dict:
        x = self.x_usado + (KERF_MM if self.x_usado > 0 else 0)
        pos = {"label": label, "w": w, "h": h, "x": x, "y": self.y}
        self.piezas_colocadas.append(pos)
        self.x_usado = x + w
        self.altura = max(self.altura, h)
        return pos


class Tabla:
    """Una tabla (slab) con estantes horizontales."""
    def __init__(self, ancho: float, alto: float):
        self.ancho = ancho
        self.alto = alto
        self.shelves: list[Shelf] = []
        self.y_usado = 0.0

    def _shelf_actual(self) -> Optional[Shelf]:
        return self.shelves[-1] if self.shelves else None

    def añadir_pieza(self, w: float, h: float, label: str) -> bool:
        """Intenta colocar la pieza. Devuelve True si hubo sitio."""
        # Intentar en la shelf actual
        sh = self._shelf_actual()
        if sh and sh.cabe(w, h):
            sh.añadir(w, h, label)
            return True
        # Abrir nueva shelf
        y_nueva = self.y_usado + (KERF_MM if self.y_usado > 0 else 0) + (sh.altura if sh else 0)
        if y_nueva + h <= self.alto:
            nueva = Shelf(y_nueva, self.ancho)
            nueva.añadir(w, h, label)
            self.shelves.append(nueva)
            self.y_usado = y_nueva
            return True
        return False

    def area_usada(self) -> float:
        total = 0.0
        for sh in self.shelves:
            for p in sh.piezas_colocadas:
                total += p["w"] * p["h"]
        return total

    def aprovechamiento(self) -> float:
        area_total = self.ancho * self.alto
        return self.area_usada() / area_total * 100 if area_total > 0 else 0


LAVAVAJILLAS_ML = 600  # ml de rodapié estándar para hueco de lavavajillas


def split_rodapie(largo: float, ancho: float, label: str,
                   tabla_largo: int, tiene_lavavajillas: bool = True) -> list[tuple[float, float, str]]:
    """
    Rodapié/zócalo: cortes especiales.
    - Máximo 3 trozos.
    - Si hay lavavajillas (default True en cocinas), el rodapié del hueco
      del lavavajillas (~600mm) va separado → 3 trozos totales si el
      rodapié es > 4m aproximadamente.
    - Si no, corte simétrico (mitad) o en 3 si no cabe en 2.
    """
    if largo <= tabla_largo:
        return [(largo, ancho, label)]

    # Rodapié > tabla_largo → necesita partir
    n_min = math.ceil(largo / tabla_largo)
    n = min(3, max(2, n_min))

    if tiene_lavavajillas and largo > 4000 and n >= 2:
        # 3 trozos: izq + lavavajillas + der
        resto = largo - LAVAVAJILLAS_ML
        mitad = resto / 2
        if mitad <= tabla_largo and LAVAVAJILLAS_ML <= tabla_largo:
            return [
                (mitad, ancho, f"{label} (izq)"),
                (LAVAVAJILLAS_ML, ancho, f"{label} (lavavajillas)"),
                (mitad, ancho, f"{label} (der)"),
            ]

    # Corte equitativo en n trozos
    chunk = largo / n
    if chunk > tabla_largo:
        # No cabe ni en n=3 → forzar n mayor (raro)
        n = math.ceil(largo / tabla_largo)
        chunk = largo / n
    return [(chunk, ancho, f"{label} ({i+1}/{n})") for i in range(n)]


def split_pieza_por_huecos(largo: float, ancho: float, label: str,
                            huecos_en_pieza: list[dict],
                            tabla_largo: int, kerf: int = KERF_MM) -> list[tuple[float, float, str]]:
    """
    Si una pieza excede el largo de tabla, la parte priorizando huecos:
    1º placa, 2º fregadero, 3º corte libre centrado.
    Devuelve lista de (largo, ancho, label) para cada sub-pieza.

    Política: el corte se hace justo al lado del hueco (del lado que permite
    que AMBAS mitades quepan en tabla). Cada sub-pieza queda con UN borde en
    el hueco (el hueco "desaparece" geométricamente al partir).
    """
    if largo <= tabla_largo:
        return [(largo, ancho, label)]

    # Ordenar huecos por prioridad: placa → fregadero → resto
    def prioridad(h):
        t = (h.get("tipo") or "").lower()
        return {"placa": 0, "fregadero": 1, "grifo": 2}.get(t, 9)

    candidatos_corte = []
    for h in sorted(huecos_en_pieza, key=prioridad):
        dist = h.get("distancia_lado_mm")
        lh = h.get("largo_mm") or 0
        if dist is None:
            continue
        borde_izq = max(0, dist - lh / 2)
        borde_der = min(largo, dist + lh / 2)
        # Dos opciones: cortar en el borde izquierdo del hueco o en el derecho
        for corte in (borde_izq, borde_der):
            sub1 = corte
            sub2 = largo - corte
            if 0 < sub1 and 0 < sub2 and sub1 <= tabla_largo and sub2 <= tabla_largo:
                candidatos_corte.append((prioridad(h), corte, h.get("tipo")))

    if candidatos_corte:
        candidatos_corte.sort()  # menor prioridad = placa primero
        _, corte, tipo_hueco = candidatos_corte[0]
        sub1 = corte
        sub2 = largo - corte
        return [
            (sub1, ancho, f"{label} (1/2 corte@{tipo_hueco})"),
            (sub2, ancho, f"{label} (2/2 corte@{tipo_hueco})"),
        ]

    # Sin huecos útiles o no permiten partición — corte libre recursivo
    n_trozos = math.ceil(largo / tabla_largo)
    largo_trozo = largo / n_trozos
    return [(largo_trozo, ancho, f"{label} ({i+1}/{n_trozos} corte libre)")
            for i in range(n_trozos)]


def pack_piezas(piezas_dim: list[tuple[float, float, str]],
                tabla_ancho: int, tabla_alto: int,
                rotar: bool) -> list[Tabla]:
    """Wrapper público que elige el mejor algoritmo disponible."""
    return pack_piezas_rectpack(piezas_dim, tabla_ancho, tabla_alto, rotar)


def pack_piezas_rectpack(piezas_dim: list[tuple[float, float, str]],
                          tabla_ancho: int, tabla_alto: int,
                          rotar: bool) -> list[Tabla]:
    """
    Empaqueta usando rectpack (MaxRects BSSF / Guillotine BSSF-SAS).
    Prueba varios algoritmos y devuelve el que menos tablas use (desempate
    por mayor aprovechamiento medio).
    """
    try:
        from rectpack import newPacker, MaxRectsBssf, MaxRectsBaf, GuillotineBssfSas, PackingMode, PackingBin, SORT_AREA
    except ImportError:
        # Fallback al shelf-packing simple (legacy)
        return pack_piezas_shelf(piezas_dim, tabla_ancho, tabla_alto, rotar)

    if not piezas_dim:
        return []

    # rectpack trabaja con enteros; redondeamos dimensiones a mm + kerf integrado
    rects = []
    for idx, (w, h, label) in enumerate(piezas_dim):
        w_i = int(round(w)) + KERF_MM
        h_i = int(round(h)) + KERF_MM
        # rid = índice en piezas_dim para recuperar label
        rects.append((w_i, h_i, idx))

    candidatos = []
    for algo in (MaxRectsBssf, MaxRectsBaf, GuillotineBssfSas):
        packer = newPacker(mode=PackingMode.Offline, bin_algo=PackingBin.BFF,
                            pack_algo=algo, sort_algo=SORT_AREA, rotation=rotar)
        # Añadir muchos bins (tablas) suficientes — rectpack solo usa los que necesite
        for _ in range(len(rects) + 2):
            packer.add_bin(tabla_ancho + KERF_MM, tabla_alto + KERF_MM)
        for w_i, h_i, rid in rects:
            packer.add_rect(w_i, h_i, rid=rid)
        packer.pack()

        # Cuántos bins se usaron realmente
        bins_usados = [b for b in packer if len(b) > 0]
        if not bins_usados:
            continue

        tablas_result = []
        for b in bins_usados:
            t = Tabla(tabla_ancho, tabla_alto)
            for rect in b:
                x, y, rw, rh, rid = rect.x, rect.y, rect.width, rect.height, rect.rid
                # Quitar kerf para dimensión real
                rw_real = rw - KERF_MM
                rh_real = rh - KERF_MM
                label = piezas_dim[rid][2]
                sh = Shelf(y, tabla_ancho)
                sh.x_usado = x
                sh.añadir(rw_real, rh_real, label)
                t.shelves.append(sh)
            tablas_result.append(t)

        # Verificar que todas las piezas se colocaron
        colocadas = sum(len(b) for b in bins_usados)
        if colocadas < len(rects):
            # Algunas piezas no cupieron — las añadimos a una tabla final con ⚠GRANDE
            ids_colocados = {rect.rid for b in bins_usados for rect in b}
            sobrantes = [(piezas_dim[i][0], piezas_dim[i][1], piezas_dim[i][2])
                          for i in range(len(rects)) if i not in ids_colocados]
            overflow = Tabla(tabla_ancho, tabla_alto)
            for w, h, label in sobrantes:
                sh = Shelf(0, tabla_ancho)
                sh.añadir(w, h, label + " ⚠GRANDE")
                overflow.shelves.append(sh)
            tablas_result.append(overflow)

        aprovechamiento_medio = sum(t.aprovechamiento() for t in tablas_result) / len(tablas_result)
        candidatos.append((len(tablas_result), -aprovechamiento_medio, tablas_result, algo.__name__))

    if not candidatos:
        return pack_piezas_shelf(piezas_dim, tabla_ancho, tabla_alto, rotar)

    candidatos.sort(key=lambda c: (c[0], c[1]))  # menos tablas, luego mayor aprovechamiento
    return candidatos[0][2]


def pack_piezas_shelf(piezas_dim: list[tuple[float, float, str]],
                       tabla_ancho: int, tabla_alto: int,
                       rotar: bool) -> list[Tabla]:
    """Fallback shelf-packing legacy (sin rectpack)."""
    items = sorted(piezas_dim, key=lambda x: x[0] * x[1], reverse=True)
    tablas: list[Tabla] = []
    for w_orig, h_orig, label in items:
        colocada = False
        orientaciones = [(w_orig, h_orig)]
        if rotar and w_orig != h_orig:
            orientaciones.append((h_orig, w_orig))
        for tabla in tablas:
            for w, h in orientaciones:
                if w <= tabla.ancho and h <= tabla.alto and tabla.añadir_pieza(w, h, label):
                    colocada = True
                    break
            if colocada:
                break
        if not colocada:
            nueva = Tabla(tabla_ancho, tabla_alto)
            for w, h in orientaciones:
                if w <= tabla_ancho and h <= tabla_alto and nueva.añadir_pieza(w, h, label):
                    colocada = True
                    break
            if not colocada:
                nueva.añadir_pieza(w_orig, h_orig, label + " ⚠GRANDE")
                colocada = True
            tablas.append(nueva)
    return tablas


# ---------------------------------------------------------------------------
# Función principal
# ---------------------------------------------------------------------------

def calcular_tablas(json_path: Path) -> dict:
    """
    Lee un JSON de extracción y calcula las tablas necesarias por material.
    Devuelve un dict con el informe completo.
    """
    with open(json_path, encoding="utf-8") as f:
        datos = json.load(f)

    job_id = datos.get("job_id", "?")
    cliente = datos.get("cliente", "?")

    # Indexar materiales por rol
    mat_index: dict[str, dict] = {}
    for m in datos.get("materiales", []):
        mat_index[m["rol"]] = m

    # Resolver material real (es_igual_a)
    def resolver_material(rol: str) -> Optional[dict]:
        m = mat_index.get(rol)
        if not m:
            return None
        if m.get("es_igual_a"):
            return mat_index.get(m["es_igual_a"], m)
        return m

    # Agrupar piezas por (marca, color, grosor) — clave de tabla
    grupos: dict[str, list] = {}
    sin_material: list = []

    for pieza in datos.get("piezas", []):
        m = resolver_material(pieza.get("material_rol", ""))
        if not m:
            sin_material.append(pieza)
            continue

        marca = m.get("marca", "?")
        color = m.get("color", "?")
        grosor = m.get("grosor_cm", "?")
        clave = f"{marca} {color} {grosor}cm"

        grupos.setdefault(clave, {
            "marca": marca,
            "color": color,
            "grosor_cm": grosor,
            "piezas": [],
            "advertencias_piezas": [],
        })["piezas"].append(pieza)

    # Para cada grupo, calcular tablas
    resultado = {
        "job_id": job_id,
        "cliente": cliente,
        "por_material": {},
        "total_tablas": 0,
        "advertencias": [],
    }

    for clave, grupo in grupos.items():
        marca = grupo["marca"]
        formatos = tabla_para_material(marca)
        rotar = puede_rotar(marca)
        # Usar el formato estándar principal (el primero)
        tabla_w, tabla_h = formatos[0]

        piezas_dim: list[tuple[float, float, str]] = []
        advertencias_g: list[str] = []

        huecos_globales = datos.get("huecos") or []

        def huecos_de_pieza(pieza: dict) -> list[dict]:
            """Devuelve los huecos asociados a una pieza por su campo `zona`.
            Si ningún hueco tiene pieza_zona, se asume que todos pertenecen a
            la encimera más larga (heurística legacy)."""
            zona_p = (pieza.get("zona") or "").strip().lower()
            asociados = [h for h in huecos_globales
                         if (h.get("pieza_zona") or "").strip().lower() == zona_p and zona_p]
            if asociados:
                return asociados
            # Fallback: si ningún hueco tiene pieza_zona, solo asignar a la
            # encimera más larga del grupo
            if not any(h.get("pieza_zona") for h in huecos_globales):
                encimeras = [p for p in grupo["piezas"] if (p.get("tipo") or "").lower() == "encimera"]
                if encimeras:
                    mas_larga = max(encimeras, key=lambda p: float(p.get("largo_mm") or 0))
                    if pieza is mas_larga:
                        return huecos_globales
            return []

        for i, pieza in enumerate(grupo["piezas"]):
            dims = dimensiones_pieza(pieza)
            if not dims:
                advertencias_g.append(
                    f"Pieza #{i+1} ({pieza.get('tipo')} {pieza.get('zona','')}) sin dimensiones — no se calcula"
                )
                continue
            w, h = dims
            label = f"{pieza.get('tipo','')} {pieza.get('zona','')}"
            if w == 0 or h == 0:
                advertencias_g.append(f"Pieza #{i+1} ({label}) dimensión 0 — ignorada")
                continue
            tipo_p = (pieza.get("tipo") or "").lower()
            fits_normal = (w <= tabla_w and h <= tabla_h)
            fits_rotada = rotar and (h <= tabla_w and w <= tabla_h)
            if not (fits_normal or fits_rotada):
                # Dispatch según tipo de pieza:
                #   encimera/isla/cascada → partir por huecos (placa > fregadero)
                #   rodapie/zocalo → regla especial (lavavajillas separado, max 3 trozos)
                #   resto (frontal, copete, chapeado, etc) → corte libre
                if tipo_p in ("encimera", "isla", "cascada"):
                    sub_piezas = split_pieza_por_huecos(w, h, label, huecos_de_pieza(pieza), tabla_w)
                elif tipo_p in ("rodapie", "zocalo"):
                    sub_piezas = split_rodapie(w, h, label, tabla_w, tiene_lavavajillas=True)
                else:
                    sub_piezas = split_pieza_por_huecos(w, h, label, [], tabla_w)
                if len(sub_piezas) > 1:
                    advertencias_g.append(
                        f"🔪 {label} ({w:.0f}×{h:.0f}mm) → {len(sub_piezas)} trozos: "
                        + " + ".join(f"{s[0]:.0f}×{s[1]:.0f}" for s in sub_piezas)
                    )
                else:
                    advertencias_g.append(
                        f"⚠ PIEZA GRANDE: {label} ({w:.0f}×{h:.0f}mm) no se pudo partir — "
                        f"supera tabla {tabla_w}×{tabla_h}mm"
                    )
                piezas_dim.extend(sub_piezas)
            else:
                piezas_dim.append((w, h, label))

        if not piezas_dim:
            resultado["por_material"][clave] = {
                "tablas_necesarias": 0,
                "formato_tabla_mm": f"{tabla_w}×{tabla_h}",
                "piezas_totales": 0,
                "layout": [],
                "advertencias": advertencias_g,
            }
            continue

        tablas = pack_piezas(piezas_dim, tabla_w, tabla_h, rotar)
        n_tablas = len(tablas)

        # Construir info de layout
        layout = []
        for idx, t in enumerate(tablas):
            piezas_en_tabla = []
            for sh in t.shelves:
                for p in sh.piezas_colocadas:
                    piezas_en_tabla.append({
                        "label": p["label"],
                        "w_mm": round(p["w"]),
                        "h_mm": round(p["h"]),
                    })
            layout.append({
                "tabla": idx + 1,
                "aprovechamiento_pct": round(t.aprovechamiento(), 1),
                "area_usada_m2": round(t.area_usada() / 1e6, 3),
                "piezas": piezas_en_tabla,
            })

        resultado["por_material"][clave] = {
            "tablas_necesarias": n_tablas,
            "formato_tabla_mm": f"{tabla_w}×{tabla_h}",
            "area_tabla_m2": round(tabla_w * tabla_h / 1e6, 3),
            "piezas_totales": len(piezas_dim),
            "layout": layout,
            "advertencias": advertencias_g,
            "rotar_permitido": rotar,
        }
        resultado["total_tablas"] += n_tablas

    if sin_material:
        resultado["advertencias"].append(
            f"{len(sin_material)} pieza(s) sin material identificado — no calculadas"
        )

    return resultado


# ---------------------------------------------------------------------------
# Generación del informe de texto
# ---------------------------------------------------------------------------

def informe_texto(resultado: dict) -> str:
    lines = []
    lines.append(f"{'='*60}")
    lines.append(f"CÁLCULO DE TABLAS — {resultado['job_id']} {resultado['cliente']}")
    lines.append(f"{'='*60}")
    lines.append(f"TOTAL TABLAS NECESARIAS: {resultado['total_tablas']}")
    lines.append("")

    for mat, info in resultado["por_material"].items():
        n = info["tablas_necesarias"]
        fmt = info.get("formato_tabla_mm", "?")
        area = info.get("area_tabla_m2", 0)
        lines.append(f"  {mat}")
        lines.append(f"    Formato tabla: {fmt} mm  ({area} m²/tabla)")
        lines.append(f"    Tablas necesarias: {n}  ({info['piezas_totales']} piezas)")

        for t in info.get("layout", []):
            lines.append(f"    ── Tabla {t['tabla']} ──  "
                         f"Aprovechamiento: {t['aprovechamiento_pct']}%  "
                         f"({t['area_usada_m2']} m² usados)")
            for p in t["piezas"]:
                lines.append(f"       • {p['label']}  {p['w_mm']}×{p['h_mm']} mm")

        for adv in info.get("advertencias", []):
            lines.append(f"    ⚠ {adv}")
        lines.append("")

    if resultado.get("advertencias"):
        lines.append("ADVERTENCIAS GENERALES:")
        for a in resultado["advertencias"]:
            lines.append(f"  ⚠ {a}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Guardar resultados
# ---------------------------------------------------------------------------

def guardar_resultado(resultado: dict, json_path: Path) -> tuple[Path, Path]:
    stem = json_path.stem.replace("_extraccion", "")
    carpeta = json_path.parent

    json_out = carpeta / f"{stem}_tablas.json"
    txt_out = carpeta / f"{stem}_tablas.txt"

    with open(json_out, "w", encoding="utf-8") as f:
        json.dump(resultado, f, ensure_ascii=False, indent=2)
    with open(txt_out, "w", encoding="utf-8") as f:
        f.write(informe_texto(resultado))

    return json_out, txt_out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Uso: python calcular_tablas.py <ruta_extraccion.json | carpeta_trabajo>")
        sys.exit(1)

    ruta = Path(sys.argv[1])

    # Si es carpeta, buscar el JSON de extracción
    if ruta.is_dir():
        candidatos = list(ruta.glob("*_extraccion.json"))
        if not candidatos:
            print(f"No se encontró *_extraccion.json en {ruta}")
            sys.exit(1)
        json_path = sorted(candidatos)[-1]
    else:
        json_path = ruta

    print(f"Procesando: {json_path.name}")
    resultado = calcular_tablas(json_path)
    print(informe_texto(resultado))

    if "--guardar" in sys.argv:
        j, t = guardar_resultado(resultado, json_path)
        print(f"\nGuardado: {j.name}")
        print(f"Guardado: {t.name}")


if __name__ == "__main__":
    main()
