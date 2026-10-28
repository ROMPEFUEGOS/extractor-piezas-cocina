# -*- coding: utf-8 -*-
"""
Extracción determinista de textos (cotas y etiquetas) de planos PDF vectoriales.

Motivación: `pdfplumber.Page.extract_text()` corrompe los planos de cocina del
taller en dos formas observadas:
  * palabras giradas 90° que salen invertidas ("COPETE" → "ETEPOC",
    930 → "039", 600 → "009", 2635 → "5362"), porque ordena los caracteres
    por `top` creciente aunque el texto se lea hacia arriba;
  * letras duplicadas por sobreimpresión (negrita simulada con doble
    trazado del texto): "EENNCCIIMMEERRAA".

Este módulo recompone las palabras a partir de los caracteres sueltos de
pdfplumber, deduplicando los sobreimpresos y respetando el sentido de
lectura real según el giro de cada carácter (matriz de transformación).

Convenio de giro (verificado empíricamente con J0024_encimera_vectorial.pdf):
  * El ángulo de lectura de un carácter es `atan2(b, a)` de su `matrix`
    redondeado al múltiplo de 90 más cercano (0, 90, 180, 270). Para esos
    planos los caracteres girados tienen matrix (0, s, -s, 0, e, f) → 90°.
  * Giro 0°: se lee de izquierda a derecha (x creciente).
  * Giro 180°: se lee de derecha a izquierda (x decreciente).
  * Giro 90°: se lee de ABAJO hacia ARRIBA, es decir `top` decreciente.
    Evidencia: en J0024 la secuencia E-N-C-I-M-E-R-A de la etiqueta vertical
    aparece con f (top) 401.4 → 396.0 → 390.6 → ... decreciendo, y con este
    convenio "930" sale como "930" y "COPETE" como "COPETE" (no invertidos).
  * Giro 270°: simétrico, se lee de arriba hacia abajo (`top` creciente).
    En los samples reales no hay texto a 270°; se valida con fixtures
    sintéticos (matplotlib rotation=270).

El campo `upright` de pdfplumber es False para cualquier carácter girado y no
distingue 90/180/270, por eso se usa la matriz.

Solo funciones puras (sin red ni efectos secundarios); el único `print` vive
en el CLI.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from typing import Any, Optional

#: Mínimo de primitivas de dibujo (líneas + rects + curvas) para considerar
#: una página como plano vectorial (y no un PDF de solo texto).
UMBRAL_VECTORES = 100
#: Valor mínimo (mm) para que un número del plano cuente como cota en el hint.
COTA_MINIMA = 20

# Factores de tolerancia respecto al tamaño de fuente del carácter (en pt).
_UMBRAL_DEDUPE = 0.35   # centros más cercanos que esto ⇒ carácter sobreimpreso
_UMBRAL_LINEA = 0.5     # separación perpendicular para partir líneas
_UMBRAL_PALABRA = 0.6   # hueco en el sentido de lectura para partir palabras


def _angulo_char(char: dict) -> int:
    """Giro de un carácter pdfplumber: atan2(b, a) de su matriz redondeado
    al múltiplo de 90 más cercano (0, 90, 180 o 270)."""
    a, b = (char.get("matrix") or (1, 0, 0, 1, 0, 0))[:2]
    if a == 0 and b == 0:
        return 0
    return int(round(math.degrees(math.atan2(b, a)) / 90.0)) % 4 * 90


def _centro(char: dict) -> tuple[float, float]:
    """Centro del bounding box de un carácter en coordenadas de pdfplumber
    (origen arriba-izquierda)."""
    return ((char["x0"] + char["x1"]) / 2.0, (char["top"] + char["bottom"]) / 2.0)


def _deduplicar_chars(chars: list[dict]) -> list[dict]:
    """Elimina caracteres sobreimpresos (negrita simulada): dos caracteres con
    el mismo texto, el mismo giro y centros a menos de `_UMBRAL_DEDUPE` ×
    tamaño son el mismo glifo (se conserva uno).

    Los espacios explícitos del content stream se conservan: no forman parte
    del texto final, pero actúan como separadores al partir palabras (verificado
    en los planos reales: "FRONTAL H.700" lleva espacio explícito).
    """
    legibles = [c for c in chars if str(c.get("text", "")) != ""]
    ordenados = sorted(
        legibles,
        key=lambda c: (_angulo_char(c), _centro(c)[1], _centro(c)[0], str(c.get("text", ""))),
    )
    unicos: list[dict] = []
    cubos: dict[tuple[int, str], list[dict]] = {}
    for char in ordenados:
        clave = (_angulo_char(char), str(char["text"]))
        cx, cy = _centro(char)
        tam = max(float(char.get("size", 1.0)), 1.0)
        bucket = cubos.setdefault(clave, [])
        duplicado = False
        for otro in bucket:
            ox, oy = _centro(otro)
            if math.hypot(ox - cx, oy - cy) < _UMBRAL_DEDUPE * tam:
                duplicado = True
                break
        if not duplicado:
            bucket.append(char)
            unicos.append(char)
    return unicos


def _orden_lectura(chars: list[dict], angulo: int) -> list[dict]:
    """Ordena los caracteres de una línea en su sentido de lectura real
    (ver convenio de giro en la cabecera del módulo)."""
    if angulo == 0:
        return sorted(chars, key=lambda c: (c["x0"], c["top"]))
    if angulo == 180:
        return sorted(chars, key=lambda c: (-c["x1"], c["top"]))
    if angulo == 90:  # se lee hacia arriba: top decreciente
        return sorted(chars, key=lambda c: (-c["top"], c["x0"]))
    return sorted(chars, key=lambda c: (c["top"], c["x0"]))  # 270: hacia abajo


def _hueco(previo: dict, actual: dict, angulo: int) -> float:
    """Huevo (separación) entre dos caracteres consecutivos en el sentido de
    lectura; negativo si se solapan."""
    if angulo == 0:
        return actual["x0"] - previo["x1"]
    if angulo == 180:
        return previo["x0"] - actual["x1"]
    if angulo == 90:
        return previo["top"] - actual["bottom"]
    return actual["top"] - previo["bottom"]  # 270


def _partir_palabras(linea: list[dict], angulo: int) -> list[list[dict]]:
    """Parte una línea (ya en orden de lectura) en palabras: corte en los
    caracteres de espacio y donde el hueco supere `_UMBRAL_PALABRA` × tamaño.

    Así "COPETE FRONTAL H.700" queda como COPETE / FRONTAL / H.700 mientras
    que los dígitos de una cota ("2635") permanecen juntos.
    """
    palabras: list[list[dict]] = []
    actual: list[dict] = []
    for char in linea:
        if not str(char.get("text", "")).strip():
            if actual:
                palabras.append(actual)
                actual = []
            continue
        if actual:
            tam = max(float(actual[-1].get("size", 1.0)), float(char.get("size", 1.0)), 1.0)
            if _hueco(actual[-1], char, angulo) > _UMBRAL_PALABRA * tam:
                palabras.append(actual)
                actual = []
        actual.append(char)
    if actual:
        palabras.append(actual)
    return palabras


def _lineas_de_grupo(chars: list[dict], angulo: int) -> list[list[dict]]:
    """Agrupa por líneas los caracteres de un mismo giro: se clusteriza por la
    coordenada PERPENDICULAR al sentido de lectura (`cy` para 0/180, `cx` para
    90/270) con tolerancia `_UMBRAL_LINEA` × tamaño, y cada línea se devuelve
    en orden de lectura."""
    if angulo in (0, 180):
        def perp(c: dict) -> float:
            return _centro(c)[1]
    else:
        def perp(c: dict) -> float:
            return _centro(c)[0]

    ordenados = sorted(chars, key=lambda c: (perp(c), c["x0"], c["top"]))
    lineas: list[list[dict]] = []
    for char in ordenados:
        if lineas:
            ref = lineas[-1][-1]
            tol = _UMBRAL_LINEA * max(float(char.get("size", 1.0)), float(ref.get("size", 1.0)), 1.0)
            if abs(perp(char) - perp(ref)) <= tol:
                lineas[-1].append(char)
                continue
        lineas.append([char])
    return [_orden_lectura(l, angulo) for l in lineas]


def _palabra_a_dict(palabra: list[dict], angulo: int) -> dict:
    """Convierte la lista de caracteres de una palabra en el diccionario de
    salida: texto, bbox (unión de caracteres), centro, giro y `numero`
    (int(texto) si el texto son solo dígitos, si no None)."""
    x0 = min(c["x0"] for c in palabra)
    x1 = max(c["x1"] for c in palabra)
    top = min(c["top"] for c in palabra)
    bottom = max(c["bottom"] for c in palabra)
    texto = "".join(str(c.get("text", "")) for c in palabra).strip()
    numero: Optional[int] = None
    if texto.isdigit():
        try:
            numero = int(texto)
        except ValueError:  # dígitos unicode raros (p.ej. superíndices)
            numero = None
    return {
        "texto": texto,
        "x0": float(x0),
        "top": float(top),
        "x1": float(x1),
        "bottom": float(bottom),
        "cx": (x0 + x1) / 2.0,
        "cy": (top + bottom) / 2.0,
        "angulo": int(angulo),
        "numero": numero,
    }


def textos_pagina(page: Any) -> list[dict]:
    """Palabras de una página pdfplumber con posición, centro y giro.

    Cada palabra: {"texto", "x0", "top", "x1", "bottom", "cx", "cy",
    "angulo" (0|90|180|270), "numero" (int|None)}. Devuelve [] si la página
    no tiene caracteres. Resultado determinista (ordenado por cy, cx, giro).
    """
    chars = list(getattr(page, "chars", None) or [])
    if not chars:
        return []
    unicos = _deduplicar_chars(chars)
    por_giro: dict[int, list[dict]] = {}
    for char in unicos:
        por_giro.setdefault(_angulo_char(char), []).append(char)
    textos: list[dict] = []
    for angulo in sorted(por_giro):
        for linea in _lineas_de_grupo(por_giro[angulo], angulo):
            for palabra in _partir_palabras(linea, angulo):
                textos.append(_palabra_a_dict(palabra, angulo))
    textos.sort(key=lambda t: (t["cy"], t["cx"], t["angulo"], t["texto"]))
    return textos


def es_plano_vectorial(page: Any) -> bool:
    """True si la página tiene suficientes primitivas de dibujo
    (>= UMBRAL_VECTORES entre líneas, rects y curvas) y algo de texto:
    es un plano vectorial, no un PDF de texto corrido ni un escaneado."""
    n_vectores = len(page.lines) + len(page.rects) + len(page.curves)
    return n_vectores >= UMBRAL_VECTORES and len(page.chars) > 0


def _contar_vectores(page: Any) -> int:
    return len(page.lines) + len(page.rects) + len(page.curves)


def extraer_textos_pdf(path: Any, max_pages: int = 5) -> list[dict]:
    """Procesa hasta `max_pages` páginas de un PDF vectorial.

    Por página: {"pagina" (1-based), "ancho_pt", "alto_pt", "n_vectores",
    "es_plano", "textos"}. Las páginas sin caracteres devuelven "textos": [].
    """
    import pdfplumber

    resultado: list[dict] = []
    with pdfplumber.open(str(path)) as pdf:
        for indice, page in enumerate(pdf.pages[:max_pages], start=1):
            resultado.append({
                "pagina": indice,
                "ancho_pt": float(page.width),
                "alto_pt": float(page.height),
                "n_vectores": _contar_vectores(page),
                "es_plano": es_plano_vectorial(page),
                "textos": textos_pagina(page),
            })
    return resultado


def hint_plano_vectorial(path: Any, max_pages: int = 5) -> Optional[str]:
    """Bloque de texto compacto (para un LLM) con las cotas y etiquetas de
    cada página que sea un plano vectorial. None si ninguna página lo es.

    Por página: una cabecera con tamaño y nº de cotas, una línea por cota
    numérica ordenada por (cy, cx) — "600 @ (x=548, y=397) pt, giro 90°" — y
    a continuación las etiquetas no numéricas con el mismo formato.
    """
    paginas = extraer_textos_pdf(path, max_pages)
    planos = [p for p in paginas if p["es_plano"]]
    if not planos:
        return None
    bloques: list[str] = []
    for pagina in planos:
        # Números menores que COTA_MINIMA no son cotas (vienen de cabeceras:
        # "DISEÑO 1", "Página 1 (1)") y se omiten del hint.
        cotas = [t for t in pagina["textos"]
                 if t["numero"] is not None and t["numero"] >= COTA_MINIMA]
        etiquetas = [t for t in pagina["textos"]
                     if t["numero"] is None and len(t["texto"]) >= 3]
        lineas = [
            "Página {} ({}x{} pt): {} cotas".format(
                pagina["pagina"], round(pagina["ancho_pt"]), round(pagina["alto_pt"]),
                len(cotas))
        ]
        for texto in sorted(cotas, key=lambda t: (t["cy"], t["cx"])):
            lineas.append("{} @ (x={}, y={}) pt, giro {}°".format(
                texto["numero"], round(texto["cx"]), round(texto["cy"]), texto["angulo"]))
        for texto in sorted(etiquetas, key=lambda t: (t["cy"], t["cx"])):
            lineas.append("{} @ (x={}, y={}), giro {}°".format(
                texto["texto"], round(texto["cx"]), round(texto["cy"]), texto["angulo"]))
        bloques.append("\n".join(lineas))
    return "\n\n".join(bloques)


def _cli(argv: Optional[list[str]] = None) -> int:
    """CLI: `python plano_vectorial.py FILE.pdf [--json] [--max-pages N]`.

    Sin --json imprime el hint (nada por stdout si el PDF no es un plano
    vectorial); con --json imprime el JSON de extraer_textos_pdf.
    """
    parser = argparse.ArgumentParser(
        description="Extrae cotas y etiquetas de un plano PDF vectorial.")
    parser.add_argument("pdf", help="Ruta del PDF")
    parser.add_argument("--json", action="store_true",
                        help="Volcar extraer_textos_pdf como JSON")
    parser.add_argument("--max-pages", type=int, default=5,
                        help="Máximo de páginas a procesar (por defecto 5)")
    args = parser.parse_args(argv)
    if args.json:
        print(json.dumps(extraer_textos_pdf(args.pdf, args.max_pages),
                         ensure_ascii=False, indent=2))
    else:
        hint = hint_plano_vectorial(args.pdf, args.max_pages)
        if hint:
            print(hint)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
