#!/usr/bin/env python3
"""
Bench del modo autónomo del extractor de encimeras.
===================================================

Para cada carpeta de trabajo que tenga `*_extraccion_validada.json`:

  1. Oculta las anotaciones del operador (renombra `anotaciones.json` a
     `anotaciones.json.off` y `anotaciones/` a `anotaciones.off/`).
  2. Ejecuta la extracción AUTÓNOMA (sin anotaciones) y guarda
     `<job_id>_extraccion_cruda.json` y `<job_id>_extraccion_cruda.txt`
     en la propia carpeta del trabajo.
  3. Restaura SIEMPRE las anotaciones (bloque finally), incluso si la
     extracción lanza una excepción o se recibe KeyboardInterrupt.
  4. Compara cruda vs validada con `comparar_extracciones.comparar` y
     guarda `<salida>/<job_id>_comparacion.json`.

Al final escribe `<salida>/scoreboard.json` e imprime una tabla compacta.
NUNCA toca `<job_id>_extraccion.json` ni `*_extraccion_validada.json`.

Uso:
    python bench_autonomo.py CARPETA [CARPETA...] [--model M]
    python bench_autonomo.py --lote /ruta/datos/Cocimoble2026
    python bench_autonomo.py CARPETA --solo-comparar   # sin llamar a la API

La API key se lee de la variable de entorno ANTHROPIC_API_KEY (solo en
modo extracción; `--solo-comparar` funciona sin ella y sin el paquete
anthropic, pues la importación de `extract_trabajo` es perezosa).
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from comparar_extracciones import comparar

# Nombres fijos de las anotaciones del operador (a ocultar/restaurar)
ANOT_JSON = "anotaciones.json"
ANOT_JSON_OFF = "anotaciones.json.off"
ANOT_DIR = "anotaciones"
ANOT_DIR_OFF = "anotaciones.off"

SALIDA_DEFECTO = Path("bench_out")


# ---------------------------------------------------------------------------
# Ocultar / restaurar anotaciones del operador
# ---------------------------------------------------------------------------

def _ocultar_anotaciones(job_dir: Path):
    """
    Renombra (si existen) `anotaciones.json` → `anotaciones.json.off` y
    `anotaciones/` → `anotaciones.off/`. Devuelve la lista de pares
    (destino, origen) para su posterior restauración.
    """
    renombrados = []
    j = job_dir / ANOT_JSON
    if j.exists():
        j.rename(job_dir / ANOT_JSON_OFF)
        renombrados.append((job_dir / ANOT_JSON_OFF, j))
    d = job_dir / ANOT_DIR
    if d.is_dir():
        d.rename(job_dir / ANOT_DIR_OFF)
        renombrados.append((job_dir / ANOT_DIR_OFF, d))
    return renombrados


def _restaurar_anotaciones(renombrados):
    """Restaura los archivos renombrados por `_ocultar_anotaciones`."""
    for oculto, original in renombrados:
        try:
            if oculto.exists():
                oculto.rename(original)
        except OSError as e:
            print("[WARN] No se pudo restaurar %s → %s: %s"
                  % (oculto, original, e), file=sys.stderr)


# ---------------------------------------------------------------------------
# Extracción autónoma (importación perezosa del extractor)
# ---------------------------------------------------------------------------

def ejecutar_extraccion(job_dir: Path, api_key: str, model: str):
    """
    Ejecuta la extracción autónoma con extract_trabajo.
    La importación es perezosa para que --solo-comparar funcione sin el
    paquete anthropic instalado.
    """
    from claude_extractor import extract_trabajo  # importación perezosa
    return extract_trabajo(folder=job_dir, api_key=api_key,
                           model=model, verbose=False)


# ---------------------------------------------------------------------------
# Descubrimiento de jobs
# ---------------------------------------------------------------------------

def _encontrar_validada(job_dir: Path):
    """Devuelve el primer *_extraccion_validada.json de la carpeta (o None)."""
    candidatas = sorted(job_dir.glob("*_extraccion_validada.json"))
    return candidatas[0] if candidatas else None


def descubrir_jobs(args) -> list:
    """
    Devuelve la lista de carpetas de trabajo a procesar:
      - Con --lote DIR: cada subcarpeta de DIR con *_extraccion_validada.json.
      - Si no: las CARPETAs pasadas como argumento.
    """
    jobs = []
    if args.lote:
        lote = Path(args.lote)
        if not lote.is_dir():
            print("[ERROR] --lote no es una carpeta: %s" % lote,
                  file=sys.stderr)
            return jobs
        for sub in sorted(lote.iterdir()):
            if sub.is_dir() and _encontrar_validada(sub):
                jobs.append(sub)
    else:
        for c in args.carpetas:
            p = Path(c)
            if not p.is_dir():
                print("[ERROR] No es una carpeta válida: %s" % p,
                      file=sys.stderr)
            elif not _encontrar_validada(p):
                print("[ERROR] Sin *_extraccion_validada.json en: %s" % p,
                      file=sys.stderr)
            else:
                jobs.append(p)
    return jobs


# ---------------------------------------------------------------------------
# Procesamiento de un job
# ---------------------------------------------------------------------------

def procesar_job(job_dir: Path, args, salida: Path) -> dict:
    """
    Procesa un job (extracción autónoma + comparación) y devuelve su
    entrada del scoreboard: {job_id, score, sub-scores, n_discrepancias,
    error}.
    """
    validada_path = _encontrar_validada(job_dir)
    if validada_path is None:
        return {"job_id": job_dir.name, "score": None, "n_discrepancias": None,
                "error": "sin *_extraccion_validada.json"}

    try:
        validada = json.loads(validada_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {"job_id": job_dir.name, "score": None,
                "n_discrepancias": None, "error": "validada ilegible: %s" % e}

    job_id = validada.get("job_id") or job_dir.name
    entrada = {"job_id": job_id, "score": None, "piezas": None,
               "huecos": None, "cantos": None, "materiales": None,
               "n_discrepancias": None, "error": None}
    cruda_path = job_dir / ("%s_extraccion_cruda.json" % job_id)

    # --- Pasos 1-3: ocultar anotaciones, extraer, restaurar SIEMPRE ---
    if not args.solo_comparar:
        renombrados = _ocultar_anotaciones(job_dir)
        try:
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise ValueError(
                    "Falta ANTHROPIC_API_KEY en el entorno")
            trabajo = ejecutar_extraccion(job_dir, api_key, args.model)
            cruda_path.write_text(trabajo.to_json(), encoding="utf-8")
            (job_dir / ("%s_extraccion_cruda.txt" % job_id)).write_text(
                trabajo.resumen_texto(), encoding="utf-8")
        except KeyboardInterrupt:
            raise  # el finally de abajo restaura antes de propagar
        except Exception as e:
            entrada["error"] = "%s: %s" % (type(e).__name__, e)
            print("  [ERROR] Extracción de %s: %s" % (job_id, e),
                  file=sys.stderr)
        finally:
            # SIEMPRE se restauran las anotaciones, incluso con excepción
            # o KeyboardInterrupt en mitad de la extracción.
            _restaurar_anotaciones(renombrados)
        if entrada["error"]:
            return entrada

    # --- Comparación cruda vs validada ---
    salida.mkdir(parents=True, exist_ok=True)
    if not cruda_path.exists():
        entrada["error"] = "no existe %s (ejecuta el modo extracción)" \
                           % cruda_path.name
        return entrada
    try:
        cruda = json.loads(cruda_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        entrada["error"] = "cruda ilegible: %s" % e
        return entrada

    resultado = comparar(validada, cruda,
                         tol_mm=args.tol_mm, tol_pct=args.tol_pct)
    comp_path = salida / ("%s_comparacion.json" % job_id)
    comp_path.write_text(
        json.dumps(resultado, ensure_ascii=False, indent=2),
        encoding="utf-8")

    entrada.update({
        "score": resultado["score"]["total"],
        "piezas": resultado["score"]["piezas"],
        "huecos": resultado["score"]["huecos"],
        "cantos": resultado["score"]["cantos"],
        "materiales": resultado["score"]["materiales"],
        "n_discrepancias": len(resultado["discrepancias"]),
    })
    return entrada


# ---------------------------------------------------------------------------
# Scoreboard y CLI
# ---------------------------------------------------------------------------

def _imprimir_tabla(entradas):
    """Imprime la tabla compacta del scoreboard."""
    print()
    print("%-28s %7s %7s %7s %7s %7s %10s"
          % ("job_id", "total", "piezas", "huecos", "cantos", "mater.",
             "n_discrep"))
    print("-" * 82)
    for e in entradas:
        if e.get("error"):
            print("%-28s %74s" % (e["job_id"], "ERROR: %s" % e["error"]))
        else:
            print("%-28s %7.3f %7.3f %7.3f %7.3f %7.3f %10d"
                  % (e["job_id"], e["score"], e["piezas"], e["huecos"],
                     e["cantos"], e["materiales"], e["n_discrepancias"]))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Benchmark del modo autónomo: oculta las anotaciones del "
                    "operador, extrae en crudo, restaura y compara contra la "
                    "extracción validada.")
    parser.add_argument("carpetas", nargs="*",
                        help="Carpetas de trabajo con *_extraccion_validada.json")
    parser.add_argument("--lote", default=None,
                        help="Directorio padre: cada subcarpeta con "
                             "*_extraccion_validada.json es un job")
    parser.add_argument("--solo-comparar", action="store_true",
                        help="No extraer: solo comparar el "
                             "*_extraccion_cruda.json existente")
    parser.add_argument("--model", "-m", default="claude-sonnet-4-6",
                        help="Modelo para extract_trabajo")
    parser.add_argument("--salida", default=str(SALIDA_DEFECTO),
                        help="Directorio de salida (default ./bench_out)")
    parser.add_argument("--tol-mm", type=float, default=20,
                        help="Tolerancia absoluta en mm (default 20)")
    parser.add_argument("--tol-pct", type=float, default=3,
                        help="Tolerancia relativa en %% (default 3)")
    args = parser.parse_args(argv)

    if not args.carpetas and not args.lote:
        parser.error("indica al menos una CARPETA o usa --lote DIR")

    salida = Path(args.salida)
    salida.mkdir(parents=True, exist_ok=True)

    jobs = descubrir_jobs(args)
    if not jobs:
        print("[ERROR] No se encontraron jobs con *_extraccion_validada.json",
              file=sys.stderr)
        return 2

    print("Bench autónomo: %d job(s) | modelo=%s | salida=%s%s"
          % (len(jobs), args.model, salida,
             " | SOLO COMPARAR" if args.solo_comparar else ""))

    entradas = []
    try:
        for i, job_dir in enumerate(jobs, 1):
            print("\n[%d/%d] %s" % (i, len(jobs), job_dir.name))
            entrada = procesar_job(job_dir, args, salida)
            entradas.append(entrada)
            if entrada.get("error"):
                print("  → ERROR (%s)" % entrada["error"])
            else:
                print("  → total=%.3f | discrepancias=%s"
                      % (entrada["score"], entrada["n_discrepancias"]))
    except KeyboardInterrupt:
        print("\n[INTERRUMPIDO] Restaurando y guardando lo que haya...",
              file=sys.stderr)

    con_score = [e["score"] for e in entradas if e.get("score") is not None]
    media = round(sum(con_score) / len(con_score), 4) if con_score else 0.0
    scoreboard = {
        "fecha": datetime.now().isoformat(timespec="seconds"),
        "model": args.model,
        "jobs": entradas,
        "media_total": media,
    }
    (salida / "scoreboard.json").write_text(
        json.dumps(scoreboard, ensure_ascii=False, indent=2),
        encoding="utf-8")

    _imprimir_tabla(entradas)
    print("-" * 82)
    print("Media total: %.3f (%d/%d jobs con score)"
          % (media, len(con_score), len(entradas)))
    print("Scoreboard: %s" % (salida / "scoreboard.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
