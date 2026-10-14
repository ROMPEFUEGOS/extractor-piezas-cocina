# Auditoría de prompts: ExtractorPiezas (pipeline de visión)

Fecha: 2026-10-14. Flujo `prompt-audit` de la skill claude-api, modo no interactivo. No se ha modificado nada bajo `/home/kecojones/PresupuestosEncimeras`; el diff es solo una propuesta (`propuesta.diff`, 39 hunks, comprobado con `patch -p1 --dry-run` sobre una copia limpia; los tres ficheros compilan y el ejemplo JSON nuevo valida contra el esquema nuevo).

## Paso 0: supuestos

- Alcance (fijo, según la petición): `claude_extractor.py` solo `SYSTEM_PROMPT` (l.15-976) y los parámetros de API de `_mensaje_streaming` (l.1005-1022) y `extract_trabajo` (l.4452-4632); `file_readers.py` los bloques de contexto de `build_claude_content` (l.397-612); `clasificador.py` completo (`PROMPT` y modelo). Fuera de alcance, listados al final como `flag`: `main.py`, `bench_autonomo.py`, `ARQUITECTURA.md`, el resto de `file_readers.py` (OCR) y la lógica de posproceso.
- Modelo objetivo: `claude-fable-5` hoy, migrando a `claude-fable-5-1` (decisión del proyecto; no se propone Opus). Modelo secundario del clasificador: `claude-haiku-4-5-20251001` hacia `claude-haiku-5-5` (ID fijo, sin sufijo de fecha). Fable 5.1 conserva el tokenizador de Fable 5, así que los recuentos de tokens no cambian al migrar.
- Sin clave de API: no se ha llamado a ningún modelo. Todo lo conductual (esquema aceptado, efecto de quitar reglas) queda como hipótesis a probar con `bench_autonomo.py` antes de aplicar (Paso 7 de la guía).

## Inventario de la superficie de prompt

| Elemento | Ubicación |
|---|---|
| System prompt monolítico (español, 76.815 caracteres, 27 secciones `##`) | `claude_extractor.py:15-976` |
| Llamada de extracción (streaming, `max_tokens=64000`, `EXTRACTOR_EFFORT` a `output_config.effort`) | `claude_extractor.py:1005-1022`, `4527-4538` |
| Segunda llamada "convierte tu análisis a JSON" | `claude_extractor.py:4574-4596` |
| Tercera llamada (reintento con 3 PDFs) | `claude_extractor.py:4614-4620` |
| Extracción de JSON por regex/llaves/reparación | `claude_extractor.py:1031-1090` |
| Bloques de contexto (clasificación, anotaciones, pilares, paredes, polilíneas) | `file_readers.py:426-612` |
| Prompt y llamada del clasificador (una llamada por página) | `clasificador.py:41-91`, `94-140` |
| Sin herramientas, sin few-shot salvo el JSON de ejemplo (l.870-975), sin prefill, sin `thinking`/`temperature`/`budget_tokens`, sin cabeceras beta | verificado por grep |

## Procedencia (git)

El prompt tiene 6 meses (commit inicial 2026-04-17) y capas claramente fechadas: MGR visible (04-17 a 04-23), "MGR filtrado del input" (04-28, `c4195a1`), polígonos y anotaciones (04-28/04-30, `53682cf`), pilar como obstáculo (05-10, `4b94e00`), regla de los 80 mm (06-11, `5031612`) y reglas J0028/J0030/J0034. La mayoría de contradicciones de abajo son una capa nueva que no retiró la antigua.

## Resumen

Tokens estimados de `SYSTEM_PROMPT`: 76.815 caracteres, unos 24.000 tokens (1 token por 3,2 caracteres; estimación sin tokenizador).

Los tres cambios de más valor:

1. Retirar el andamiaje de JSON (l.863-868, segunda llamada l.4574-4596, comentarios `/* */` dentro del ejemplo) y usar `output_config.format` con esquema. En el clasificador, además, Haiku 5.5 rompe `msg.content[0].text` y puede agotar `max_tokens=400` pensando.
2. Eliminar del prompt las reglas heredadas del MGR (unas 45 líneas con "MGR" aunque el propio prompt dice en l.338-342 que no se recibe) y resolver las contradicciones entre capas: polígono único frente a multi-tramo, tamaños y márgenes de huecos, pilar/pilastra frente a lo que el posproceso descarta (l.4651-4665).
3. Cachear el system prompt (bloque base idéntico en todos los trabajos) y dividirlo en base más bloque de anotaciones condicional: el modo autónomo deja de pagar unos 2.200 tokens de reglas de overlay que no puede usar.

Ahorro estimado de tokens de prompt: de 24.000 a unos 18.000 en modo autónomo (-25%) y a unos 20.200 con anotaciones (-16%). De esos, unos 2.200 son el bloque de anotaciones, que pasa a ser condicional, y unos 3.800 son texto eliminado (MGR, multi-tramo, formato, pilastra). Con caché (lectura a 0,25 USD/MTok en Fable 5.1 frente a 10 USD/MTok de entrada), un prompt de ~20.000 tokens cuesta ~0,005 USD por lectura frente a ~0,20 USD sin caché: unos 0,19 USD por llamada en lotes. Quitar la segunda llamada evita además reenviar todas las imágenes y el prompt completo cuando falla el JSON.

Recuento por grupo: Grupo 1 (texto fechado): 5 hallazgos; Grupo 2 (hechos obsoletos y conflictos): 6; Grupo 3 (herramientas): no aplicable (no hay `tools`); Grupo 4 (configuración de petición): 7.

## Hallazgos (ordenados por confianza)

### Alta confianza

**A1. Andamiaje de JSON sustituible por salida estructurada** (Grupo 1b, `replace-with-API-feature`)
- Ubicación: `claude_extractor.py:863-868`, `4574-4596`, `1031-1090`; `clasificador.py:53-57`, `80-86`.
- Evidencia: `**PROHIBIDO escribir cualquier texto antes del JSON.**` / `**Si no puedes hacerlo en una sola respuesta, escribe el JSON igualmente — es mejor JSON incompleto que análisis sin JSON.**`; reintento "Tu respuesta anterior contenía el análisis correcto pero no en formato JSON"; extractor por regex/llaves; en el clasificador "Devuelve SOLO JSON válido (sin texto extra)" más limpieza de ``` y `json.loads`.
- Por qué es obsoleto: es el patrón "output ONLY valid JSON + regex + retry-on-parse" que la guía sustituye por `output_config.format`. Fable 5.1 y Haiku 5.5 admiten salida estructurada. Además la segunda llamada reenvía todas las imágenes, y si la primera respuesta no tuvo texto (`response_text == ""`) envía un turno de asistente vacío que la API rechaza con 400 (cae en el `except` y se pierde el trabajo).
- Confianza: alta. Acción: `replace-with-API-feature`. En el diff: `RESPUESTA_SCHEMA` (derivado de `json_to_trabajo`), `output_config.format` en `_mensaje_streaming`, sección "FORMATO DE RESPUESTA" sin prohibiciones, segunda llamada eliminada y sustituida por manejo de `refusal`. Se conserva `extract_json_from_response` a propósito: su reparación de JSON truncado sigue siendo útil si `stop_reason == "max_tokens"`. Esquema del clasificador con `enum` sobre `CATEGORIAS`.
- Riesgos a probar: el esquema no se ha enviado a la API (restricciones de esquema, versión mínima del SDK con `output_config.format`); en organizaciones con CMEK la salida estructurada no está disponible en modelos Fable.

**A2. Clasificador incompatible con Haiku 5.5** (Grupo 4, `rewrite`)
- Ubicación: `clasificador.py:71` (`max_tokens=400`), `:80` (`msg.content[0].text`), `:94` y `:152` (`claude-haiku-4-5-20251001`).
- Por qué es obsoleto: Haiku 5.5 piensa por defecto; el pensamiento cuenta para `max_tokens` (una cota de 400 puede agotarse antes del texto) y la respuesta puede empezar por un bloque `thinking`, de modo que `content[0].text` falla. Documentado en la sección "Migrating to Claude Haiku 5.5".
- Acción (en el diff): `claude-haiku-5-5`, `effort: low`, `max_tokens=2048`, lectura de bloques por tipo, constante `MODELO_CLASIFICADOR` para los dos puntos de fijación del ID. Confianza: alta.

**A3. Reglas del MGR heredadas, contradictorias con la propia fuente de verdad** (Grupo 2, `remove`/`rewrite`)
- Ubicación: `claude_extractor.py:338-342` frente a 52 menciones de "MGR" en las líneas 397-407, 424-446, 455, 467, 473, 487, 500, 645, 675, 680-686, 695-707, 712, 720, 736, 741-746, 754-756, 765, 779-792, 807, 814-815, 819, 828-832, 839-840 y 858; `file_readers.py:431`, `464-488`.
- Evidencia: l.338 "Ya **no debes** apelar al 'MGR' como fuente" y l.342 "Cuando este prompt menciona 'MGR' más adelante, ignóralo — son referencias legacy". Pero l.426-430 define el MGR como "rectángulo de material consumido", l.832 ordena "úsalo TAL CUAL" el valor del MGR y `file_readers.py:464-466` lo mete en la jerarquía de fuentes.
- Contradicción en el repositorio: `file_readers._es_mgr_marmolista` (l.230) excluye esos ficheros antes de construir el contenido, y el filtrado se introdujo el 2026-04-28. El modelo recibe unas 45 líneas de reglas sobre un documento que no existe y una instrucción de ignorarlas: gasta atención en reconciliarlas.
- Acción en el diff: se reduce a un párrafo de fuentes (sin MGR en el flujo; si aparece un presupuesto, es solo referencia, usar la revisión más reciente y no duplicar), se elimina la sección "NO DOBLAR CUENTAS", las conversiones ml/m² del MGR y el párrafo de facturas ACyC, y se neutraliza el resto de menciones. Se conservan las reglas de taller que no dependían del MGR (sección keep-list abajo). Confianza: alta.

**A4. Forma de la encimera: polígono único frente a multi-tramo** (Grupo 2, conflicto, `rewrite`)
- Ubicación: `claude_extractor.py:357-358` ("Una encimera en L, U o con escalón es UN POLÍGONO ÚNICO"), 170-176, 248-262 y 810 ("NO las dividas en rectángulos") frente a `:440` ("L → 2 piezas. U → 3 piezas"), `:452` ("**SIEMPRE prefiere multi-tramo**"), `:458` ("**OBLIGATORIO** emitir multi-tramo") y 478-481.
- Orden por `git log -S`: multi-tramo (04-23, `bfed8b4`, cuando existía el MGR) es anterior al polígono único (04-28/04-30). Dirección asumida: manda el más reciente.
- Acción: se reescribe el bloque l.394-492 como una sola sección "Forma real y fondos reducidos" que conserva las reglas de taller (fondos reducidos 280/300/350/400/450, barra con vuelo, estimar y anotar, coherencia entre `notas` y vértices) sin pedir tramos separados. Es un cambio de comportamiento: validar con los lotes I016-I020 y J0028-J0034. Confianza: alta (instrucciones opuestas).

**A5. Pilar y pilastra: el prompt pide piezas que el posproceso descarta** (Grupo 2, `rewrite`)
- Ubicación: `claude_extractor.py:54` (paleta: "Marrón | pilar / pilastra | ... | Pieza con `vertices_mm`"), `:568-573` ("Se clasifican como tipo `pilastra`") frente a `claude_extractor.py:4651-4665` (descarta `pilar`/`pilastra` con advertencia) y `file_readers.py:547-558` ("PROHIBIDO: emitir piezas tipo 'pilar' o 'pilastra'").
- Orden: el "pilar como obstáculo" es del 2026-05-10 (`4b94e00`); la paleta es del 04-30. Se reescribe lo antiguo: paleta y sección Pilastra apuntan a muesca y chapeado envolvente, y las "pilastras de cocina de hierro" se emiten como `frontal` (coherente con la regla J0034 de las líneas 185-193). Confianza: alta.

**A6. Tamaños y márgenes de huecos duplicados con valores distintos** (Grupo 2, conflicto, `rewrite`)
- Ubicación: `claude_extractor.py:291-296` (fregadero 1 seno 500×400, 2 senos 780×440, escurridor 1000×440) frente a `:632-637` (400×400, 780×480, 1000×480); `:300`/`:302-303` ("Margen al frente ≥40mm", "Fregadero: centro_y ≈ 290", placa centrada) frente a `:625` (regla de taller de 2026-06-11: borde delantero a 80 mm del frente).
- Orden por blame: tabla de 04-23 contra tabla 2D de 04-28; márgenes de 04-28 contra la regla de 80 mm de 06-11. Se deja una sola tabla (largo × ancho) y `centro_y = 80 + ancho_hueco/2`. El ejemplo se actualiza en consecuencia (placa 326, fregadero 300 con 780×440, grifo 565). Confianza: alta para la contradicción; el valor correcto de cada medida lo debe confirmar el taller, porque la dirección asumida es "gana lo más reciente".

**A7. Sin caché de prompt** (Grupo 4, `add`)
- Ubicación: `claude_extractor.py:4527-4538`, `4575-4588`, `4614-4620`: `system=SYSTEM_PROMPT` como cadena, sin `cache_control`.
- Por qué: el system prompt es constante (sin marcas de tiempo ni UUID), supera el mínimo de 512 tokens y se envía hasta tres veces por trabajo y en cada trabajo de un lote. En Fable 5.1 la lectura de caché cuesta 0,025x. Acción: dos bloques `system` con `cache_control` (base común; anotaciones solo si hay). Cálculo en el Resumen. Confianza: alta. Nota: el TTL de 5 minutos cuenta desde el inicio de cada petición; con razonamiento largo, un lote espaciado puede fallar la caché (usar `ttl: "1h"` solo si la métrica `cache_read_input_tokens` lo pide).

### Confianza media

**M1. Sin manejo de `refusal` ni de `max_tokens`** (Grupo 4)
- Ubicación: `claude_extractor.py:1016-1020` solo imprime "Respuesta SIN TEXTO"; `extract_trabajo` no mira `stop_reason`.
- Fable 5.1 pasa clasificadores de seguridad que pueden dar falsos positivos en trabajo legítimo, y devuelve `stop_reason: "refusal"` con éxito HTTP. Acción en el diff: aviso explícito de `stop_reason` y retorno con advertencia sin reintentar. Las `fallbacks` del lado del servidor (beta `server-side-fallback-2026-06-01`, destinos Opus) se dejan como `flag`, porque el proyecto no quiere Opus y el dominio (encimeras) no es el que activa esos clasificadores.

**M2. Modelo por defecto contradictorio** (Grupo 4)
- Ubicación: `claude_extractor.py:4455` (`claude-sonnet-4-6`) frente a `main.py:148` (`claude-fable-5`). El diff pone `claude-fable-5-1` en `extract_trabajo`. Los otros fijados (`main.py:148-150`, `bench_autonomo.py:252`, `ARQUITECTURA.md:160,178`) quedan fuera de alcance y hay que cambiarlos a la vez.

**M3. Ejemplo JSON no válido y que contradice sus propias reglas** (Grupo 1c, ejemplos)
- Ubicación: `claude_extractor.py:886-947` (comentarios `/* ... */` dentro del JSON de ejemplo) y `:893-901`.
- El JSON con comentarios no es válido y el extractor no los elimina; un modelo que los copie cae en la segunda llamada. Además la geometría del ejemplo (`[2700,400],[3000,400],[3000,600]`, "saliente trasero") añade material fuera del bbox, justo lo que la regla de la línea 273 prohíbe ("NO uses saliente exterior (X > 2700). El pilar reduce la pieza"), y los pilares "van en Y alto" mientras el hueco del ejemplo queda en Y bajo. El diff lo sustituye por la muesca 2700×600 con pilar de 300×200 (cierre de aristas comprobado: 2700-300-2400=0; 400+200-600=0), con la explicación en prosa. Incluye también un nombre real de cliente (`Elisa Baños`, l.870-873) que pasa a datos ficticios. Probar con A/B porque un ejemplo único fija mucho el comportamiento.

**M4. Registro de énfasis y huecos de prioridad** (Grupo 1a)
- Ubicación: densidad en el prompt: "NO " 89 veces, NUNCA 16, IMPORTANTE 11, SIEMPRE 11, OBLIGATORI* 7, CRÍTICO 5, PROHIBIDO 3, más "REGLA INVIOLABLE... NO HAY EXCEPCIONES" (l.517-519), "REGLA AGRESIVA" (l.74), "LEER PRIMERO / LEER SIEMPRE / LEER ANTES DE EMPEZAR" (l.18, 168, 327) y "¡CUIDADO!" (l.65).
- Problema concreto: l.517-519 dice "NO HAY EXCEPCIONES" y el algoritmo de debajo (l.525-529) y la regla de cabezas vistas (l.546-552) sí tienen excepciones. Con tantos marcadores ninguno informa y Fable 5.1 sigue las instrucciones al pie de la letra. El diff solo toca los encabezados y esas dos reglas; no toca el contenido de dominio (cotas rotadas, ingletes implícitos, etc.).

**M5. Reglas de anotaciones mezcladas con las del modo autónomo** (Grupo 1d, acreción de parches)
- Ubicación: l.18-64, 91-106 y 143-150 (unos 7.000 caracteres, ~2.200 tokens) abren el prompt con "PRIORIDAD ABSOLUTA ... (LEER PRIMERO)" y "Regla número 1: ni añadir piezas que no estén anotadas, ni eliminar...", pero en modo autónomo (sin `anotaciones.json`) no hay overlay. Solo la regla de cotas rotadas y la de ingletes implícitos (l.65-90) valen para documentos sin anotar. Ver tabla más abajo.
- Acción en el diff: `SYSTEM_PROMPT` pasa a ser la base común y `SYSTEM_PROMPT_ANOTACIONES` se añade solo si `_hay_anotaciones(folder)` (misma condición que usa `build_claude_content`, l.446-450). El texto anotado es idéntico; solo cambia el orden en trabajos anotados (base primero). Cambia el comportamiento en autónomo: validar con `bench_autonomo.py`.

**M6. Bloque de anotaciones de `file_readers.py` repetido y en conflicto** (Grupo 2 y 1c)
- Ubicación: `file_readers.py:451-492`. La misma regla ("una área amarilla = una encimera") aparece tres veces (l.480, 483-488, 490-492), cita el MGR (l.464-466, 469-473, 483) y el párrafo "MARRÓN = PILAR ... genera 1-3 piezas" (l.474) choca con el bloque PILARES del mismo contexto (l.547-558: "El pilar NUNCA es una pieza de piedra"). El diff lo condensa en un párrafo, sin MGR, y aclara que las piezas pequeñas son de chapeado/encimera. También elimina la línea 431 ("PRECIOS... usa SOLO 'MGR-tabla'"). Confianza media porque la lectura "1-3 piezas pequeñas" admite la interpretación de chapeado.

**M7. El clasificador gasta llamadas en ficheros que nunca se envían** (Grupo 4, LLM sobre plan determinista)
- Ubicación: `clasificador.py:105-106` itera todos los ficheros salvo los ignorados, incluidos los PDF `PR*`/`F*` (hasta 10 páginas, una llamada por página) que `_es_mgr_marmolista` excluye después. Diff: `continue` si `_es_mgr_marmolista(archivo)` (importa una función privada; efecto secundario: `clasificacion.json` ya no lista esos ficheros).

### Baja confianza y `flag` (no están en el diff)

- F1. Encabezados y viñetas con emoji (34 caracteres: l.18, 108, 168, 248, 327, 357, 360, 373, 381, 384, 394, 410, 426, 454, 460...): huella de época sin daño documentado. Se pierden con las reescrituras anteriores en las secciones tocadas; el resto es un cambio mecánico opcional.
- F2. Pilares (l.384-392): "pilar exento ... 4 tramos ... cada uno = pieza" y "lateral pilar izq/dch" como piezas de encimera chocan con "una encimera continua = un polígono" y con "el pilar es un obstáculo". Requiere decisión del taller: ¿un pilar exento parte la encimera en piezas independientes?
- F3. Profundidad estándar: l.353 dice 600 mm; l.678 y 687 dicen 620 mm mínimo/por defecto; la regla del vuelo (l.194-199) suma 15-20 mm a cotas de mueble. Los ejemplos usan 600. Decidir una convención.
- F4. "Segundas": l.733-738 manda usar las Segundas como definitivas; `file_readers.py:443` manda tratar la carpeta raíz como la más definitiva y `collect_files` no procesa subcarpetas. Sin dirección clara por historial; confirmar si todavía entran PDF con etiqueta distinta de "Primeras" (`file_readers.py:414`).
- F5. `EXTRACTOR_EFFORT` sin fijar (`claude_extractor.py:1012`): el valor por defecto de Fable 5.1 es `high`. Los niveles no equivalen entre generaciones: repetir el barrido low/medium/high/xhigh con `bench_autonomo.py` antes de migrar. `max_tokens=64000` es razonable (límite 128K); a `xhigh`/`max` revisar que el JSON largo no se redacte dos veces en el razonamiento.
- F6. `file_readers.py:436` pide indicar de qué `[TIPO]` vino cada cota, pero el esquema no tiene campo para ello (instrucción sin cumplimiento posible).
- F7. Preproceso OCR (EasyOCR) y rasterizado a 200/250 dpi (`file_readers.py:~640-680`, fuera del tramo pedido): "visual-input scaffolding" candidato a prueba de retirada en Fable 5.1, que lee planos con más precisión. Probar con y sin OCR en la plantilla manuscrita.
- F8. Sin contabilidad de tokens: no se registra `usage` (incluidos `cache_read_input_tokens`/`cache_creation_input_tokens`) por trabajo. Es el requisito para medir cualquiera de los cambios anteriores.
- F9. Reintento con menos PDF (`claude_extractor.py:4614`) pierde la clasificación (ya listado en `ARQUITECTURA.md` §6.3): fuera de alcance de prompt.
- F10. Historial editado entre turnos (bloques de `thinking` enlazados a la conversación): no aplica; cada llamada es de un solo turno. La única llamada multiturno (la conversión a JSON) desaparece con A1.

## Reglas de taller que se conservan (keep-list)

No se ha tocado el contenido de dominio salvo para reconciliar contradicciones: cotas rotadas 180° y cero inicial; ingletes implícitos por esquina vista; polilíneas exactas del operador; encimera unificada; huecos nunca omitidos por posición desconocida; cocina de hierro exenta (J0034); vuelo de la encimera; chapeados por zona y pilar saliente; chapeado alto que parte la pared (restar, no copiar); no inventar chapeados; zócalos bajo todos los muebles bajos y cabezas vistas (J0028); copete solo en cabezas vistas; `aristas_contacto`/`aristas_cota` y cierre del polígono; escalones y asignación de cotas; defaults de huecos; subtipo de fregadero nulo si no consta; enchufes por defecto y exclusión de lavandería; múltiples materiales sin duplicar huecos ni cantos; cantos pulidos (qué se pule); ancho mínimo 620; cliente aporta encimera; "frontis"; ACyC (menos la línea de facturas MGR).

## Secciones que dependen de las anotaciones del operador frente a las que valen para documentos sin anotar

Dependen de anotaciones (overlay, notas globales, trazos):
- `claude_extractor.py:18-64` (prioridad del overlay, tipos de trazo, cotas remarcadas, paleta de colores, trazos de canto), `:91-106` (polilínea frente a mano alzada, orden de razonamiento con overlay), `:143-150` (zonas de anotación manual); `:333` (fuente 4).
- `file_readers.py:449-612`: leyenda de colores y jerarquía, notas globales, bloque PILARES, PAREDES Y MUEBLES ALTOS (regla de pulidos y frontales), POLILÍNEAS EXACTAS; más los rótulos de overlay de las páginas (l.~680-727).

Valen para documentos sin anotar (autónomo): `:65-90` (cotas rotadas, ingletes implícitos), `:108-142` y `:152-326` (identificación visual, reglas de taller, geometría 2D), `:327-975` completo (fuentes, tipos de pieza, huecos, cantos, medidas, enchufes, materiales, formato). Mixtas: el default de 80 mm de los huecos (l.625) y los ingletes implícitos ("aunque el operador no los marque"), que viven hoy dentro de la sección de overlay.

## Verificación sugerida (Paso 7)

1. Aplicar el diff en una rama y repetir los lotes con `bench_autonomo.py` sobre J0028/J0030/J0033/J0034 y I016-I020, comparando con `comparar_extracciones.py`; un cambio cada vez en los de más riesgo (A4 polígono, A6 huecos, M5 división del prompt, M3 ejemplo).
2. Comprobar `cache_read_input_tokens > 0` a partir del segundo trabajo del lote y que `stop_reason` nunca sea `refusal`/`max_tokens` sin aviso.
3. Probar el esquema de salida con una llamada real (restricciones de esquema, versión del SDK) y el clasificador de Haiku 5.5 sobre 20 páginas conocidas.
4. Antes de borrar nada más, buscar en el repositorio texto del prompt usado por otros programas (`grep` de "ingletado implícito", "trazo rojo", "auto exclusión": `ARQUITECTURA.md` §6.6 documenta contratos por subcadena en `notas`). Las subcadenas que esa sección cita (`trazo rojo`, `operador cian`, `auto exclusión`) las genera el código, no el prompt (0 apariciones en l.15-976); aun así, el ejemplo de "inglete cabeza-frontal implícito por esquina vista" (l.89) se ha conservado intacto por ese motivo.
