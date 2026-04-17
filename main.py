#!/usr/bin/env python3
"""
Extractor de Piezas de Cocinas de Piedra - Cocimoble
=====================================================
Uso:
    python main.py <carpeta_trabajo>           # Procesa una carpeta
    python main.py <carpeta_trabajos> --batch  # Procesa todas las subcarpetas
    python main.py <carpeta> --json            # Salida solo JSON
    python main.py <carpeta> --guardar         # Guarda resultados en archivos
"""

import argparse
import json
import os
import sys
from pathlib import Path

from claude_extractor import extract_trabajo


def procesar_una_carpeta(folder: Path, args) -> None:
    """Procesa una sola carpeta y muestra/guarda el resultado."""
    if not folder.is_dir():
        print(f"[ERROR] No es una carpeta válida: {folder}")
        return

    try:
        trabajo = extract_trabajo(
            folder=folder,
            api_key=args.api_key,
            model=args.model,
            verbose=not args.json,
        )

        if args.json:
            print(trabajo.to_json())
        else:
            print()
            print(trabajo.resumen_texto())

        if args.guardar:
            output_dir = folder
            json_path = output_dir / f"{trabajo.job_id}_extraccion.json"
            txt_path = output_dir / f"{trabajo.job_id}_extraccion.txt"

            json_path.write_text(trabajo.to_json(), encoding='utf-8')
            txt_path.write_text(trabajo.resumen_texto(), encoding='utf-8')
            if not args.json:
                print(f"\n  Guardado: {json_path.name}")
                print(f"  Guardado: {txt_path.name}")

        return trabajo

    except Exception as e:
        print(f"[ERROR] Procesando {folder.name}: {e}")
        import traceback
        traceback.print_exc()
        return None


def procesar_batch(parent_folder: Path, args) -> None:
    """Procesa todas las subcarpetas de un directorio."""
    subdirs = sorted([d for d in parent_folder.iterdir() if d.is_dir()])

    if not subdirs:
        print(f"No se encontraron subcarpetas en: {parent_folder}")
        return

    print(f"Procesando {len(subdirs)} trabajos en: {parent_folder}")
    print("=" * 60)

    resultados = []
    errores = []

    for i, folder in enumerate(subdirs, 1):
        print(f"\n[{i}/{len(subdirs)}] {folder.name}")
        try:
            trabajo = extract_trabajo(
                folder=folder,
                api_key=args.api_key,
                model=args.model,
                verbose=True,
            )

            if args.guardar:
                json_path = folder / f"{trabajo.job_id}_extraccion.json"
                txt_path = folder / f"{trabajo.job_id}_extraccion.txt"
                json_path.write_text(trabajo.to_json(), encoding='utf-8')
                txt_path.write_text(trabajo.resumen_texto(), encoding='utf-8')

            resultados.append(trabajo.to_dict())

            if not args.json:
                print(trabajo.resumen_texto())
                print("-" * 60)

        except Exception as e:
            print(f"  [ERROR] {e}")
            errores.append({'carpeta': folder.name, 'error': str(e)})

    # Resumen batch
    if args.guardar and resultados:
        batch_json = parent_folder / "batch_extraccion.json"
        batch_json.write_text(
            json.dumps(resultados, ensure_ascii=False, indent=2),
            encoding='utf-8'
        )
        print(f"\nResultados batch guardados: {batch_json}")

    print(f"\n{'='*60}")
    print(f"Completados: {len(resultados)}/{len(subdirs)}")
    if errores:
        print(f"Errores: {len(errores)}")
        for e in errores:
            print(f"  - {e['carpeta']}: {e['error']}")

    if args.json:
        print(json.dumps(resultados, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(
        description='Extractor de piezas de cocinas de piedra (Cocimoble)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  # Procesar un trabajo concreto:
  python main.py "/path/J0297_Elisa Baños_..."

  # Procesar y guardar resultados:
  python main.py "/path/J0297_Elisa Baños_..." --guardar

  # Procesar todos los trabajos de una carpeta:
  python main.py "/path/Cocimoble2026" --batch --guardar

  # Solo JSON (para integrar con otros sistemas):
  python main.py "/path/J0297..." --json
        """
    )

    parser.add_argument('carpeta', help='Carpeta del trabajo (o carpeta padre con --batch)')
    parser.add_argument('--batch', '-b', action='store_true',
                        help='Procesar todas las subcarpetas')
    parser.add_argument('--json', '-j', action='store_true',
                        help='Salida solo en formato JSON')
    parser.add_argument('--guardar', '-g', action='store_true',
                        help='Guardar resultados en archivos .json y .txt')
    parser.add_argument('--model', '-m', default='claude-sonnet-4-6',
                        help='Modelo de Claude (default: claude-sonnet-4-6)')
    parser.add_argument('--api-key', '-k', default=None,
                        help='API key de Anthropic (o usa ANTHROPIC_API_KEY env var)')

    args = parser.parse_args()

    # Verificar API key
    api_key = args.api_key or os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        print("[ERROR] Se necesita API key de Anthropic.")
        print("  Opción 1: export ANTHROPIC_API_KEY=sk-ant-...")
        print("  Opción 2: python main.py <carpeta> --api-key sk-ant-...")
        sys.exit(1)
    args.api_key = api_key

    folder = Path(args.carpeta)
    if not folder.exists():
        print(f"[ERROR] No existe: {folder}")
        sys.exit(1)

    if args.batch:
        procesar_batch(folder, args)
    else:
        procesar_una_carpeta(folder, args)


if __name__ == '__main__':
    main()
