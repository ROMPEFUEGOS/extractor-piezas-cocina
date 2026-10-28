#!/usr/bin/env python3
"""OCR local de planos de cocina escaneados y plantillas manuscritas (RapidOCR, PP-OCRv5, ONNX, CPU).

Sustituye a EasyOCR (``run_easyocr_on_image`` de file_readers.py) para imágenes raster:
planos CAD impresos y escaneados (2480x3508, 300 dpi) y plantillas rellenadas a boli.
Todo el proceso es local (los modelos se descargan una vez desde ModelScope y no sale
ninguna imagen de la máquina).

Piezas principales
------------------
- :func:`ocr_imagen` — OCR completo con cajas en píxeles, orientación de página y por línea.
- :func:`hint_ocr` — bloque compacto en español para pastar a un LLM.
- :func:`ocr_hint_imagen` — conveniencia (OCR + hint), nunca lanza excepciones.
- :func:`lectura_girada_180` — lectura de un número puramente simétrico girado 180°.
- :func:`angulo_linea` — dirección de lectura de una línea sobre la página.

Convenciones verificadas empíricamente (texto sintético + cotas verticales 1700/1620/2280
de las muestras J0030-2/J0033-2)
--------------------------------------------------------------------------------------
RapidOCR recorta cada caja con perspectiva; si el recorte es vertical (alto/ancho >= 1.5)
lo rota 90° **antihorario** (``np.rot90``) antes de pasarlo al clasificador de línea (cls)
y al reconocedor (rec). El cls devuelve "0" (franja ya legible) o "180" (franja boca abajo;
el pipeline la rota 180° antes del rec). Por tanto, sobre la página ORIGINAL:

- caja horizontal + cls "0"   -> angulo 0   (lee de izquierda a derecha, derecho)
- caja horizontal + cls "180" -> angulo 180 (boca abajo)
- caja vertical   + cls "0"   -> angulo 270 (lee de ARRIBA hacia ABAJO)
- caja vertical   + cls "180" -> angulo 90  (lee de ABAJO hacia ARRIBA)
- cls desconocido (None)      -> 0 en horizontal, 90 en vertical (dirección indeterminada)

Formatos de entrada de ``ocr_imagen``
-------------------------------------
- ``PIL.Image`` (cualquier modo; se convierte a RGB y luego a BGR).
- ``numpy.ndarray``: se interpreta como **BGR** (convención OpenCV). Se aceptan 2 canales
  (gris), 3 (BGR) y 4 (BGRA).
- ruta ``str``/``pathlib.Path`` de una imagen legible por OpenCV.

Las cajas devueltas son cuadriláteros ``[[x,y] x 4]`` en píxeles de la imagen ORIGINAL
(sin rotar), con x en [0, ancho) e y en [0, alto).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from importlib import metadata
from pathlib import Path
from typing import Any

import numpy as np

# Dígitos que girados 180° se leen como otro dígito: 0,1,8 y 5 invariables, 6<->9.
# Nota: se incluye el 5 porque en la fuente de estos planos el 5 girado se sigue leyendo 5
# (gt.json: la cota 1550 aparece como 0551 en lectura girada); 2,3,4 y 7 no son legibles.
DIGITOS_SIMETRICOS = set("015689")

_MAPA_180 = str.maketrans({"6": "9", "9": "6"})  # 0, 1, 5 y 8 no cambian

UMBRAL_CLS = 0.9          # confianza mínima del clasificador 0/180 para dar por buena la línea
UMBRAL_TEXTO = 0.3        # filtro de líneas por confianza del reconocedor
MIN_LINEAS_VOTACION = 5   # mínimo de líneas con cls fiable para votar el giro de página

_CONFIG_REC: dict[str, dict[str, str]] = {
    "latin_mobile": {"lang": "latin", "mtype": "mobile"},
    "ch_server": {"lang": "ch", "mtype": "server"},
    "ch_mobile": {"lang": "ch", "mtype": "mobile"},
    "en_mobile": {"lang": "en", "mtype": "mobile"},
}

# Caché perezosa de motores: una instancia por (rec, usar_cls) y por proceso.
_MOTORES: dict[tuple[str, bool], tuple[Any, dict[str, Any]]] = {}


def lectura_girada_180(token: str) -> str | None:
    """Lectura de ``token`` tras girarlo 180° (invertir el orden y 6<->9).

    Solo funciona con dígitos simétricos (:data:`DIGITOS_SIMETRICOS`); si algún carácter
    no lo es, devuelve ``None``.

    >>> lectura_girada_180("009")
    '600'
    >>> lectura_girada_180("0551")
    '1550'
    >>> lectura_girada_180("1991")
    '1661'
    >>> lectura_girada_180("1234") is None
    True
    """
    if not token or any(c not in DIGITOS_SIMETRICOS for c in token):
        return None
    return token[::-1].translate(_MAPA_180)


def angulo_linea(caja, cls_label: str | None) -> int:
    """Dirección de lectura de una línea de texto sobre la página, en grados.

    - 0: lee de izquierda a derecha (derecha y derecho).
    - 90: lee de abajo hacia arriba.
    - 180: boca abajo.
    - 270: lee de arriba hacia abajo.

    ``caja`` es el cuadrilátero ``[[x,y] x 4]`` en píxeles; una caja es vertical cuando
    alto/ancho >= 1.5 (mismo criterio que usa RapidOCR al rotar el recorte 90° antihorario
    antes del cls/rec). Convenión (verificada con texto sintético y las cotas verticales
    1700/1620/2280 de las muestras; ver docstring del módulo):

    ====================  ==========  ==============================
    caja                  cls_label   angulo
    ====================  ==========  ==============================
    horizontal (w > h)    "0"         0
    horizontal            "180"       180
    vertical (h/w >= 1.5) "0"         270 (lee hacia abajo)
    vertical              "180"       90 (lee hacia arriba)
    cualquiera            None        0 horizontal / 90 vertical
    ====================  ==========  ==============================
    """
    xs = [float(p[0]) for p in caja]
    ys = [float(p[1]) for p in caja]
    ancho = max(xs) - min(xs) + 1.0
    alto = max(ys) - min(ys) + 1.0
    vertical = (alto / max(ancho, 1.0)) >= 1.5
    if not vertical:
        return 180 if cls_label == "180" else 0
    if cls_label == "0":
        return 270
    return 90  # cls "180" (lee hacia arriba) o etiqueta desconocida


def _resultado_vacio(error: str, ancho: int = 0, alto: int = 0, motor: str = "") -> dict:
    """Resultado de error con la misma estructura que el caso de éxito."""
    return {
        "ancho": int(ancho),
        "alto": int(alto),
        "orientacion_pagina": 0,
        "n_lineas": 0,
        "segundos": 0.0,
        "motor": motor or f"rapidocr-{_version_rapidocr()}",
        "error": error,
        "lineas": [],
    }


def _version_rapidocr() -> str:
    try:
        return metadata.version("rapidocr")
    except Exception:
        return "?"


def _construir_motor(rec: str = "latin_mobile", usar_cls: bool = True):
    """Construye (o reutiliza) el motor RapidOCR con los parámetros medidos.

    Det ch PP-OCRv5 server a resolución nativa (sin reducir un A4 a 300 dpi),
    Cls ch PP-OCRv5 server (umbral 0.9) y Rec según ``rec`` (por defecto
    ``latin_mobile``, que tiene Ñ/acentos y no alucina texto sobre el dibujo).

    Devuelve ``(motor, captura)`` donde ``captura`` es un diccionario que el
    envoltorio de ``run_ocr_steps`` rellena en cada pasada con las cajas/textos/
    etiquetas cls ANTES del filtro por confianza (API privada de rapidocr; si
    cambia, se degrada a la salida pública sin información cls).
    """
    clave = (rec, usar_cls)
    if clave in _MOTORES:
        return _MOTORES[clave]

    if rec not in _CONFIG_REC:
        raise ValueError(f"modelo de reconocimiento desconocido: {rec!r}")

    from rapidocr import (  # import perezoso: el módulo se importa sin arrastrar onnxruntime
        EngineType,
        LangCls,
        LangDet,
        LangRec,
        ModelType,
        OCRVersion,
        RapidOCR,
    )

    cfg_rec = _CONFIG_REC[rec]
    params = {
        "Global.text_score": UMBRAL_TEXTO,
        "Global.use_cls": usar_cls,
        "Global.max_side_len": 4000,  # no reducir una página 2480x3508
        "Global.min_side_len": 30,
        "Global.log_level": "warning",
        "Det.engine_type": EngineType.ONNXRUNTIME,
        "Det.lang_type": LangDet.CH,
        "Det.model_type": ModelType.SERVER,
        "Det.ocr_version": OCRVersion.PPOCRV5,
        "Det.limit_side_len": 3600,
        "Det.limit_type": "max",
        "Det.thresh": 0.3,
        "Det.box_thresh": 0.5,
        "Det.unclip_ratio": 1.6,
        "Cls.engine_type": EngineType.ONNXRUNTIME,
        "Cls.lang_type": LangCls.CH,
        "Cls.model_type": ModelType.SERVER,
        "Cls.ocr_version": OCRVersion.PPOCRV5,
        "Cls.cls_thresh": UMBRAL_CLS,
        "Rec.engine_type": EngineType.ONNXRUNTIME,
        "Rec.lang_type": LangRec(cfg_rec["lang"]),
        "Rec.model_type": ModelType(cfg_rec["mtype"]),
        "Rec.ocr_version": OCRVersion.PPOCRV5,
    }
    motor = RapidOCR(params=params)

    captura: dict[str, Any] = {}
    try:
        _pasos_originales = motor.run_ocr_steps

        def _pasos_capturados(img, op_record, *args, **kwargs):
            det_res, cls_res, rec_res, crops = _pasos_originales(img, op_record, *args, **kwargs)
            try:
                captura["cajas"] = (
                    None if det_res.boxes is None else np.array(det_res.boxes, dtype=float).copy()
                )
                captura["textos"] = None if rec_res.txts is None else list(rec_res.txts)
                captura["confianzas"] = (
                    None if rec_res.scores is None else list(map(float, rec_res.scores))
                )
                captura["cls"] = (
                    None if cls_res.cls_res is None else [(str(l), float(s)) for l, s in cls_res.cls_res]
                )
                captura["op_record"] = op_record
            except Exception:
                captura.clear()  # la API privada cambió: degradar a salida pública (sin cls)
            return det_res, cls_res, rec_res, crops

        motor.run_ocr_steps = _pasos_capturados
    except Exception:
        pass  # sin envoltorio: ocr_imagen caerá a la salida pública (sin cls)

    _MOTORES[clave] = (motor, captura)
    return _MOTORES[clave]


def _a_bgr(img: Any) -> np.ndarray:
    """Convierte la entrada (PIL / ndarray BGR / ruta) a un ndarray BGR contiguo."""
    import cv2

    if isinstance(img, np.ndarray):
        arr = img
        if arr.ndim == 2:
            return np.ascontiguousarray(cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR))
        if arr.ndim == 3 and arr.shape[2] == 4:
            return np.ascontiguousarray(cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR))
        if arr.ndim == 3 and arr.shape[2] == 3:
            return np.ascontiguousarray(arr)  # se asume BGR (convención OpenCV)
        raise ValueError(f"array con forma no soportada: {arr.shape}")
    if isinstance(img, (str, Path)):
        leida = cv2.imread(str(img), cv2.IMREAD_COLOR)
        if leida is None:
            raise ValueError(f"no se pudo leer la imagen: {img}")
        return leida
    if hasattr(img, "convert") and hasattr(img, "size"):  # PIL.Image
        rgb = np.asarray(img.convert("RGB"))
        return np.ascontiguousarray(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    raise TypeError(f"tipo de imagen no soportado: {type(img)!r}")


def _pasada_ocr(motor: Any, captura: dict[str, Any], imagen: np.ndarray) -> list[dict]:
    """Una pasada del motor sobre ``imagen`` (BGR).

    Devuelve líneas con caja (píxeles de ``imagen``), texto, confianza del
    reconocedor y, si la captura privada funcionó, etiqueta y confianza del
    clasificador 0/180. Si la API privada cambió, se degrada a la salida
    pública (mismas cajas y textos, sin cls).
    """
    lineas: list[dict] = []
    res = motor(imagen, text_score=UMBRAL_TEXTO)

    cajas = captura.get("cajas")
    textos = captura.get("textos")
    confianzas = captura.get("confianzas")
    cls = captura.get("cls")
    op_record = captura.get("op_record")

    if cajas is not None and textos and confianzas and len(cajas) == len(textos) == len(confianzas):
        alto, ancho = imagen.shape[:2]
        cajas_orig = None
        try:
            from rapidocr.utils.process_img import map_boxes_to_original

            cajas_orig = map_boxes_to_original(np.array(cajas, dtype=float).copy(), op_record, alto, ancho)
        except Exception:
            cajas_orig = None
        if cajas_orig is not None:
            for i, (texto, conf) in enumerate(zip(textos, confianzas)):
                if not texto.strip() or conf < UMBRAL_TEXTO:
                    continue
                etiqueta, conf_cls = (cls[i] if cls and i < len(cls) else (None, None))
                quad = [[int(round(x)), int(round(y))] for x, y in cajas_orig[i].tolist()]
                lineas.append(
                    {
                        "caja": quad,
                        "texto": texto.strip(),
                        "conf": round(float(conf), 4),
                        "cls_label": etiqueta,
                        "cls_conf": None if conf_cls is None else round(float(conf_cls), 4),
                    }
                )
            return lineas

    # Degradación: salida pública (cajas ya mapeadas a la imagen original, sin cls).
    try:
        for caja, texto, conf in zip(res.boxes, res.txts, res.scores):
            if not texto.strip():
                continue
            quad = [[int(round(x)), int(round(y))] for x, y in np.asarray(caja).tolist()]
            lineas.append(
                {
                    "caja": quad,
                    "texto": texto.strip(),
                    "conf": round(float(conf), 4),
                    "cls_label": None,
                    "cls_conf": None,
                }
            )
    except Exception:
        pass
    return lineas


def _descr_motor(motor: Any, rec: str, usar_cls: bool) -> str:
    """Cadena descriptiva del motor, p. ej. rapidocr-3.10.0 det=... rec=... cls=..."""
    version = _version_rapidocr()
    try:
        c = motor.cfg

        def clave(sec, tarea):
            return f"{sec.lang_type.value}_{sec.ocr_version.value}_{tarea}_{sec.model_type.value}"

        det = clave(c.Det, "det")
        rec_k = clave(c.Rec, "rec")
        cls_k = (
            f"{c.Cls.lang_type.value}_{c.Cls.ocr_version.value}_textline_ori_cls_{c.Cls.model_type.value}"
            if usar_cls
            else "off"
        )
        return f"rapidocr-{version} det={det} rec={rec_k} cls={cls_k}"
    except Exception:
        return f"rapidocr-{version} rec={rec} cls={'on' if usar_cls else 'off'}"


def ocr_imagen(
    img: Any,
    rec: str = "latin_mobile",
    usar_cls: bool = True,
    corregir_pagina: bool = True,
) -> dict:
    """OCR local de una imagen raster (plano escaneado o plantilla manuscrita).

    Parámetros
    ----------
    img:
        ``PIL.Image``, ``numpy.ndarray`` en **BGR** (o gris/BGRA) o ruta de imagen.
    rec:
        Modelo de reconocimiento: ``latin_mobile`` (por defecto; Ñ/acentos y no
        alucina sobre zonas de dibujo) o ``ch_server``.
    usar_cls:
        Usar el clasificador 0/180 por línea (recomendado; sin él las cotas
        giradas colapsan).
    corregir_pagina:
        Si entre las líneas con cls fiable (>= 0.9) la mayoría (mínimo 5) está
        etiquetada "180", la página se escaneó boca abajo: se rota 180°, se
        repite el OCR y las cajas se devuelven en el marco de píxeles ORIGINAL.

    Devuelve un dict con ``ancho``, ``alto``, ``orientacion_pagina`` (0|180),
    ``n_lineas``, ``segundos``, ``motor``, ``error`` (None si todo fue bien) y
    ``lineas``; cada línea tiene ``texto``, ``conf``, ``caja`` (cuadrilátero en
    píxeles de la imagen original), ``cx``/``cy`` (centroide), ``angulo``
    (dirección de lectura sobre la página original, ver :func:`angulo_linea`),
    ``cls_label``/``cls_conf``, ``numero`` (int si el texto son solo dígitos,
    tras quitar espacios), ``lectura_180`` y ``ambiguo_180`` (True cuando el
    número es legible girado y el cls no es fiable). Nunca lanza excepciones:
    ante cualquier fallo devuelve ``error`` con la causa.
    """
    t0 = time.perf_counter()
    try:
        imagen = _a_bgr(img)
    except Exception as exc:
        return _resultado_vacio(f"entrada no válida: {exc}")

    alto, ancho = imagen.shape[:2]
    try:
        motor, captura = _construir_motor(rec, usar_cls)
    except Exception as exc:
        return _resultado_vacio(f"motor OCR no disponible: {exc}", ancho, alto)

    try:
        lineas = _pasada_ocr(motor, captura, imagen)
        orientacion = 0

        if corregir_pagina and usar_cls and lineas:
            fiables = [l for l in lineas if l["cls_conf"] is not None and l["cls_conf"] >= UMBRAL_CLS]
            n_180 = sum(1 for l in fiables if l["cls_label"] == "180")
            if n_180 >= MIN_LINEAS_VOTACION and n_180 > len(fiables) - n_180:
                girada = np.ascontiguousarray(imagen[::-1, ::-1])
                segundas = _pasada_ocr(motor, captura, girada)
                if segundas:
                    for l in segundas:  # cajas al marco de la página original
                        l["caja"] = [[ancho - 1 - x, alto - 1 - y] for x, y in l["caja"]]
                    lineas = segundas
                    orientacion = 180

        lineas_finales = []
        for l in lineas:
            xs = [p[0] for p in l["caja"]]
            ys = [p[1] for p in l["caja"]]
            cx = int(round(sum(xs) / len(xs)))
            cy = int(round(sum(ys) / len(ys)))
            angulo = angulo_linea(l["caja"], l["cls_label"])
            if orientacion == 180:
                angulo = (angulo + 180) % 360
            digitos = l["texto"].replace(" ", "")
            numero = int(digitos) if digitos.isdigit() else None
            lectura = lectura_girada_180(digitos) if numero is not None else None
            ambiguo = lectura is not None and (l["cls_conf"] is None or l["cls_conf"] < UMBRAL_CLS)
            lineas_finales.append(
                {
                    "texto": l["texto"],
                    "conf": l["conf"],
                    "caja": l["caja"],
                    "cx": cx,
                    "cy": cy,
                    "angulo": angulo,
                    "cls_label": l["cls_label"],
                    "cls_conf": l["cls_conf"],
                    "numero": numero,
                    "lectura_180": lectura,
                    "ambiguo_180": ambiguo,
                }
            )

        return {
            "ancho": int(ancho),
            "alto": int(alto),
            "orientacion_pagina": orientacion,
            "n_lineas": len(lineas_finales),
            "segundos": round(time.perf_counter() - t0, 2),
            "motor": _descr_motor(motor, rec, usar_cls),
            "error": None,
            "lineas": lineas_finales,
        }
    except Exception as exc:
        return _resultado_vacio(f"fallo de OCR: {exc}", ancho, alto)


def hint_ocr(resultado: dict, max_lineas: int = 150) -> str:
    """Bloque compacto en español con lo leído, pensado para pastar a un LLM.

    Primera línea: ``OCR local (PP-OCRv5) — página escaneada girada 180° (lecturas
    ya corregidas)`` o ``... con orientación normal``. Luego ``COTAS`` (líneas
    numéricas ordenadas por (cy, cx)) con formato
    ``1165 @ (1222, 1180), giro 180°, conf 0.98``, añadiendo
    ``— podría ser 009 si está girada`` a las cotas ambiguas; y ``TEXTOS`` con las
    líneas no numéricas de conf >= 0.3 (se descartan las de menos confianza y las
    de 1 carácter). Máximo ``max_lineas`` líneas en total (cotas primero).
    Si ``resultado`` trae ``error``, devuelve ``[OCR local error: ...]``.
    """
    try:
        error = resultado.get("error")
        if error:
            return f"[OCR local error: {error}]"

        cabecera = (
            "OCR local (PP-OCRv5) — página escaneada girada 180° (lecturas ya corregidas)"
            if resultado.get("orientacion_pagina") == 180
            else "OCR local (PP-OCRv5) — página escaneada con orientación normal"
        )

        lineas = [l for l in (resultado.get("lineas") or []) if isinstance(l, dict)]
        cotas = [l for l in lineas if l.get("numero") is not None]
        textos = [
            l
            for l in lineas
            if l.get("numero") is None
            and float(l.get("conf", 0.0)) >= UMBRAL_TEXTO
            and len(str(l.get("texto", "")).strip()) >= 2
        ]
        cotas.sort(key=lambda l: (int(l.get("cy", 0)), int(l.get("cx", 0))))
        textos.sort(key=lambda l: (int(l.get("cy", 0)), int(l.get("cx", 0))))

        cotas = cotas[:max_lineas]
        textos = textos[: max(0, max_lineas - len(cotas))]

        partes = [cabecera]
        if cotas:
            partes.append("COTAS (número @ (x,y) px, giro, conf):")
            for l in cotas:
                trozo = (
                    f"{str(l.get('texto', '')).strip()} @ ({int(l.get('cx', 0))}, {int(l.get('cy', 0))}), "
                    f"giro {int(l.get('angulo', 0))}°, conf {float(l.get('conf', 0.0)):.2f}"
                )
                if l.get("ambiguo_180") and l.get("lectura_180") and l["lectura_180"] != str(
                    l.get("texto", "")
                ).strip():
                    trozo += f" — podría ser {l['lectura_180']} si está girada"
                partes.append(trozo)
        if textos:
            partes.append("TEXTOS:")
            for l in textos:
                partes.append(
                    f"{str(l.get('texto', '')).strip()} @ ({int(l.get('cx', 0))}, {int(l.get('cy', 0))}), "
                    f"giro {int(l.get('angulo', 0))}°, conf {float(l.get('conf', 0.0)):.2f}"
                )
        omitidas = len(lineas) - len(cotas) - len(textos)
        if omitidas > 0:
            partes.append(f"... ({omitidas} líneas omitidas: baja confianza, 1 carácter o límite)")
        return "\n".join(partes)
    except Exception as exc:
        return f"[OCR local error: {exc}]"


def ocr_hint_imagen(img: Any) -> str:
    """OCR + hint en una llamada (``ocr_imagen`` + ``hint_ocr``); nunca lanza.

    Ante cualquier fallo devuelve ``[OCR local error: ...]``.
    """
    try:
        return hint_ocr(ocr_imagen(img))
    except Exception as exc:
        return f"[OCR local error: {exc}]"


def _main(argv: list[str] | None = None) -> int:
    """CLI: ``python plano_raster.py IMG... [--json DIR] [--rec ...] [--sin-cls] [--sin-corregir]``."""
    parser = argparse.ArgumentParser(
        description="OCR local (RapidOCR, PP-OCRv5) de planos escaneados y plantillas manuscritas."
    )
    parser.add_argument("imagenes", nargs="+", help="rutas de imágenes (PNG/JPG)")
    parser.add_argument("--json", default=None, metavar="DIR", help="escribir DIR/<stem>.json con el resultado completo")
    parser.add_argument("--rec", default="latin_mobile", choices=["latin_mobile", "ch_server"], help="modelo de reconocimiento")
    parser.add_argument("--sin-cls", action="store_true", help="desactivar el clasificador 0/180 por línea")
    parser.add_argument("--sin-corregir", action="store_true", help="no corregir el giro de página 180°")
    args = parser.parse_args(argv)

    salida_json = Path(args.json) if args.json else None
    if salida_json is not None:
        salida_json.mkdir(parents=True, exist_ok=True)

    codigo = 0
    for ruta in args.imagenes:
        resultado = ocr_imagen(
            ruta,
            rec=args.rec,
            usar_cls=not args.sin_cls,
            corregir_pagina=not args.sin_corregir,
        )
        print(
            f"=== {ruta} ({resultado['ancho']}x{resultado['alto']} px, "
            f"orientación {resultado['orientacion_pagina']}°, {resultado['n_lineas']} líneas, "
            f"{resultado['segundos']:.1f} s, motor {resultado['motor']})"
        )
        print(hint_ocr(resultado))
        if resultado.get("error"):
            codigo = 1
        if salida_json is not None:
            destino = salida_json / f"{Path(ruta).stem}.json"
            destino.write_text(json.dumps(resultado, ensure_ascii=False, indent=1), encoding="utf-8")
        print()
    return codigo


if __name__ == "__main__":
    sys.exit(_main())
