#!/usr/bin/env python3
"""
clasificador.py — Clasificador de contenido por archivo/página de un proyecto.

Para cada PDF (página a página) e imagen del proyecto, pide a Claude que diga
qué tipo de contenido es. El resultado guía la extracción posterior:
  - Forma → solo de plano-* (CAD > anotado > manuscrito)
  - MGR → MGR-tabla
  - Huecos/material → plantilla-texto
  - Perspectiva 3D → solo contexto, NUNCA cotas

Uso:
    python clasificador.py <carpeta_proyecto>
    python clasificador.py <carpeta_padre> --batch
"""

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from typing import Optional

import anthropic

from file_readers import image_to_base64, pdf_pages_to_base64, IMG_EXTENSIONS, should_ignore

CATEGORIAS = [
    "plano-planta-2D-cad",
    "plano-manuscrito",
    "plano-anotado",
    "perspectiva-3D",
    "plantilla-texto",
    "MGR-tabla",
    "foto-cocina",
    "whatsapp-plantilla",
    "otro",
]

PROMPT = """Eres un clasificador de contenido para una marmolería que fabrica encimeras de cocina. Te paso UNA imagen (o página de PDF). Clasifícala en EXACTAMENTE UNA categoría:

1. **plano-planta-2D-cad**: Vista AÉREA de la cocina dibujada con software (programa 2020 u similar). Líneas finas regulares, cotas en mm impresas, símbolos de electrodomésticos vistos desde arriba.
2. **plano-manuscrito**: Croquis hecho a mano (líneas irregulares), normalmente sobre papel cuadriculado, cotas a mano.
3. **plano-anotado**: Plano CAD con anotaciones MANUSCRITAS encima (cotas escritas a mano, marcas, X).
4. **perspectiva-3D**: Render 3D de la cocina montada (vista frontal o axonométrica). NO útil para cotas precisas.
5. **plantilla-texto**: Formulario con campos rellenados (MARCA, COLOR, GROSOR, FREGADERO, etc.), SIN dibujo de la cocina.
6. **MGR-tabla**: Presupuesto del marmolista. Membrete "Mármoles y Granitos Redondela" o tabla con columnas Longo/Ancho/Precio/Total.
7. **foto-cocina**: Fotografía real de la cocina física (muebles, electrodomésticos visibles).
8. **whatsapp-plantilla**: Foto de WhatsApp de un croquis del operador en papel/tablet (típicamente desenfocada, recorte irregular).
9. **otro**: Portadas, condiciones de venta, páginas en blanco, hojas administrativas, etc.

Devuelve SOLO JSON válido (sin texto extra), con esta estructura:
{
  "categoria": "una de las 9",
  "confianza": "alta|media|baja",
  "tiene_cotas": true|false,
  "cotas_visibles_mm": ["1500", "600", "2.5", "..."],
  "justificacion": "una frase corta"
}

cotas_visibles_mm: hasta 8 cotas que veas (en mm o en m, tal cual aparecen). Si la imagen es perspectiva-3D o foto-cocina o MGR-tabla o plantilla-texto, déjalo vacío.
tiene_cotas: true solo si hay medidas asociadas a la geometría (NO si son medidas en una tabla MGR).
"""


def clasificar_imagen_b64(client, b64_data: str, media_type: str, model: str) -> dict:
    """Clasifica una imagen ya en base64."""
    msg = client.messages.create(
        model=model,
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64_data}},
                {"type": "text", "text": PROMPT},
            ],
        }],
    )
    text = msg.content[0].text.strip()
    # Limpia markdown fences si los hay
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        return {"categoria": "otro", "confianza": "baja",
                "tiene_cotas": False, "cotas_visibles_mm": [],
                "justificacion": f"parse error: {text[:80]}"}


def clasificar_proyecto(folder: Path, api_key: str, model: str = "claude-haiku-4-5-20251001",
                          verbose: bool = True) -> dict:
    """Clasifica todos los archivos del proyecto y devuelve dict con resultados."""
    client = anthropic.Anthropic(api_key=api_key)

    resultado = {
        "proyecto": folder.name,
        "modelo": model,
        "archivos": {},
    }

    for archivo in sorted(folder.iterdir()):
        if archivo.is_dir() or should_ignore(archivo):
            continue
        ext = archivo.suffix.lower()

        if ext in IMG_EXTENSIONS:
            if verbose:
                print(f"  📷 {archivo.name}")
            try:
                b64, mt = image_to_base64(archivo)
                cat = clasificar_imagen_b64(client, b64, mt, model)
                resultado["archivos"][archivo.name] = cat
                if verbose:
                    print(f"      → {cat.get('categoria')} ({cat.get('confianza')})")
            except Exception as e:
                resultado["archivos"][archivo.name] = {"error": str(e)}
                if verbose:
                    print(f"      ✗ error: {e}")

        elif ext == ".pdf":
            if verbose:
                print(f"  📄 {archivo.name}")
            try:
                paginas_b64 = pdf_pages_to_base64(archivo, dpi=140, max_pages=10)
                paginas_clas = []
                for i, item in enumerate(paginas_b64, 1):
                    b64, mt = item[0], item[1]
                    cat = clasificar_imagen_b64(client, b64, mt, model)
                    cat["pagina"] = i
                    paginas_clas.append(cat)
                    if verbose:
                        print(f"      pág {i} → {cat.get('categoria')} ({cat.get('confianza')})")
                resultado["archivos"][archivo.name] = {"paginas": paginas_clas}
            except Exception as e:
                resultado["archivos"][archivo.name] = {"error": str(e)}
                if verbose:
                    print(f"      ✗ error: {e}")

    return resultado


def main():
    p = argparse.ArgumentParser()
    p.add_argument("ruta")
    p.add_argument("--batch", action="store_true",
                    help="Si es carpeta de proyectos, procesar cada subcarpeta")
    p.add_argument("--api-key", default=os.environ.get("ANTHROPIC_API_KEY"))
    p.add_argument("--model", default="claude-haiku-4-5-20251001",
                    help="Modelo a usar (haiku es rápido y barato; sonnet más preciso)")
    p.add_argument("--guardar", action="store_true", default=True,
                    help="Guardar clasificacion.json en cada carpeta de proyecto")
    args = p.parse_args()

    if not args.api_key:
        print("Falta ANTHROPIC_API_KEY")
        sys.exit(1)

    ruta = Path(args.ruta)

    if args.batch and ruta.is_dir():
        subdirs = [d for d in sorted(ruta.iterdir()) if d.is_dir()]
        for d in subdirs:
            print(f"\n=== {d.name} ===")
            res = clasificar_proyecto(d, args.api_key, args.model)
            if args.guardar:
                (d / "clasificacion.json").write_text(
                    json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        print(f"=== {ruta.name} ===")
        res = clasificar_proyecto(ruta, args.api_key, args.model)
        if args.guardar:
            (ruta / "clasificacion.json").write_text(
                json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
