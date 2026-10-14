#!/usr/bin/env python3
"""
Comparador de extracciones (validada vs cruda) para el modo autónomo.
=====================================================================

Compara dos JSON de extracción del pipeline de encimeras:

  - VALIDADA: extracción revisada por un operador (verdad de referencia).
  - CRUDA:    salida del extractor autónomo (sin anotaciones del operador).

Uso:
    python comparar_extracciones.py VALIDADA.json CRUDA.json
    python comparar_extracciones.py VALIDADA.json CRUDA.json --json resultado.json
    python comparar_extracciones.py V.json C.json --tol-mm 30 --tol-pct 5

Función principal reutilizable como librería:
    from comparar_extracciones import comparar
    resultado = comparar(validada_dict, cruda_dict, tol_mm=20, tol_pct=3)

Reglas de comparación:
  - Piezas: (largo_mm, ancho_mm) como par SIN orden. Tiras (copete,
    zócalo, frontal, costado, pilastra) emparejan por longitud efectiva
    (largo_mm o longitud_ml*1000) y segunda cota (altura_mm o ancho_mm).
  - Huecos: dimensiones como par SIN orden; posiciones solo se comparan
    si las encimeras de ambos lados están en el mismo marco (misma
    orientación de la caja); si no, se emite 'hueco_posicion_no_comparable'.
  - Materiales: el acabado se compara como conjunto de tokens alfabéticos.

La lista `discrepancias` del resultado NO contiene datos personales
(nunca se copian cliente, vendedor, direccion ni ciudad).
"""

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

# Campos numéricos de dimensión que se comparan en piezas (par sin orden).
_EPS = 1e-9
_PESO_APROXIMADA = 0.7  # peso de una pieza aproximada en el F1


# ---------------------------------------------------------------------------
# Utilidades de normalización / lectura robusta
# ---------------------------------------------------------------------------

def _lista(d, clave):
    """Devuelve d[clave] como lista; tolera None, ausencia y no-dicts."""
    if not isinstance(d, dict):
        return []
    v = d.get(clave)
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    return []


def _num(v):
    """Convierte v a float si es posible (acepta "1,2", " 3.5 ", números)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", ".")
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _quitar_acentos(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s)
        if not unicodedata.combining(c))


def _norm_texto(v):
    """Normaliza texto: strip, minúsculas, sin acentos, espacios colapsados."""
    if v is None:
        return None
    s = _quitar_acentos(str(v)).strip().lower()
    return " ".join(s.split())


def _norm_campo(v):
    """Normaliza un campo de material: texto normalizado o float ("1,2"→1.2)."""
    if v is None:
        return None
    n = _num(v)
    if n is not None:
        return round(n, 4)
    return _norm_texto(v)


def _tolerancia(ref, tol_mm, tol_pct):
    """Tolerancia absoluta para una cota de referencia: max(tol_mm, tol_pct%)."""
    ref = abs(ref) if ref is not None else 0.0
    return max(float(tol_mm), float(tol_pct) / 100.0 * abs(ref))


# Tipos "tira": piezas lineales que suelen llevar longitud_ml (metros) y
# altura_mm en vez de largo_mm/ancho_mm. Un frontal puede llevar
# largo_mm + altura_mm.
_TIPOS_TIRA = {"copete", "zocalo", "frontal", "costado", "pilastra"}


def _es_tira(p):
    return (_norm_texto(p.get("tipo")) or "") in _TIPOS_TIRA


def _cota_longitud(p):
    """(mm, campo): largo_mm si existe; si no longitud_ml*1000; si no (None, None)."""
    l = _num(p.get("largo_mm"))
    if l is not None:
        return l, "largo_mm"
    ml = _num(p.get("longitud_ml"))
    if ml is not None:
        return ml * 1000.0, "longitud_ml"
    return None, None


def _cota_segunda(p):
    """(mm, campo): altura_mm si existe; si no ancho_mm; si no (None, None)."""
    a = _num(p.get("altura_mm"))
    if a is not None:
        return a, "altura_mm"
    an = _num(p.get("ancho_mm"))
    if an is not None:
        return an, "ancho_mm"
    return None, None


def _etiqueta_cota(campo_v, campo_c):
    """Etiqueta de una cota comparada: 'largo_mm' si se comparó el mismo
    campo, o 'largo_mm/ancho_mm' si fue un match cruzado
    (validada.largo contra cruda.ancho)."""
    return campo_v if campo_v == campo_c else "%s/%s" % (campo_v, campo_c)


def _fmt_medida(comp):
    """Delta con su unidad: '5.0 mm' o '0.020 ml'."""
    return ("%.3f ml" % comp["delta"]) if comp["unidad"] == "ml" \
        else ("%.1f mm" % comp["delta"])


def _desc_medidas(p):
    """Medidas legibles para informes: tiras 'copete 0.62ml H50';
    resto de piezas '2400x600'."""
    if _es_tira(p):
        partes = []
        lg, ml = _num(p.get("largo_mm")), _num(p.get("longitud_ml"))
        if lg is not None:
            partes.append("%gmm" % lg)
        elif ml is not None:
            partes.append("%.2fml" % ml)
        alt, an = _num(p.get("altura_mm")), _num(p.get("ancho_mm"))
        if alt is not None:
            partes.append("H%g" % alt)
        elif an is not None:
            partes.append("A%g" % an)
        return " ".join(partes) if partes else "sin cotas"
    return "%sx%s" % (p.get("largo_mm"), p.get("ancho_mm"))


def _comparacion_cota(pv, pc, campo_v, campo_c, rv, rc):
    """
    Diferencia de una cota realmente comparada entre pieza validada y cruda.
    Devuelve None si son iguales o un dict con los valores MOSTRADOS
    (validada/cruda) y un delta que SIEMPRE es |validada - cruda| de esos
    valores (en ml si ambos lados usan longitud_ml, si no en mm).
    """
    d = abs(rc - rv)
    if d <= _EPS:
        return None
    if campo_v == "longitud_ml" and campo_c == "longitud_ml":
        return {"campo_validada": campo_v, "campo_cruda": campo_c,
                "validada": _num(pv.get("longitud_ml")),
                "cruda": _num(pc.get("longitud_ml")),
                "delta": round(d / 1000.0, 4), "unidad": "ml"}
    return {"campo_validada": campo_v, "campo_cruda": campo_c,
            "validada": rv, "cruda": rc, "delta": round(d, 2),
            "unidad": "mm"}


def _area(piezas, tipo=None):
    total = 0.0
    for p in piezas:
        if tipo is not None and _norm_texto(p.get("tipo")) != tipo:
            continue
        a = _num(p.get("area_m2"))
        if a:
            total += a
    return round(total, 4)


def _n_vertices(p):
    v = p.get("vertices_mm")
    return len(v) if isinstance(v, list) else 0


# ---------------------------------------------------------------------------
# Piezas: emparejamiento voraz por costo óptimo dentro de cada tipo
# ---------------------------------------------------------------------------

def _emparejar_piezas(val_p, cru_p, tol_mm, tol_pct, tira=False):
    """
    Empareja piezas validada↔cruda del MISMO tipo de forma voraz:
    calcula todos los pares candidatos (todas las cotas dentro de
    tolerancia) y los consume de menor a mayor costo relativo.

    - Piezas normales: (largo_mm, ancho_mm) como par SIN orden (igual o
      cruzado) + altura_mm si está en ambos lados.
    - Tiras (copete/zócalo/frontal/costado/pilastra): longitud efectiva
      (largo_mm o longitud_ml*1000) contra longitud efectiva y segunda
      cota (altura_mm o ancho_mm) contra segunda cota, SIN cruzar. Una
      tira sin ninguna cota comparable NO casa (nada de casar a ciegas).

    Devuelve (exactas, aproximadas, faltantes, sobrantes). Las entradas
    exactas/aproximadas llevan `comparaciones`: pares
    {campo_validada, campo_cruda, validada, cruda, delta, unidad} con los
    valores realmente comparados (útil para etiquetar matches cruzados).
    """
    candidatos = []
    if tira:
        for i, pv in enumerate(val_p):
            lv, f_lv = _cota_longitud(pv)
            dv, f_dv = _cota_segunda(pv)
            for j, pc in enumerate(cru_p):
                lc, f_lc = _cota_longitud(pc)
                dc, f_dc = _cota_segunda(pc)
                cotas = []
                if lv is not None and lc is not None:
                    cotas.append((f_lv, f_lc, lv, lc))
                if dv is not None and dc is not None:
                    cotas.append((f_dv, f_dc, dv, dc))
                if not cotas:
                    continue  # sin cotas comparables: no casar por tipo solo
                if any(abs(rc - rv) > _tolerancia(rv, tol_mm, tol_pct) + _EPS
                       for _, _, rv, rc in cotas):
                    continue
                coste = sum(abs(rc - rv) / max(abs(rv), 1.0)
                            for _, _, rv, rc in cotas)
                candidatos.append((coste, i, j, cotas))
    else:
        def dims(p):
            return _num(p.get("largo_mm")), _num(p.get("ancho_mm")), \
                _num(p.get("altura_mm"))

        def alineacion(pares):
            """
            Cotas comparadas de una alineación, como pares
            (campo_validada, campo_cruda, ref_validada, val_cruda).
            Devuelve (cotas, dentro_de_tolerancia, coste) o None si no
            hay cotas comparables en esta alineación.
            """
            cotas = [(cv, cc, r, v) for cv, cc, r, v in pares
                     if r is not None and v is not None]
            if not cotas:
                return None
            dentro = all(
                abs(v - r) <= _tolerancia(r, tol_mm, tol_pct) + _EPS
                for _, _, r, v in cotas)
            coste = sum(abs(v - r) / max(abs(r), 1.0)
                        for _, _, r, v in cotas)
            return cotas, dentro, coste

        for i, pv in enumerate(val_p):
            lv, av, alt_v = dims(pv)
            for j, pc in enumerate(cru_p):
                lc, ac, alt_c = dims(pc)
                opciones = [
                    o for o in (
                        # 600x2400 ↔ 600x2400 (identidad)
                        alineacion([("largo_mm", "largo_mm", lv, lc),
                                    ("ancho_mm", "ancho_mm", av, ac)]),
                        # 600x2400 ↔ 2400x600 (cruzado)
                        alineacion([("largo_mm", "ancho_mm", lv, ac),
                                    ("ancho_mm", "largo_mm", av, lc)]))
                    if o is not None and o[1]]
                if not opciones and (lv is not None or av is not None) and \
                        (lc is not None or ac is not None):
                    continue  # ambas tienen cotas pero ninguna alineación vale
                cotas, _, coste = min(opciones, key=lambda o: o[2]) if opciones \
                    else ([], False, 0.0)
                if alt_v is not None and alt_c is not None:
                    cotas = cotas + [("altura_mm", "altura_mm", alt_v, alt_c)]
                    if abs(alt_c - alt_v) > _tolerancia(alt_v, tol_mm, tol_pct):
                        continue
                    coste += abs(alt_c - alt_v) / max(abs(alt_v), 1.0)
                # altura presente solo en un lado: comparable pero sin cota
                candidatos.append((coste, i, j, cotas))

    # Voraz: menor costo primero; cada pieza se usa como mucho una vez
    candidatos.sort(key=lambda t: (t[0], t[1], t[2]))
    usados_v, usados_c = set(), set()
    exactas, aproximadas = [], []
    for _, i, j, cotas in candidatos:
        if i in usados_v or j in usados_c:
            continue
        usados_v.add(i)
        usados_c.add(j)
        pv, pc = val_p[i], cru_p[j]
        comparaciones = [c for c in (_comparacion_cota(pv, pc, *cota)
                                     for cota in cotas) if c is not None]
        base = {
            "tipo": pv.get("tipo"),
            "zona": pv.get("zona") or pc.get("zona"),
            "largo_mm": pv.get("largo_mm"),
            "ancho_mm": pv.get("ancho_mm"),
            "longitud_ml": pv.get("longitud_ml"),
            "altura_mm": pv.get("altura_mm"),
            "largo_mm_cruda": pc.get("largo_mm"),
            "ancho_mm_cruda": pc.get("ancho_mm"),
            "longitud_ml_cruda": pc.get("longitud_ml"),
            "altura_mm_cruda": pc.get("altura_mm"),
        }
        if comparaciones:
            base["deltas"] = {_etiqueta_cota(c["campo_validada"],
                                             c["campo_cruda"]): c["delta"]
                              for c in comparaciones}
            base["comparaciones"] = comparaciones
            aproximadas.append(base)
        else:
            exactas.append(base)
    faltantes = [p for i, p in enumerate(val_p) if i not in usados_v]
    sobrantes = [p for j, p in enumerate(cru_p) if j not in usados_c]
    return exactas, aproximadas, faltantes, sobrantes


def _comparar_piezas(validada, cruda, tol_mm, tol_pct, discrep):
    val_todas = _lista(validada, "piezas")
    cru_todas = _lista(cruda, "piezas")

    tipos = []
    for p in val_todas + cru_todas:
        t = _norm_texto(p.get("tipo")) or "otro"
        if t not in tipos:
            tipos.append(t)

    exactas, aproximadas, faltantes, sobrantes = [], [], [], []
    por_tipo = {}
    for t in sorted(tipos):
        v = [p for p in val_todas if (_norm_texto(p.get("tipo")) or "otro") == t]
        c = [p for p in cru_todas if (_norm_texto(p.get("tipo")) or "otro") == t]
        e, a, f, s = _emparejar_piezas(v, c, tol_mm, tol_pct,
                                       tira=(t in _TIPOS_TIRA))
        exactas += e
        aproximadas += a
        faltantes += f
        sobrantes += s
        por_tipo[t] = {
            "n_validada": len(v),
            "n_cruda": len(c),
            "area_m2_validada": _area(v),
            "area_m2_cruda": _area(c),
            "vertices_validada": sum(_n_vertices(p) for p in v),
            "vertices_cruda": sum(_n_vertices(p) for p in c),
        }

    for p in faltantes:
        discrep.append({
            "categoria": "pieza_faltante", "tipo": p.get("tipo"),
            "zona": p.get("zona"), "campo": None,
            "validada": {"largo_mm": p.get("largo_mm"),
                         "ancho_mm": p.get("ancho_mm"),
                         "longitud_ml": p.get("longitud_ml"),
                         "altura_mm": p.get("altura_mm")},
            "cruda": None, "delta": None,
            "detalle": "pieza %s %s no extraída"
                       % (p.get("tipo"), _desc_medidas(p)),
        })
    for p in sobrantes:
        discrep.append({
            "categoria": "pieza_sobrante", "tipo": p.get("tipo"),
            "zona": p.get("zona"), "campo": None,
            "validada": None,
            "cruda": {"largo_mm": p.get("largo_mm"),
                      "ancho_mm": p.get("ancho_mm"),
                      "longitud_ml": p.get("longitud_ml"),
                      "altura_mm": p.get("altura_mm")},
            "delta": None,
            "detalle": "pieza %s %s de más"
                       % (p.get("tipo"), _desc_medidas(p)),
        })
    for m in aproximadas:
        # Cada comparación lleva los campos y valores realmente comparados:
        # en un match cruzado se etiqueta validada.largo vs cruda.ancho y el
        # delta siempre es |validada - cruda| de los valores mostrados.
        for comp in sorted(m["comparaciones"],
                           key=lambda c: (c["campo_validada"],
                                          c["campo_cruda"])):
            cv, cc = comp["campo_validada"], comp["campo_cruda"]
            if cv == cc:
                campo = cv
                detalle = "cota %s difiere %s (dentro de tolerancia)" % (
                    cv, _fmt_medida(comp))
            else:
                campo = _etiqueta_cota(cv, cc)
                detalle = ("cota validada.%s vs cruda.%s difiere %s "
                           "(dentro de tolerancia)"
                           % (cv, cc, _fmt_medida(comp)))
            discrep.append({
                "categoria": "pieza_dim", "tipo": m.get("tipo"),
                "zona": m.get("zona"), "campo": campo,
                "validada": comp["validada"], "cruda": comp["cruda"],
                "delta": comp["delta"], "detalle": detalle,
            })

    return {
        "exactas": exactas,
        "aproximadas": aproximadas,
        "faltantes": [
            {"tipo": p.get("tipo"), "zona": p.get("zona"),
             "largo_mm": p.get("largo_mm"), "ancho_mm": p.get("ancho_mm"),
             "longitud_ml": p.get("longitud_ml"),
             "altura_mm": p.get("altura_mm")}
            for p in faltantes],
        "sobrantes": [
            {"tipo": p.get("tipo"), "zona": p.get("zona"),
             "largo_mm": p.get("largo_mm"), "ancho_mm": p.get("ancho_mm"),
             "longitud_ml": p.get("longitud_ml"),
             "altura_mm": p.get("altura_mm")}
            for p in sobrantes],
        "por_tipo": por_tipo,
    }


def _score_piezas(res, n_val, n_cru):
    tp = len(res["exactas"]) + _PESO_APROXIMADA * len(res["aproximadas"])
    if n_val == 0 and n_cru == 0:
        return 1.0
    p = tp / n_cru if n_cru else 1.0
    r = tp / n_val if n_val else 1.0
    return round(2 * p * r / (p + r), 4) if (p + r) > 0 else 0.0


# ---------------------------------------------------------------------------
# Huecos, cantos, materiales, advertencias
# ---------------------------------------------------------------------------

_TIPOS_HUECO = ["placa", "fregadero", "grifo", "enchufe", "otros"]


def _hueco_tipo(h):
    t = _norm_texto(h.get("tipo")) or "otros"
    return t if t in ("placa", "fregadero", "grifo", "enchufe") else "otros"


def _cantidad(hs):
    total = 0
    for h in hs:
        c = h.get("cantidad", 1)
        n = _num(c)
        total += int(n) if n is not None else 1
    return total


def _marcos_por_zona(extraccion):
    """zona normalizada → (largo_mm, ancho_mm) de la encimera/isla de esa zona
    (bounding box del marco al que pertenecen los huecos)."""
    out = {}
    for p in _lista(extraccion, "piezas"):
        if _norm_texto(p.get("tipo")) not in ("encimera", "isla"):
            continue
        z = _norm_texto(p.get("zona"))
        if z is None or z in out:
            continue
        out[z] = (_num(p.get("largo_mm")), _num(p.get("ancho_mm")))
    return out


def _marco_de_hueco(h, marcos):
    z = _norm_texto(h.get("pieza_zona"))
    return marcos.get(z) if z else None


def _marcos_comparables(m_v, m_c, tol_mm, tol_pct):
    """
    ¿Están las encimeras de dos huecos emparejados en el mismo marco
    (misma orientación)? Se exige igualdad NO cruzada de la caja:
    |largo_v - largo_c| <= tol y |ancho_v - ancho_c| <= tol. Si algún
    marco es desconocido se asume comparable (comportamiento histórico).
    """
    if not m_v or not m_c:
        return True
    (lv, av), (lc, ac) = m_v, m_c
    if lv is None or av is None or lc is None or ac is None:
        return True
    return (abs(lc - lv) <= _tolerancia(lv, tol_mm, tol_pct) and
            abs(ac - av) <= _tolerancia(av, tol_mm, tol_pct))


def _fmt_marco(m):
    if not m:
        return "?x?"
    return "%gx%g" % (m[0], m[1])


def _alinear_dims_hueco(hv, hc):
    """
    Mejor alineación de (largo_mm, ancho_mm) del hueco como par SIN orden
    (400x500 == 500x400). Devuelve (pares, coste); pares es una lista de
    (campo_validada, campo_cruda, ref_validada, val_cruda) de la mejor
    alineación (identidad o cruzada) por diferencia relativa.
    """
    lv, av = _num(hv.get("largo_mm")), _num(hv.get("ancho_mm"))
    lc, ac = _num(hc.get("largo_mm")), _num(hc.get("ancho_mm"))
    mejor, mejor_coste = [], 0.0
    for pares in (
            (("largo_mm", "largo_mm", lv, lc),
             ("ancho_mm", "ancho_mm", av, ac)),
            (("largo_mm", "ancho_mm", lv, ac),
             ("ancho_mm", "largo_mm", av, lc))):
        cotas = [(cv, cc, r, v) for cv, cc, r, v in pares
                 if r is not None and v is not None]
        if not cotas:
            continue
        coste = sum(abs(v - r) / max(abs(r), 1.0) for _, _, r, v in cotas)
        if not mejor or coste < mejor_coste:
            mejor, mejor_coste = cotas, coste
    return mejor, mejor_coste


def _emparejar_huecos(v_list, c_list, tol_mm, tol_pct, marcos_v, marcos_c):
    """
    Emparejamiento voraz de huecos por posición y dimensiones.
    Las dimensiones se comparan como par SIN orden; las posiciones solo
    cuentan para el coste cuando los marcos de ambas extracciones son
    comparables. Todos los pares son candidatos (coste = diferencia
    relativa); la tolerancia NO limita el emparejamiento, solo decide qué
    deltas se reportan como discrepancia.
    """
    candidatos = []
    for i, hv in enumerate(v_list):
        for j, hc in enumerate(c_list):
            pares_dim, coste = _alinear_dims_hueco(hv, hc)
            mv = _marco_de_hueco(hv, marcos_v)
            mc = _marco_de_hueco(hc, marcos_c)
            if _marcos_comparables(mv, mc, tol_mm, tol_pct):
                for campo in ("centro_x_mm", "centro_y_mm"):
                    rv, rc = _num(hv.get(campo)), _num(hc.get(campo))
                    if rv is not None and rc is not None:
                        coste += abs(rc - rv) / max(abs(rv), 1.0)
            candidatos.append((coste, i, j, pares_dim, mv, mc))
    candidatos.sort(key=lambda t: (t[0], t[1], t[2]))
    usados_v, usados_c, pares = set(), set(), []
    for _, i, j, pares_dim, mv, mc in candidatos:
        if i in usados_v or j in usados_c:
            continue
        usados_v.add(i)
        usados_c.add(j)
        pares.append((v_list[i], c_list[j], pares_dim, mv, mc))
    return pares


def _comparar_huecos(validada, cruda, tol_mm, tol_pct, discrep):
    val_h = _lista(validada, "huecos")
    cru_h = _lista(cruda, "huecos")
    marcos_v = _marcos_por_zona(validada)
    marcos_c = _marcos_por_zona(cruda)
    # Suelo de tolerancia para posiciones: max(50 mm, tol_mm).
    tol_pos = max(50.0, float(tol_mm))
    resultado = {}
    for t in _TIPOS_HUECO:
        v = [h for h in val_h if _hueco_tipo(h) == t]
        c = [h for h in cru_h if _hueco_tipo(h) == t]
        n_v, n_c = _cantidad(v), _cantidad(c)
        entry = {"validada": n_v, "cruda": n_c, "delta": n_c - n_v,
                 "emparejados": []}
        if n_v != n_c:
            discrep.append({
                "categoria": "hueco_cantidad", "tipo": t, "zona": None,
                "campo": "cantidad", "validada": n_v, "cruda": n_c,
                "delta": n_c - n_v,
                "detalle": "huecos %s: %d en validada vs %d en cruda"
                           % (t, n_v, n_c),
            })
        if t in ("placa", "fregadero") and v and c:
            for hv, hc, pares_dim, mv, mc in _emparejar_huecos(
                    v, c, tol_mm, tol_pct, marcos_v, marcos_c):
                par = {}
                # Dimensiones como par SIN orden: solo se reporta hueco_dim
                # cuando el par sin orden difiere fuera de tolerancia.
                for cv, cc, rv, rc in pares_dim:
                    d = round(abs(rc - rv), 2)
                    par["delta_" + cv] = d
                    if d > _EPS and d > _tolerancia(rv, tol_mm, tol_pct):
                        if cv == cc:
                            campo = cv
                            detalle = ("hueco %s: %s difiere %.1f mm"
                                       % (t, cv, d))
                        else:
                            campo = _etiqueta_cota(cv, cc)
                            detalle = ("hueco %s: validada.%s vs cruda.%s "
                                       "difiere %.1f mm" % (t, cv, cc, d))
                        discrep.append({
                            "categoria": "hueco_dim", "tipo": t,
                            "zona": hv.get("pieza_zona"), "campo": campo,
                            "validada": rv, "cruda": rc, "delta": d,
                            "detalle": detalle,
                        })
                # Posiciones: solo comparables si ambas encimeras están en
                # el mismo marco (misma orientación de la caja).
                if _marcos_comparables(mv, mc, tol_mm, tol_pct):
                    for campo in ("centro_x_mm", "centro_y_mm"):
                        rv, rc = _num(hv.get(campo)), _num(hc.get(campo))
                        if rv is None or rc is None:
                            continue
                        d = round(abs(rc - rv), 2)
                        par["delta_" + campo] = d
                        if d > tol_pos + _EPS:
                            discrep.append({
                                "categoria": "hueco_posicion", "tipo": t,
                                "zona": hv.get("pieza_zona"), "campo": campo,
                                "validada": rv, "cruda": rc, "delta": d,
                                "detalle": "hueco %s: %s difiere %.1f mm"
                                           % (t, campo, d),
                            })
                elif any(_num(hv.get(c)) is not None and
                         _num(hc.get(c)) is not None
                         for c in ("centro_x_mm", "centro_y_mm")):
                    # Marcos con distinta orientación: los centros no son
                    # comparables entre marcos. UNA discrepancia por hueco,
                    # que NO cuenta en el score de huecos (ese score solo
                    # usa cantidades).
                    discrep.append({
                        "categoria": "hueco_posicion_no_comparable",
                        "tipo": t, "zona": hv.get("pieza_zona"),
                        "campo": None,
                        "validada": _fmt_marco(mv), "cruda": _fmt_marco(mc),
                        "delta": None,
                        "detalle": "marcos distintos: validada %s vs cruda %s"
                                   % (_fmt_marco(mv), _fmt_marco(mc)),
                    })
                entry["emparejados"].append(par)
        resultado[t] = entry
    return resultado


def _score_huecos(res_huecos):
    errores = []
    for t, e in res_huecos.items():
        if e["validada"] or e["cruda"]:
            denom = max(e["validada"], e["cruda"], 1)
            errores.append(abs(e["delta"]) / denom)
    return round(1.0 - (sum(errores) / len(errores)), 4) if errores else 1.0


def _comparar_cantos(validada, cruda, discrep):
    def suma(cantos):
        out = {}
        for c in cantos:
            t = _norm_texto(c.get("tipo")) or "otro"
            ml = _num(c.get("longitud_ml")) or 0.0
            out[t] = round(out.get(t, 0.0) + ml, 4)
        return out

    s_v = suma(_lista(validada, "cantos"))
    s_c = suma(_lista(cruda, "cantos"))
    resultado = {}
    for t in sorted(set(s_v) | set(s_c)):
        v, c = s_v.get(t, 0.0), s_c.get(t, 0.0)
        delta = round(c - v, 4)
        rel = abs(delta) / v if v > _EPS else (1.0 if abs(delta) > _EPS else 0.0)
        resultado[t] = {"validada_ml": v, "cruda_ml": c,
                        "delta_ml": delta, "delta_rel": round(rel, 4)}
        if abs(delta) > 0.01 and rel > 0.01:
            discrep.append({
                "categoria": "canto_ml", "tipo": t, "zona": None,
                "campo": "longitud_ml", "validada": v, "cruda": c,
                "delta": delta,
                "detalle": "canto %s: %.2f ml vs %.2f ml" % (t, v, c),
            })
    return resultado


def _score_cantos(res_cantos):
    valores = []
    for e in res_cantos.values():
        if e["validada_ml"] > _EPS or e["cruda_ml"] > _EPS:
            valores.append(1.0 - min(1.0, e["delta_rel"]))
    return round(sum(valores) / len(valores), 4) if valores else 1.0


_CAMPOS_MATERIAL = ("marca", "color", "grosor_cm", "acabado")


def _tokens_acabado(v):
    """
    Normaliza el acabado como CONJUNTO de tokens alfabéticos, de modo que
    'Pulido (Polish)' == 'Polish (pulido)' (orden y paréntesis dan igual).
    Los valores numéricos se comparan tal cual.
    """
    if v is None:
        return None
    n = _norm_campo(v)
    if isinstance(n, float):
        return n
    return frozenset(re.findall(r"[a-z]+", n))


def _comparar_materiales(validada, cruda, discrep):
    def por_rol(d):
        out = {}
        for m in _lista(d, "materiales"):
            rol = _norm_texto(m.get("rol")) or "otro"
            out.setdefault(rol, []).append(m)
        return out

    roles_v = por_rol(validada)
    roles_c = por_rol(cruda)
    resultado = {}
    iguales, comparados = 0, 0
    for rol in sorted(set(roles_v) | set(roles_c)):
        entry = {"campos_iguales": 0, "campos_distintos": 0, "diferencias": []}
        if rol not in roles_v or rol not in roles_c:
            entry["ausente_en"] = "cruda" if rol in roles_v else "validada"
            discrep.append({
                "categoria": "material_campo", "tipo": rol, "zona": None,
                "campo": "rol", "validada": rol in roles_v,
                "cruda": rol in roles_c, "delta": None,
                "detalle": "material rol '%s' ausente en %s"
                           % (rol, entry["ausente_en"]),
            })
            resultado[rol] = entry
            continue
        mv = roles_v[rol][0]
        mc = roles_c[rol][0]
        for campo in _CAMPOS_MATERIAL:
            if campo == "acabado":
                nv = _tokens_acabado(mv.get(campo))
                nc = _tokens_acabado(mc.get(campo))
            else:
                nv, nc = _norm_campo(mv.get(campo)), _norm_campo(mc.get(campo))
            if nv is None and nc is None:
                continue
            comparados += 1
            if nv == nc:
                iguales += 1
                entry["campos_iguales"] += 1
            else:
                entry["campos_distintos"] += 1
                entry["diferencias"].append(
                    {"campo": campo,
                     "validada": mv.get(campo), "cruda": mc.get(campo)})
                discrep.append({
                    "categoria": "material_campo", "tipo": rol, "zona": None,
                    "campo": campo, "validada": mv.get(campo),
                    "cruda": mc.get(campo), "delta": None,
                    "detalle": "material %s: %s ≠ %s"
                               % (rol, mv.get(campo), mc.get(campo)),
                })
        resultado[rol] = entry
    score = round(iguales / comparados, 4) if comparados else 1.0
    return resultado, score


# ---------------------------------------------------------------------------
# Función principal de librería
# ---------------------------------------------------------------------------

def comparar(validada: dict, cruda: dict, tol_mm: float = 20,
             tol_pct: float = 3) -> dict:
    """
    Compara dos extracciones (dicts tal cual to_dict()) y devuelve un dict
    con piezas/huecos/cantos/materiales/advertencias/discrepancias/score.
    Sin datos personales: nunca copia cliente/vendedor/direccion/ciudad.
    """
    if not isinstance(validada, dict):
        raise TypeError("validada debe ser un dict")
    if not isinstance(cruda, dict):
        raise TypeError("cruda debe ser un dict")

    discrepancias = []
    piezas = _comparar_piezas(validada, cruda, tol_mm, tol_pct, discrepancias)
    huecos = _comparar_huecos(validada, cruda, tol_mm, tol_pct, discrepancias)
    cantos = _comparar_cantos(validada, cruda, discrepancias)
    materiales, sc_mat = _comparar_materiales(validada, cruda, discrepancias)

    n_val = len(_lista(validada, "piezas"))
    n_cru = len(_lista(cruda, "piezas"))
    sc_pie = _score_piezas(piezas, n_val, n_cru)
    sc_hue = _score_huecos(huecos)
    sc_can = _score_cantos(cantos)
    total = round(0.5 * sc_pie + 0.2 * sc_hue + 0.2 * sc_can + 0.1 * sc_mat, 4)

    return {
        "job_id": validada.get("job_id") or cruda.get("job_id"),
        "tolerancias": {"tol_mm": tol_mm, "tol_pct": tol_pct},
        "piezas": piezas,
        "huecos": huecos,
        "cantos": cantos,
        "materiales": materiales,
        "advertencias": {
            "validada": len(validada.get("advertencias") or []),
            "cruda": len(cruda.get("advertencias") or []),
        },
        "discrepancias": discrepancias,
        "score": {
            "piezas": sc_pie, "huecos": sc_hue, "cantos": sc_can,
            "materiales": sc_mat, "total": total,
        },
    }


# ---------------------------------------------------------------------------
# Informe legible y CLI
# ---------------------------------------------------------------------------

def informe_texto(r: dict) -> str:
    """Genera el informe legible en español a partir del resultado."""
    lineas = []
    sc = r["score"]
    lineas.append("=" * 62)
    lineas.append("COMPARACIÓN DE EXTRACCIONES — job %s" % r.get("job_id", "?"))
    lineas.append("Tolerancias: ±%s mm o ±%s%% (la mayor)"
                  % (r["tolerancias"]["tol_mm"], r["tolerancias"]["tol_pct"]))
    lineas.append("=" * 62)

    pz = r["piezas"]
    lineas.append("")
    lineas.append("--- PIEZAS ---")
    lineas.append("  Exactas: %d | Aproximadas: %d | Faltantes: %d | "
                  "Sobrantes: %d" % (len(pz["exactas"]),
                                     len(pz["aproximadas"]),
                                     len(pz["faltantes"]),
                                     len(pz["sobrantes"])))
    for m in pz["aproximadas"]:
        lineas.append("  ~ %s %s: deltas %s"
                      % (m["tipo"], m.get("zona") or "-", m.get("deltas")))
    for p in pz["faltantes"]:
        lineas.append("  - FALTA %s %s (%s)"
                      % (p["tipo"], _desc_medidas(p), p.get("zona") or "-"))
    for p in pz["sobrantes"]:
        lineas.append("  + SOBRA %s %s (%s)"
                      % (p["tipo"], _desc_medidas(p), p.get("zona") or "-"))
    for t, e in sorted(pz["por_tipo"].items()):
        lineas.append("  [%s] n: %d→%d | área m²: %.3f→%.3f | vértices: "
                      "%d→%d" % (t, e["n_validada"], e["n_cruda"],
                                 e["area_m2_validada"], e["area_m2_cruda"],
                                 e["vertices_validada"], e["vertices_cruda"]))

    lineas.append("")
    lineas.append("--- HUECOS ---")
    for t, e in r["huecos"].items():
        if e["validada"] or e["cruda"]:
            extra = ""
            if e["emparejados"]:
                extra = " | emparejados: %s" % e["emparejados"]
            lineas.append("  %s: %d→%d (delta %+d)%s"
                          % (t, e["validada"], e["cruda"], e["delta"], extra))

    lineas.append("")
    lineas.append("--- CANTOS ---")
    for t, e in sorted(r["cantos"].items()):
        lineas.append("  %s: %.2f→%.2f ml (Δ%+.2f, %+.1f%%)"
                      % (t, e["validada_ml"], e["cruda_ml"],
                         e["delta_ml"], e["delta_rel"] * 100))

    lineas.append("")
    lineas.append("--- MATERIALES ---")
    for rol, e in sorted(r["materiales"].items()):
        if "ausente_en" in e:
            lineas.append("  [%s] AUSENTE en %s" % (rol, e["ausente_en"]))
        else:
            lineas.append("  [%s] %d iguales / %d distintos %s"
                          % (rol, e["campos_iguales"], e["campos_distintos"],
                             e["diferencias"] or ""))

    lineas.append("")
    lineas.append("--- ADVERTENCIAS ---")
    lineas.append("  validada: %d | cruda: %d"
                  % (r["advertencias"]["validada"], r["advertencias"]["cruda"]))

    lineas.append("")
    lineas.append("--- DISCREPANCIAS (%d) ---" % len(r["discrepancias"]))
    for d in r["discrepancias"]:
        lineas.append("  · [%s] %s %s: %s"
                      % (d["categoria"], d["tipo"], d["campo"] or "",
                         d["detalle"]))

    lineas.append("")
    lineas.append("=" * 62)
    lineas.append("SCORE: total=%.3f | piezas=%.3f | huecos=%.3f | "
                  "cantos=%.3f | materiales=%.3f"
                  % (sc["total"], sc["piezas"], sc["huecos"],
                     sc["cantos"], sc["materiales"]))
    lineas.append("=" * 62)
    return "\n".join(lineas)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Compara una extracción VALIDADA (operador) con una "
                    "extracción CRUDA (modo autónomo).")
    parser.add_argument("validada", help="JSON de extracción validada")
    parser.add_argument("cruda", help="JSON de extracción cruda (autónoma)")
    parser.add_argument("--json", dest="salida_json", default=None,
                        help="Guardar el resultado como JSON en este archivo")
    parser.add_argument("--tol-mm", type=float, default=20,
                        help="Tolerancia absoluta en mm (default 20)")
    parser.add_argument("--tol-pct", type=float, default=3,
                        help="Tolerancia relativa en %% (default 3)")
    args = parser.parse_args(argv)

    try:
        validada = json.loads(
            Path(args.validada).read_text(encoding="utf-8"))
        cruda = json.loads(Path(args.cruda).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print("[ERROR] No se pudo leer los JSON: %s" % e, file=sys.stderr)
        return 2

    resultado = comparar(validada, cruda,
                         tol_mm=args.tol_mm, tol_pct=args.tol_pct)
    print(informe_texto(resultado))

    if args.salida_json:
        Path(args.salida_json).write_text(
            json.dumps(resultado, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print("\nResultado JSON guardado en: %s" % args.salida_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
