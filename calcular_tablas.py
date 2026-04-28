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
                   tabla_largo: int, tiene_lavavajillas: bool = True,
                   forzar_n: Optional[int] = None) -> list[tuple[float, float, str]]:
    """
    Rodapié/zócalo: cortes especiales.
    - Por defecto, 2-3 trozos (mínimo necesario para que entren en tabla, máximo 3).
    - Si hay lavavajillas y largo > 4m: izq + lavavajillas (600) + der.
    - Si no, corte simétrico.
    - `forzar_n`: si se pasa, parte EXACTAMENTE en N trozos iguales (consolidación).
    """
    if forzar_n is not None and forzar_n >= 2:
        chunk = largo / forzar_n
        return [(chunk, ancho, f"{label} ({i+1}/{forzar_n})") for i in range(forzar_n)]
    if largo <= tabla_largo:
        return [(largo, ancho, label)]

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
    Prueba varios algoritmos + 2 estrategias de orden:
      - SORT_AREA: máxima compactación pura (puede romper orden de colocación)
      - SORT_NONE: respeta el orden de entrada (para veteados — frontales/chapeados
        del mismo grupo van consecutivos en la tabla)
    Elige el resultado con menos tablas; en empate, prefiere SORT_NONE para mantener
    veteado continuo.
    """
    try:
        from rectpack import (newPacker, MaxRectsBssf, MaxRectsBaf,
                               GuillotineBssfSas, PackingMode, PackingBin,
                               SORT_AREA, SORT_NONE)
    except ImportError:
        return pack_piezas_shelf(piezas_dim, tabla_ancho, tabla_alto, rotar)

    if not piezas_dim:
        return []

    rects = []
    for idx, (w, h, label) in enumerate(piezas_dim):
        w_i = int(round(w)) + KERF_MM
        h_i = int(round(h)) + KERF_MM
        rects.append((w_i, h_i, idx))

    candidatos = []
    # Probamos cada combinación de algoritmo de empaque × estrategia de orden
    for algo in (MaxRectsBssf, MaxRectsBaf, GuillotineBssfSas):
     for sort_algo in (SORT_NONE, SORT_AREA):
        packer = newPacker(mode=PackingMode.Offline, bin_algo=PackingBin.BFF,
                            pack_algo=algo, sort_algo=sort_algo, rotation=rotar)
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
        # 3ª clave: prefiere SORT_NONE para mantener orden de colocación (veteado)
        prefer_orden = 0 if sort_algo == SORT_NONE else 1
        candidatos.append((len(tablas_result), -aprovechamiento_medio, prefer_orden,
                            tablas_result, algo.__name__))

    if not candidatos:
        return pack_piezas_shelf(piezas_dim, tabla_ancho, tabla_alto, rotar)

    # menos tablas → mayor aprovechamiento → SORT_NONE para empates
    candidatos.sort(key=lambda c: (c[0], c[1], c[2]))
    return candidatos[0][3]


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

def detectar_opciones(datos: dict) -> list[int]:
    """Devuelve lista de números de opción presentes en los materiales (ej: [1,2]).
    Lista vacía si no hay opciones (caso material único)."""
    nums = set()
    import re as _re
    for m in datos.get("materiales", []):
        match = _re.search(r"_opcion(\d+)$", (m.get("rol") or "").lower())
        if match:
            nums.add(int(match.group(1)))
    return sorted(nums)


def filtrar_por_opcion(datos: dict, opcion: int) -> dict:
    """Devuelve una copia de datos con SOLO los materiales/piezas/huecos de esa opción.
    Las piezas/materiales sin sufijo _opcionN se mantienen (compartidos)."""
    import re as _re
    sufijo = f"_opcion{opcion}"
    def _es_de_otra_opcion(rol: str) -> bool:
        m = _re.search(r"_opcion(\d+)$", (rol or "").lower())
        return bool(m) and int(m.group(1)) != opcion
    nuevos = dict(datos)
    nuevos["materiales"] = [m for m in datos.get("materiales",[])
                             if not _es_de_otra_opcion(m.get("rol",""))]
    nuevos["piezas"] = [p for p in datos.get("piezas",[])
                         if not _es_de_otra_opcion(p.get("material_rol",""))]
    nuevos["huecos"] = list(datos.get("huecos", []))
    return nuevos


def calcular_tablas(json_path: Path, datos_override: Optional[dict] = None) -> dict:
    """
    Lee un JSON de extracción y calcula las tablas necesarias por material.
    Devuelve un dict con el informe completo.

    `datos_override`: si se pasa un dict, se usa en lugar de leer json_path
    (útil para procesar opciones filtradas).
    """
    if datos_override is not None:
        datos = datos_override
    else:
        with open(json_path, encoding="utf-8") as f:
            datos = json.load(f)

    job_id = datos.get("job_id", "?")
    cliente = datos.get("cliente", "?")

    # Indexar materiales por rol
    mat_index: dict[str, dict] = {}
    for m in datos.get("materiales", []):
        mat_index[m["rol"]] = m

    # Resolver material real (es_igual_a). Si no existe el rol referenciado pero
    # sí existe rol+_opcion1 (caso de varios materiales alternativos), redirigir.
    # Si el rol viene con sufijo _opcionN pero el material no lo tiene, despojarlo.
    import re as _re

    def resolver_material(rol: str) -> Optional[dict]:
        m = mat_index.get(rol)
        if not m:
            # Si rol viene con sufijo _opcionN pero el material está sin sufijo, despojar
            base = _re.sub(r"_opcion\d+$", "", rol)
            if base != rol and base in mat_index:
                m = mat_index[base]
            elif rol + "_opcion1" in mat_index:
                m = mat_index[rol + "_opcion1"]
            else:
                return None
        if m.get("es_igual_a"):
            ref = m["es_igual_a"]
            if ref in mat_index:
                return mat_index[ref]
            # Buscar primera opcionN del rol referenciado
            for k in mat_index:
                if k == ref + "_opcion1":
                    return mat_index[k]
            for k in mat_index:
                if k.startswith(ref + "_opcion"):
                    return mat_index[k]
            return m
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
        # Mapa: label → info completa (vertices, huecos asociados) para visualización
        label_info: dict = {}

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
            # Asociar info de polígono y huecos al label (para visualización 2D real)
            huecos_pza = huecos_de_pieza(pieza) if tipo_p in ("encimera","isla","cascada") else []
            label_info[label] = {
                "vertices_mm": pieza.get("vertices_mm"),
                "forma": pieza.get("forma"),
                "tipo": tipo_p,
                "zona": pieza.get("zona"),
                "huecos": huecos_pza,
                "bbox_w": w,
                "bbox_h": h,
            }
            fits_normal = (w <= tabla_w and h <= tabla_h)
            fits_rotada = rotar and (h <= tabla_w and w <= tabla_h)
            if not (fits_normal or fits_rotada):
                if tipo_p in ("encimera", "isla", "cascada"):
                    sub_piezas = split_pieza_por_huecos(w, h, label, huecos_pza, tabla_w)
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

        # CONSOLIDACIÓN: si la última tabla tiene poco aprovechamiento (<40%),
        # sub-dividir SOLO los rodapiés/zócalos que están en esa tabla (no todos)
        # para meterlos en las tablas anteriores.
        def _intentar_consolidar(piezas_dim_actual, tablas_actuales):
            if len(tablas_actuales) <= 1:
                return tablas_actuales, piezas_dim_actual
            ultima = tablas_actuales[-1]
            if ultima.aprovechamiento() >= 40:
                return tablas_actuales, piezas_dim_actual
            # Labels de piezas en la última tabla
            labels_en_ultima = set()
            for sh in ultima.shelves:
                for p in sh.piezas_colocadas:
                    labels_en_ultima.add(p["label"])
            # Solo sub-dividir rodapiés/zócalos cuyos sub-trozos están en la última tabla
            roda_a_dividir = {}
            for k, info in label_info.items():
                if info.get("tipo") not in ("rodapie", "zocalo"):
                    continue
                en_ultima = any(
                    (lbl == k) or lbl.startswith(k + " (")
                    for lbl in labels_en_ultima
                )
                if en_ultima:
                    roda_a_dividir[k] = info
            if not roda_a_dividir:
                return tablas_actuales, piezas_dim_actual
            # Probar incrementando trozos solo en los rodapiés problemáticos
            mejor_tablas, mejor_dim = tablas_actuales, piezas_dim_actual
            for incremento in (1, 2, 3, 4):
                nuevas = []
                for w, h, lbl in piezas_dim_actual:
                    pertenece_a_dividir = any(
                        lbl == k or lbl.startswith(k + " (")
                        for k in roda_a_dividir
                    )
                    if pertenece_a_dividir:
                        continue
                    nuevas.append((w, h, lbl))
                for k, info in roda_a_dividir.items():
                    largo_orig = info["bbox_w"]
                    ancho_orig = info["bbox_h"]
                    n_min = math.ceil(largo_orig / tabla_w)
                    n_target = max(n_min, 2) + incremento
                    sub = split_rodapie(largo_orig, ancho_orig, k, tabla_w,
                                         tiene_lavavajillas=False, forzar_n=n_target)
                    nuevas.extend(sub)
                t2 = pack_piezas(nuevas, tabla_w, tabla_h, rotar)
                if len(t2) < len(mejor_tablas):
                    mejor_tablas, mejor_dim = t2, nuevas
                    break
            return mejor_tablas, mejor_dim

        tablas, piezas_dim = _intentar_consolidar(piezas_dim, tablas)
        n_tablas = len(tablas)

        # Construir info de layout (con x/y para visualización)
        layout = []
        for idx, t in enumerate(tablas):
            piezas_en_tabla = []
            for sh in t.shelves:
                for p in sh.piezas_colocadas:
                    # Buscar label_info por label exacto. Si label es de un sub-trozo
                    # (ej "encimera xyz (1/2 corte@placa)"), usa el prefijo.
                    info = label_info.get(p["label"])
                    if not info:
                        for k, v in label_info.items():
                            if p["label"].startswith(k):
                                info = v; break
                    bbox_w = (info or {}).get("bbox_w")
                    sub_trozo = info and bbox_w and abs(p["w"] - bbox_w) > 5
                    pieza_layout = {
                        "label": p["label"],
                        "x_mm": round(p["x"]),
                        "y_mm": round(p["y"]),
                        "w_mm": round(p["w"]),
                        "h_mm": round(p["h"]),
                    }
                    if info and not sub_trozo:
                        # Pieza completa (no partida): incluir polígono y huecos
                        if info.get("vertices_mm"):
                            pieza_layout["vertices_mm"] = info["vertices_mm"]
                        if info.get("huecos"):
                            pieza_layout["huecos"] = info["huecos"]
                        if info.get("tipo"):
                            pieza_layout["tipo"] = info["tipo"]
                    piezas_en_tabla.append(pieza_layout)
            layout.append({
                "tabla": idx + 1,
                "ancho_mm": tabla_w,
                "alto_mm": tabla_h,
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


def dibujar_layout_pdf(resultado: dict, pdf_path: Path) -> Path:
    """Genera un PDF con una página por tabla mostrando el reparto de piezas
    como polígonos 2D reales (con huecos posicionados)."""
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle, Polygon as MplPolygon
    from matplotlib.backends.backend_pdf import PdfPages

    # Paleta por tipo de pieza (busca palabras clave en el label)
    COLORES = {
        "encimera": "#FFD966",   # amarillo cálido
        "isla":     "#FFC080",   # naranja
        "cascada":  "#FFA060",
        "frontal":  "#9DC3E6",   # azul claro
        "chapeado": "#9DC3E6",
        "costado":  "#A9D18E",   # verde claro
        "pilastra": "#C5E0B4",
        "copete":   "#D5A6BD",   # rosa
        "zocalo":   "#B4A7D6",   # lila
        "rodapie":  "#B4A7D6",
        "default":  "#CCCCCC",
    }

    def color_para(label: str) -> str:
        low = label.lower()
        for k, c in COLORES.items():
            if k in low:
                return c
        return COLORES["default"]

    with PdfPages(pdf_path) as pdf:
        # Portada
        fig, ax = plt.subplots(figsize=(11, 8.5))
        ax.axis('off')
        ax.text(0.5, 0.75, f"REPARTO EN TABLAS — {resultado['job_id']}",
                fontsize=20, ha='center', fontweight='bold')
        ax.text(0.5, 0.68, resultado.get('cliente', ''), fontsize=13, ha='center')
        ax.text(0.5, 0.58, f"TOTAL TABLAS: {resultado['total_tablas']}",
                fontsize=24, ha='center', color='#C00000', fontweight='bold')
        # Resumen por material
        y = 0.48
        for mat, info in resultado["por_material"].items():
            ax.text(0.1, y, f"• {mat}: {info['tablas_necesarias']} tablas "
                            f"({info['piezas_totales']} piezas, "
                            f"formato {info.get('formato_tabla_mm','?')} mm)",
                    fontsize=11)
            y -= 0.04
        pdf.savefig(fig); plt.close(fig)

        # Una página por cada tabla
        for mat, info in resultado["por_material"].items():
            tabla_w = info["layout"][0]["ancho_mm"] if info.get("layout") else 3000
            tabla_h = info["layout"][0]["alto_mm"] if info.get("layout") else 1440
            for t in info.get("layout", []):
                fig, ax = plt.subplots(figsize=(14, max(6, tabla_h / tabla_w * 12)))
                # Fondo de la tabla
                ax.add_patch(Rectangle((0, 0), tabla_w, tabla_h,
                                       facecolor='#F5F5F5', edgecolor='black', linewidth=2))
                # Piezas
                for p in t["piezas"]:
                    x, y = p["x_mm"], p["y_mm"]
                    w, h = p["w_mm"], p["h_mm"]
                    color = color_para(p["label"])
                    verts_pieza = p.get("vertices_mm")

                    if verts_pieza and len(verts_pieza) >= 3:
                        # Dibujar como polígono real, trasladado a (x, y) en la tabla
                        verts_trans = [(vx + x, vy + y) for (vx, vy) in verts_pieza]
                        ax.add_patch(MplPolygon(verts_trans, closed=True,
                                                 facecolor=color, edgecolor='black',
                                                 linewidth=1.2, alpha=0.9))
                    else:
                        # Pieza simple: rectángulo
                        ax.add_patch(Rectangle((x, y), w, h, facecolor=color,
                                                edgecolor='black', linewidth=1, alpha=0.9))

                    # Huecos dentro de la pieza (si están)
                    for hueco in (p.get("huecos") or []):
                        cx = hueco.get("centro_x_mm")
                        cy = hueco.get("centro_y_mm")
                        hw = hueco.get("largo_mm") or 50
                        hh = hueco.get("ancho_mm") or 50
                        if cx is None or cy is None:
                            continue
                        ax.add_patch(Rectangle((x + cx - hw/2, y + cy - hh/2), hw, hh,
                                                facecolor='white', edgecolor='red',
                                                linewidth=0.8, alpha=0.95, zorder=10))
                        # Etiqueta hueco
                        ax.text(x + cx, y + cy, hueco.get('tipo','')[:1].upper(),
                                fontsize=7, ha='center', va='center',
                                color='red', fontweight='bold', zorder=11)

                    # Etiqueta centrada de la pieza
                    etiq = f"{p['label']}\n{w}×{h}"
                    area_rel = (w * h) / (tabla_w * tabla_h)
                    fs = max(6, min(10, int(area_rel * 80) + 5))
                    ax.text(x + w/2, y + h/2, etiq,
                            fontsize=fs, ha='center', va='center', wrap=True, zorder=5)

                ax.set_xlim(-100, tabla_w + 100)
                ax.set_ylim(-100, tabla_h + 100)
                ax.set_aspect('equal')
                ax.set_title(
                    f"{mat} — TABLA {t['tabla']}/{info['tablas_necesarias']}   "
                    f"{tabla_w}×{tabla_h}mm   "
                    f"Aprovechamiento: {t['aprovechamiento_pct']}%",
                    fontsize=12)
                ax.set_xlabel("mm")
                ax.set_ylabel("mm")
                ax.grid(True, alpha=0.3)
                plt.tight_layout()
                pdf.savefig(fig); plt.close(fig)

    return pdf_path


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
    with open(json_path, encoding="utf-8") as f:
        datos_full = json.load(f)
    opciones = detectar_opciones(datos_full)
    stem = json_path.stem.replace("_extraccion", "")
    nombres_opciones: list[str] = []   # nombre legible de cada opción

    if opciones:
        # Hay varias opciones de material → 1 PDF por opción
        for n in opciones:
            datos_op = filtrar_por_opcion(datos_full, n)
            # Nombre legible: marca+color del rol "encimera_opcionN"
            nombre_op = f"opcion{n}"
            for m in datos_op.get("materiales", []):
                if m.get("rol") == f"encimera_opcion{n}":
                    partes = [m.get("marca",""), m.get("color","")]
                    txt = " ".join(p for p in partes if p)
                    if txt: nombre_op = f"opcion{n}-{txt}"
                    break
            nombres_opciones.append(nombre_op)

            print(f"\n=== OPCIÓN {n}: {nombre_op} ===")
            res = calcular_tablas(json_path, datos_override=datos_op)
            print(informe_texto(res))
            stem_op = f"{stem}_{nombre_op.replace(' ','_')}"
            if "--guardar" in sys.argv:
                json_out = json_path.parent / f"{stem_op}_tablas.json"
                txt_out  = json_path.parent / f"{stem_op}_tablas.txt"
                json_out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
                txt_out.write_text(informe_texto(res), encoding="utf-8")
                print(f"Guardado: {json_out.name}")
                print(f"Guardado: {txt_out.name}")
            if "--pdf" in sys.argv or "--guardar" in sys.argv:
                pdf_out = json_path.parent / f"{stem_op}_tablas.pdf"
                dibujar_layout_pdf(res, pdf_out)
                print(f"Guardado: {pdf_out.name}")
    else:
        # Material único → 1 solo PDF
        resultado = calcular_tablas(json_path, datos_override=datos_full)
        print(informe_texto(resultado))
        if "--guardar" in sys.argv:
            j, t = guardar_resultado(resultado, json_path)
            print(f"\nGuardado: {j.name}")
            print(f"Guardado: {t.name}")
        if "--pdf" in sys.argv or "--guardar" in sys.argv:
            pdf_out = json_path.parent / f"{stem}_tablas.pdf"
            dibujar_layout_pdf(resultado, pdf_out)
            print(f"Guardado: {pdf_out.name}")


if __name__ == "__main__":
    main()
