# Plan de autonomía del extractor — revisión del 14/10/2026

Objetivo: que, solo con los documentos de la carpeta de trabajo (plantilla manuscrita,
plano 2D, renders, notas), el sistema defina todas las piezas, huecos, cantos y
materiales sin intervención del operador, con calidad suficiente para presupuestar.

Mapa del código actual: `ARQUITECTURA.md`. Herramientas de medida: `bench_autonomo.py`,
`comparar_extracciones.py`, `verificar_contra_excel.py`.

## 1. Diagnóstico

**Arquitectura.** Una sola llamada a Claude (prompt de sistema de 78 KB + documentos como
imagen/texto) devuelve un JSON que luego pasa por ~3.700 líneas de postprocesado. La parte
más potente del postprocesado (reconstrucción del polígono desde la polilínea, clasificación
pared/vista en píxel, creación de copetes/zócalos/frontales desde trazos, huecos desde marcas
rojas, muesca de pilar, des-inclinación de escaneos) solo se activa si existe
`anotaciones.json` del operador. En modo autónomo la geometría final depende de
`vertices_mm` + `aristas_contacto` + `aristas_cota` emitidos por Claude y corregidos por cotas,
y una pieza omitida por Claude no tiene vía de recuperación (sección 4 de `ARQUITECTURA.md`).

**Medición disponible.**

| Nivel de verdad | Cobertura | Qué mide | Estado |
|---|---|---|---|
| Extracciones validadas por el operador (`*_extraccion_validada.json`) | 4 proyectos con crudo (J0028/J0030/J0033/J0034); 29 anotados en total | Piezas, dimensiones, huecos, cantos, materiales | Banco creado hoy; media 0,80 sobre los 4 crudos (comparador v2, tiras por longitud) |
| Excel MGR (presupuesto real) | 775 de 796 carpetas | Material, nº huecos, ml de cantos, m² chapeado, ml copete/rodapié; NO la geometría de la encimera | Verificador existente; ~5 % de ruido humano |

**Línea base contra Excel (hoy, sin API).** Cocimoble2025: 243/326 conceptos (74,5 %, 47
proyectos); Cocimoble2026: 94,7 % (20); ACyC: 90,4 % (18). Ojo: los 57 proyectos "viejos"
dan 90 % porque entonces el MGR entraba en el input del extractor (fuga de la verdad); los 28
anotados con el pipeline actual dan un 65,9 % honesto. Errores dominantes: ml de pulido
(31 fallos de 85, mediana json/excel 0,72: se infra-estima), enchufes (17/60, 13 de ellos por
exceso: el default "1 cada 1,5 ml" sobre-emite), ingletes (13/38, 10 por exceso: fantasmas),
fregadero/grifo/placa (≈10 % cada uno), chapeado m² (5/53), material (4/85).

**El Excel mide conceptos de presupuesto, no el JSON.** Las 4 extracciones VALIDADAS por el
operador también fallan contra su Excel en cantos: J0034 (porcelánico 12 mm) factura 10,8 ml de
inglete y 9,29 ml de pulido donde el JSON validado tiene 0 y 4,47 (canto 12+12 ingletado que el
modelo de datos no representa como canto); J0030 factura 8,59 ml de pulido y 2,34 m² de chapeado
frente a 4,52 y 1,79. Conclusión: entre el JSON de piezas y el presupuesto falta la capa
**presupuestador** (JSON → líneas MGR: m² por tipo, ml de canto según material/grosor, huecos,
enchufes). Hasta tenerla, el verificador Excel solo sirve para material y nº de huecos.

**Contaminación por salidas propias (corregida el 14/10, commit en file_readers).** Los `.txt` de
extracciones anteriores (`*_extraccion_cruda.txt`) entraban como fuente de texto: el modelo copiaba la
extracción previa en vez de leer los documentos. Se detectó al medir Qwen (avisos idénticos a los de
Fable de julio). Afecta también al crudo de J0034 de julio (existía un `_cruda.txt` previo). Regla:
toda medida del banco debe hacerse con fuentes limpias; `should_ignore` ya excluye `_extraccion` y
`divergencia` por subcadena, con test.

**Crudo frente a validado (4 proyectos, 51 discrepancias, clasificadas con Jev).**
hueco_estimado 21 (en su mayoría ruido de marco de coordenadas distinto + posiciones estimadas,
irrelevantes para presupuestar), omisión de tiras sin cota 13 (zócalo bajo columnas, zócalo de
cabeza, copete de cabecera, frontales cortos), contorno 5 (J0030: barra fusionada, bbox
2570×2120 frente a 2115×2150), clasificación de aristas 4, inglete fantasma 3 (los crudos son
anteriores al fix e8b0682: hay que re-medir), tipo de pieza (costado emitido como encimera, J0034).

**Documentos.** En una muestra de 120 carpetas, 78 de 146 PDF no-MGR son vectoriales con texto
(53 %), 64 escaneados. De los planos clasificados, 9 de 20 planos CAD son PDF vectorial y 10 son
PNG; los 22 "plano-anotado" son escaneos. En los PDF vectoriales (J0020, J0024) las cotas van
como texto con posición, la escala es uniforme (9,45 y 9,03 mm/pt, derivable del par cota↔línea),
las cotas giradas (006→600, 0511→1150, 039→930) se detectan por `upright=False` y la matriz de
rotación del carácter, y las etiquetas ENCIMERA / FRONTAL H.600 / COPETE llevan posición.

**Entorno.** Python del sistema pasó a 3.14 y no quedaba ninguna dependencia: creado `.venv`
(anthropic 1.14, pdfplumber, openpyxl, ezdxf, EasyOCR con torch CPU). Qwen sin cuota hasta el
30/10. La API key está escrita en texto plano en `memoria/project_extractor_piezas.md`.

## 2. Palancas, por impacto y coste

1. **Banco de medida único** (hecho: `bench_autonomo.py` + comparador; falta ampliar).
   Sin métrica repetible no hay iteración autónoma posible.
2. **Capa vectorial determinista** (`plano_vectorial.py`, pdfplumber + shapely + utilidades
   geométricas de `~/Documents/ProgramaDeAcotacionesDXF/dxf_autodim/core.py`): escala,
   cotas des-rotadas con posición, etiquetas, segmentos → **anotaciones sintéticas en el mismo
   formato que `anotaciones.json`** (contorno de encimera, paredes, etiquetas de copete/frontal).
   Así TODO el postprocesado existente se reactiva sin operador. Cubre ~50 % de los trabajos
   con precisión de CAD y elimina OCR y cotas giradas en esos casos.
3. **Anotador automático para raster**: primera pasada de Claude (Fable, alta resolución) que
   emite las mismas anotaciones sintéticas en píxel (polilínea, paredes, marcas de hueco),
   validadas por cierre de cotas (Σ+ = Σ−) antes de entrar al postproc. Sustituye al operador
   con el mismo contrato de datos.
4. **Cascada de verificación por campo** (patrón TypeSafe `sde_cascade`): extracción con modelo
   más barato (Opus 5.5), Jev pregunta por campo "¿está respaldado por el texto del plano /
   plantilla?" y "¿falta algo que el documento sí da?", y solo los campos dudosos (P>0,7)
   escalan a Fable. Más una pasada de checklist de taller (cada pared → copete o chapeado;
   zócalo bajo todos los bajos y columnas; costados ≠ encimera; huecos sin posición → VERIFICAR).
5. **Prompt y API**: prompt modular (bloques de operador fuera en modo autónomo), salidas
   estructuradas (`output_config.format`, adiós al regex y a la segunda llamada), caché del
   prompt de sistema (≈20k tokens por llamada), `fallbacks` para refusals de Fable, Batch API
   (−50 %) para los lotes del banco. Aplicar la auditoría de prompt (`scratchpad/prompt_audit`).
6. **Reglas con evidencia a escala**: enchufes por defecto, pulido por exclusión, inglete
   implícito: ajustarlas contra el Excel de 30-50 proyectos, no contra 1 caso.
7. **Higiene** (observaciones 1-10 de `ARQUITECTURA.md`): modelo por defecto contradictorio,
   reintento sin clasificación, contratos por subcadenas de `notas`, código muerto.

## 2b. Economía de API: API mínima, local primero (petición de Fran, 14/10/2026)

Coste actual por proyecto con Fable 5: 60-100k tokens de entrada (imágenes) + 10-30k de salida
≈ 1-2,5 $. Objetivo: < 0,10 $ de media, con Claude solo como árbitro de casos dudosos.
Orden de preferencia para cada dato, del más barato al más caro:

| Nivel | Vía | Coste/proyecto | Cubre |
|---|---|---|---|
| 0 | **PDF vectorial nativo** (pdfplumber: líneas, rects, chars con matriz) | 0 | ~50 % de planos: contorno, cotas des-rotadas, escala, etiquetas |
| 0 | **OpenCV sobre planos raster limpios** (PNG/PDF exportados del programa de diseño: líneas negras nítidas, X de muebles altos, cotas impresas) + EasyOCR con prueba de rotación | 0 | Buena parte del resto de planos CAD; segmentos, rectángulos, texto |
| 0 | **Modelos de visión locales en la GPU** (GTX 1080 Ti de 11 GB comprobada el 14/10; el `.venv` lleva torch CPU para EasyOCR, para entrenar o servir un VLM instalar torch CUDA aparte; ranura `gpu` en el toolkit): OCR de documentos (PaddleOCR/docTR), OCR manuscrito (TrOCR) para la plantilla, VLM abierto (Qwen2.5-VL 7B, Florence-2) para preguntas de contenido (¿fregadero bajo ventana? ¿cuántos muebles altos?) — el papel que tenía Qwen en la nube, gratis | 0 (GPU propia) | Contenido, texto, presencia de elementos; no mediciones finas |
| 0 | **Entrenar un detector/segmentador propio** (Ultralytics YOLO-seg o similar) con los planos ya anotados: `anotaciones.json` guarda polilíneas y trazos EN PÍXEL sobre la imagen del plano = máscaras de entrenamiento gratis (29 hoy; el anotador web sigue siendo la herramienta de etiquetado: cada proyecto que el operador anote es un ejemplo más). Dominio muy homogéneo (mismo programa de diseño) → con 100-200 planos es realista detectar contorno de encimera, paredes, muebles altos y marcas de hueco. Su salida entra al reconciliador existente como "anotaciones sintéticas" | 0 tras entrenar | Contorno + paredes + huecos en píxel para planos raster |
| 1 | **Jev** (TypeSafe) para verificar campos contra el texto extraído (cascada) y clasificar | céntimos | Verificación, routing, consistencia |
| 2 | **Haiku 5.5** (0,10 $/M entrada) para lectura de la plantilla manuscrita y clasificación de documentos (hoy el clasificador usa Haiku 4.5, 10× más caro) | ~0,01-0,03 $ | Campos de plantilla, categorías |
| 3 | **Opus 5.5 / Fable** solo para: planos que fallen el cierre de cotas, discrepancias entre fuentes, campos con P(error) > 0,7 según Jev | 0,5-2 $ solo en el 10-20 % de casos | Árbitro |

Ahorros inmediatos en las llamadas que queden: caché del prompt de sistema (~20k tokens → cobra
al 10 %), salidas estructuradas (sin segunda llamada "dame solo el JSON"), Batch API (−50 %) para
lotes del banco, no enviar renders 3D salvo que falte información, no enviar cada página anotada
por duplicado (original + overlay), DPI por tipo de documento, y modelo por tarea.

Bucle de aprendizaje propuesto: cada proyecto que el operador valide o anote alimenta (a) el banco
de medida, (b) el conjunto de entrenamiento del detector local y (c) el corpus few-shot cacheado.
Así el coste por proyecto baja con el uso en lugar de subir.

## 2c. Auditoría del prompt (skill claude-api `prompt-audit`, 14/10/2026)

Informe y diff propuesto (NO aplicado) en `auditoria_prompt_2026-10-14/`. Prompt actual ≈ 24.000
tokens. Hallazgos de alta confianza: (A1) sustituir el andamiaje "PROHIBIDO texto antes del JSON" +
regex + segunda llamada por salidas estructuradas (`output_config.format`); (A2) el clasificador
rompe con Haiku 5.5 (`content[0].text`, `max_tokens=400`); (A3) ~45 líneas de reglas del MGR que el
propio prompt manda ignorar; (A4) contradicción polígono único vs multi-tramo; (A5) pide pilastras que
el postproc descarta; (A6) dos tablas de tamaños de hueco con valores distintos y márgenes 40 mm vs
regla de 80 mm; (A7) sin caché del prompt. Ahorro: 24.000 → ~18.000 tokens en autónomo (−25 %) y,
con caché, ~0,19 $ menos por llamada. Aplicar en rama y medir con el banco, un cambio cada vez
(A4, A6, división del prompt y ejemplo son cambios de comportamiento).

## 2d. ¿Qwen en lugar de Claude? (pregunta de Fran, 14/10/2026)

Qwen (plan de tokens, modelos qwen3.8-max / qwen3.7-max / qwen3.7-plus con visión, endpoint
compatible con la API de Anthropic) tiene coste marginal ~0 dentro de la cuota mensual, así que
es el candidato natural a **extractor de primer escalón** en la cascada (2b): Qwen extrae, Jev
verifica por campo, y solo lo dudoso escala a Fable. Lo que sabemos (registro del toolkit,
pruebas del 30/09): visión fiable para CONTENIDO (textos literales, presencia de elementos,
estado); coordenadas en píxel NO fiables. Para este extractor eso significa:

- Probablemente suficiente: campos de la plantilla manuscrita, clasificación de documentos
  (sustituye a Haiku), lectura de cotas impresas en planos CAD limpios, materiales, huecos.
- Dudoso: geometría del contorno a partir de la imagen (vértices en mm por cierre de cotas en
  planos girados/escaneados) y cualquier salida en píxel (anotaciones sintéticas).
- Irrelevante donde ya no hace falta visión: planos PDF vectoriales (capa 0 del plan).

Decisión por datos, no por intuición: el extractor usa el SDK de Anthropic y lee
`ANTHROPIC_BASE_URL`, así que se puede medir SIN cambiar código:

```
set -a; . ~/.config/fable/qwen.env; set +a      # exporta la clave del plan de Qwen
ANTHROPIC_BASE_URL=<endpoint Anthropic-compatible de Qwen> ANTHROPIC_API_KEY=$QWEN_API_KEY \
  .venv/bin/python bench_autonomo.py --model qwen3.8-max --salida bench_out/qwen \
  ../datos/Cocimoble2025/J0028_* ../datos/Cocimoble2025/J0030_* ../datos/Cocimoble2025/J0033_* ../datos/Cocimoble2025/J0034_*
```

y comparar el scoreboard con el de Fable sobre los mismos 4 proyectos.

**Resultado (15/10/2026, fuentes limpias, qwen3.8-max, razonamiento activado):**

| Proyecto | Fable jul | Qwen prompt viejo | Qwen prompt A4 | Excel: Fable / Qwen A4 |
|---|---|---|---|---|
| J0028 | 0,77 | 0,68 | 0,64 | 7/8 / 6/8 |
| J0030 | 0,83 | 0,48 | 0,72 | 4/8 / 5/8 |
| J0033 | 0,82 | 0,68 | 0,81 | 6/8 / 7/8 |
| J0034 | 0,79 | 0,79 | 0,73 | 5/7 / 5/7 |
| Media | 0,80 | 0,66 | 0,73 | |

"Prompt A4" = sección multi-tramo (capa de abril, era MGR) sustituida por "Forma real de la encimera
y fondos reducidos" (polígono único), commit del 15/10. Con ello Qwen emite la encimera como un solo
polígono en J0028 (8 vértices), J0033 (L, 6) y J0034 (L, 6); J0030 (G con barra) sigue mal
(2117×1700 frente a 2115×2150). Corridas únicas: hay varianza entre corridas (J0028/J0034 bajan
0,04-0,05 sin cambio atribuible), así que las comparaciones finas necesitan 2-3 repeticiones.

Huecos: 1,0 en los cuatro con ambos modelos. A nivel de conceptos facturables (Excel) Qwen iguala o
supera a los crudos de Fable; a nivel de piezas pierde sobre todo por GEOMETRÍA: parte la encimera en
rectángulos por tramo (J0033: 2600×600 + 1550×350 en vez de la L 1550×2600; J0030: tres rectángulos
en vez de la G), que es la contradicción "multi-tramo vs polígono único" (hallazgo A4 de la auditoría:
Qwen obedece la regla antigua, Fable la nueva). También omite el `acabado` del material y, en J0030,
los frontales. Conclusión: Qwen vale como extractor de primer escalón a coste marginal cero (huecos 1,0; a nivel
de Excel iguala a Fable; geometría correcta en 3 de 4 tras A4). Sus huecos restantes coinciden con
los de Fable (copetes por exclusión, zócalos/frontales sin cota, J0030) más dos propios: omite el
`acabado` del material y en J0028 parte los frontales con la regla de "zona sin muebles altos →
H1000". Siguientes pasos del bucle (todos a coste cero con Qwen): `acabado` desde plantilla, aplicar
el resto de la auditoría (structured outputs, MGR muerto, huecos duplicados) un cambio por corrida,
y la capa vectorial para la forma. Riesgos a vigilar en
esa prueba: `max_tokens=64000` y streaming (qwen-chat usa llamadas sin streaming; probar), el
razonamiento de Qwen va activado por defecto y cuesta tokens (qwen-chat lo apaga con
`thinking: {type: disabled}`; el extractor no lo envía, añadir un interruptor), bloques de
`thinking` en la respuesta (el extractor ya los ignora), tamaño del prompt (24k tokens) y de las imágenes, y la
cuota mensual (cada proyecto son 60-100k tokens de entrada; la cuota se agotó el 14/10 y vuelve
el 30/10/2026). Si Qwen iguala a Fable en materiales/huecos/tiras y solo pierde en contorno, el
reparto queda: Qwen + capa vectorial/OpenCV para la forma, Fable solo como árbitro.

## 3. Secuencia propuesta

| Fase | Entregable | Coste estimado |
|---|---|---|
| F0 (hoy) | Entorno, mapa de arquitectura, banco + comparador, línea base, inventario | hecho |
| F1 Medir | Re-medir los 4 crudos con el código actual; validar cuáles de las 29 extracciones anotadas son verdad; banco sobre 29; escribir `presupuestador.py` (JSON → conceptos MGR, con las reglas de canto 12+12, pulido por material, enchufes) y medir contra el Excel a ese nivel | 29 extracciones × ~1-2 $ (Fable, 60-100k tokens de imagen); mitad con Batch + caché |
| F2 Local primero | `plano_vectorial.py` (PDF nativo) + `plano_raster.py` (OpenCV + EasyOCR) → anotaciones sintéticas; validar en J0020/J0024/I018 (vectoriales) y en planos PNG; re-medir. En paralelo, experimento de VLM local y de detector entrenado con las 29 anotaciones | GLM para el código con fixtures sintéticos; GPU local; 0 $ de API |
| F3 Raster difícil | Solo lo que F2 no resuelva (escaneos torcidos, fotos): pasada "anotador automático" con Claude + cierre de cotas; re-medir en los 22 escaneados | API, acotada al residuo |
| F4 Verificación | Cascada Opus 5.5 → Jev por campo → Fable solo en dudas; checklist de taller | Jev ≈0,35 s y céntimos por campo |
| F5 Prompt/API | Modularizar, structured outputs, caché, fallbacks, auditoría | API para A/B |
| F6 Escala | Banco contra Excel en 50-100 proyectos; ajuste de reglas por evidencia | API, Batch |

Criterio de "listo para presupuestar": ≥95 % de conceptos de Excel acertados en 50 proyectos
nuevos y 0 piezas inventadas; lo que no llegue, con aviso VERIFICAR explícito.

## 4. Reutilización (regla: buscar antes de escribir)

- **Ya en el proyecto**: pdfplumber (líneas, rects, chars con matriz), shapely (polígonos),
  rectpack (nesting), ezdxf, EasyOCR, `verificar_contra_excel.py`, `dxf_autodim/core.py`
  del proyecto hermano (área, rectángulo mínimo, esquinas, descuadre, arcos).
- **Skills cargadas y aplicables**: `claude-api` (structured outputs, prompt caching, Batch,
  `prompt-audit`, `build-eval`/`hillclimb` para iterar contra el banco), `typesafe-ai`
  (cookbooks `sde_cascade` y `citation_check` para la verificación por campo), `pdf`
  (pypdf/pdfplumber/pytesseract: nada nuevo frente a lo instalado), `xlsx` (openpyxl, ya en uso).
- **Evaluado y descartado**: `hamzaabduljabbar/construction-drawing-analyzer` (MIT, archivado;
  polígonos = cajas envolventes y escala por regex "1:N", inferior a lo ya probado aquí);
  `TerminalSkills/architectural-drawing-parser` (solo prompt, raster, normativa IBC);
  `earthtojake/text-to-cad` dxf (genera DXF, no lee geometría); `pyPDFtoDXF` (pdf2svg +
  Inkscape; queda como fallback si pdfplumber no bastara). No existe librería pública que
  parsee planos de cocina vectoriales.

## 5. Decisiones que necesita Fran

1. La API key que había en la memoria ya NO es válida (401 el 14/10). Está en `~/.config/fable/anthropic.env` (600): sustituir el valor por una clave nueva de console.anthropic.com y cargarla con `set -a; . ~/.config/fable/anthropic.env; set +a` antes de correr el banco.
2. Presupuesto para F1 (orden de 30-60 $) y confirmar qué extracciones cuentan como verdad.
3. Commit de lo creado hoy (`ARQUITECTURA.md`, banco, comparador, tests, `.gitignore`,
   este plan) o descartarlo.
