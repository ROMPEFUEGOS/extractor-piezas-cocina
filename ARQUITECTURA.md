# ARQUITECTURA — Pipeline de extracción de piezas de encimera (ExtractorPiezas)

Mapa de arquitectura del pipeline para decidir dónde trabajar de cara al objetivo de **extracción 100% autónoma** (sin `anotaciones.json` del operador). Todas las referencias son `archivo:línea` verificadas con Grep/Read sobre el estado actual del repo.

---

## 1. Flujo de extract_trabajo

`extract_trabajo()` está en **claude_extractor.py:4452**. OJO: la cadena de postprocesado NO está dentro de `extract_trabajo()` sino dentro de `json_to_trabajo()` (claude_extractor.py:1088), que `extract_trabajo` llama en :4606. Orden real de ejecución:

| # | Paso | Función | Línea |
|---|------|---------|-------|
| 1 | Info del nombre de carpeta (job_id, cliente, tienda, ciudad) | `parse_folder_name` | claude_extractor.py:979 (llamada en :4470) |
| 2 | Si no existe `clasificacion.json` y `auto_clasificar=True`: clasificar fuentes con Claude Haiku y escribir `clasificacion.json`; si existe, cargarlo | `clasificador.clasificar_proyecto` | clasificador.py:94 (llamada en claude_extractor.py:4475-4496) |
| 3 | Construir bloques de contenido (textos + imágenes base64 + overlays del operador + etiquetas `[TIPO:…]`) y lista de archivos | `build_claude_content` (+ `collect_files`) | file_readers.py:314 (y :261); llamada en claude_extractor.py:4499 |
| 4 | Si solo hay el texto de contexto (sin archivos útiles) → devolver `TrabajoExtraido` con confianza 'baja' | inline | claude_extractor.py:4502-4514 |
| 5 | Validar API key y crear cliente | inline | claude_extractor.py:4517-4521 |
| 6 | Llamada a Claude con `SYSTEM_PROMPT` (streaming, `max_tokens=64000`), hasta 3 intentos con backoff ante `RateLimitError`; ante otro error → retorno con advertencia | `_mensaje_streaming`, `_texto_respuesta` | claude_extractor.py:1005, :1024 (bucle en :4528-4562) |
| 7 | Extraer JSON de la respuesta; si no hay JSON, segunda llamada pidiendo "solo el JSON"; si sigue fallando → retorno con advertencia | `extract_json_from_response` | claude_extractor.py:1031 (usado en :4568 y :4591; reintento :4571-4604) |
| 8 | **`json_to_trabajo(data, folder_info, folder=folder)`** — parsea el JSON a `TrabajoExtraido` y ejecuta TODA la cadena de postprocesado (ver desglose abajo) | `json_to_trabajo` | claude_extractor.py:1088 (llamada en :4606) |
| 9 | Limpieza final (materiales vacíos, normalización de tipos, descarte de pilares, cantos inválidos → observaciones) | `_limpiar_trabajo` | claude_extractor.py:4634 (llamada en :4607) |
| 10 | Si `materiales` vacío y >4 archivos: reintentar TODO con `max_pdfs=3` (sin pasar `clasificacion`); si el segundo intento tiene materiales, se devuelve ese y se descarta el primero | inline + `build_claude_content` | claude_extractor.py:4610-4628 |
| 11 | Asignar `archivos_fuente` y devolver | inline | claude_extractor.py:4630-4631 |

Desglose de la cadena DENTRO de `json_to_trabajo` (claude_extractor.py:1221-1253), en orden exacto:

1. Parseo de `materiales`/`piezas`/`huecos`/`cantos` (con validación de `aristas_contacto` y `aristas_cota` malformadas → se ignoran, :1136-1156).
2. `_cargar_paredes_y_muebles_altos(folder, trabajo)` — claude_extractor.py:1615, llamada en :1223 (lee `anotaciones.json`, asigna `pieza._anot_reg` con trazos en píxeles).
3. Snapshot de lo emitido por Claude ANTES del postproc (`snapshot_claude`) — :1228-1231.
4. `_espejar_opciones` — :3947, llamada en :1232.
5. `_ajustar_costado_cascada` — :1985, llamada en :1233.
6. `_reconciliar_geometria_encimera` — :2191, llamada en :1234 (incluye `_dedup_frontales` :2032, escuadrado/snap a cotas, validación contra polilínea, clasificación de aristas, pulidos/ingletes/copetes por exclusión).
7. `_completar_piezas_desde_trazos` — :4011, llamada en :1235.
8. `_reposicionar_huecos_desde_trazos` — :3560, llamada en :1236.
9. `_ajustar_profundidad_huecos` — :3752, llamada en :1237.
10. `_validar_huecos_dentro` — :3817, llamada en :1238.
11. `_completar_pulidos_pata` — :1880, llamada en :1239.
12. `_completar_inglete_pata` — :1830, llamada en :1240.
13. `_completar_copete_principal` — :1256, llamada en :1241.
14. `_completar_ingletes_implicitos` — :3045, llamada en :1242.
15. `_verificar_muesca_pilar` — :4328, llamada en :1243.
16. `_espejar_opciones` (SEGUNDA vez, para piezas creadas por el postproc) — llamada en :1247.
17. `_volcar_divergencia(trabajo, folder, snapshot_claude)` — :4389, llamada en :1250 (escribe `divergencia.json` en la carpeta del trabajo).

---

## 2. Prompt del sistema

`SYSTEM_PROMPT` es una única cadena de **~78.200 caracteres** (78 KB) que ocupa **claude_extractor.py:15-976**. Se envía como `system` en las llamadas (:4534, :4588, :4617).

### Secciones principales (título → rango de líneas aproximado)

| Sección | Líneas | Contenido |
|---|---|---|
| Intro (rol) | 15-17 | Experto en extracción de proyectos de encimeras |
| 🎨 PRIORIDAD ABSOLUTA — ANOTACIONES DEL OPERADOR | 18-106 | **Solo aplica si hay overlay**: tipos de trazo (contorno cerrado / lineal / cruz roja, :26-41), cotas remarcadas (:43-44), paleta de colores→tipo de pieza (:46-57), reglas de trazos de canto cian/magenta (:59-63), cotas rotadas 180° (:65-78), ingletes implícitos (:80-89), polilínea vs mano alzada (:91-98), orden de razonamiento (:100-104). En :106: *"Si NO hay overlay, sigue las reglas de identificación visual de la sección siguiente"* |
| 🔍 IDENTIFICACIÓN VISUAL (sin overlay) | 108-166 | Muebles altos (X)/bajos, zonas sin muebles altos, columnas, cabeza vista, **zonas de anotación manual** (:143-150, colores del operador), paredes, aplicación práctica |
| 📐 REGLAS ADICIONALES — CASOS QUE SE OLVIDAN | 168-246 | Encimera unificada L/U, huecos nunca omitir (J0034), cocina de hierro, vuelo de encimera (+15-20mm), chapeados por zona, rodapiés de todas las zonas, copetes regla estricta |
| 🗺 REPRESENTACIÓN GEOMÉTRICA 2D | 248-325 | Convención de ejes (X largo, Y=0 frente, Y=alto pared, CCW desde frontal-izq), formas típicas (rect/L/U/muesca), bbox obligatorio, tamaños estándar de huecos, posicionamiento y no solapamiento, chapeados en esquina de pilar |
| ⚠ FUENTES DE INFORMACIÓN | 327-342 | Las 5 fuentes válidas (la 4ª = anotaciones del operador, :333); **MGR filtrado del input** (:336-342, "referencias legacy") |
| Tipos de documentos | 344-348 | Plantilla, plano 2D, render 3D, TXT |
| TIPOS DE PIEZAS — DEFINICIONES PRECISAS | 350-603 | Subsecciones: Encimera :352, Frontal :494, Copete :507, Zócalo/Rodapié :562 y :589, Pilastra :568, Pilastras de hierro :572, Isla/Península :575, Costado/Cascada :579 |
| ELABORACIONES Y TRATAMIENTOS | 605-671 | Bisel, boleado, etc. |
| MEDIDAS | 673-689 | Unidades, redondeos |
| LECTURA DE LETRA MANUSCRITA | 691-707 | Crítico para plantilla |
| GROSOR DOBLE "12+12" | 709-712 | |
| NOMBRE DEL CLIENTE | 714-716 | |
| HUECOS — ENCHUFES | 718-731 | |
| MEDIDAS REVISADAS — SUBCARPETAS | 733-738 | |
| MÚLTIPLES PRESUPUESTOS / ZONAS / DISCREPANCIAS / CASOS ESPECIALES | 740-796 | Incluye "MÚLTIPLES MATERIALES" :771-796 (sufijos `_opcionN`) |
| COSTADO INGLETADO / REGLAS IMPORTANTES / CANTOS PULIDOS / ESPESOR / INGLETADO PORCELÁNICO / EMPRESA ACyC | 798-861 | |
| FORMATO DE RESPUESTA | 863-975 | JSON puro sin markdown; ejemplo completo de J0297 |

### Campos JSON que el prompt pide emitir

- Trabajo: `job_id`, `cliente`, `tienda`, `vendedor`, `ciudad`, `direccion`, `piso`, `fecha`, `observaciones`, `notas_extra`, `confianza`, `advertencias`, `fregadero_tipo`, `tipo_cascada`, `tablas_reservadas`, `proveedor_tablas`.
- `materiales[]`: `rol`, `marca`, `color`, `grosor_cm`, `acabado`, `altura_cm`, `canto`, `es_igual_a`, `notas`.
- `piezas[]`: `tipo`, `material_rol`, `forma`, **`vertices_mm`**, **`aristas_contacto`** (`pared|vista|mueble|ventana`, alineada con vértices, :894-898), **`aristas_cota`** (cota leída por arista o `null`, "nunca valores calculados", :899-901), `largo_mm`, `ancho_mm`, `altura_mm`, `area_m2`, `longitud_ml`, `zona`, `notas`.
- `huecos[]`: `tipo`, `cantidad`, `subtipo`, `pieza_zona`, `centro_x_mm`, `centro_y_mm`, `largo_mm`, `ancho_mm`, `posicion`, `distancia_lado_mm`, `notas`.
- `cantos[]`: `tipo` (`recto_pulido`, `recto_pulido_agua`, `ingletado`…), `longitud_ml`, `notas`.

### Reglas condicionales a anotaciones del operador vs documentos crudos

- **Condicionales a overlay/trazos** (solo si el operador anotó): todo el bloque :18-106 — "ni añadir piezas que no estén anotadas ni eliminar las anotadas" (:22), lectura de vértices del contorno del operador (:26-33), paleta de colores estricta (:46-57), trazos cian=pulido / magenta=inglete (:59-63), "cotas remarcadas/subrayadas" (:43-44), polilíneas exactas (:91-98), orden de razonamiento por blobs (:100-104). También :143-150 ("ZONAS DE ANOTACIÓN MANUAL") y :325 ("si pintaste pilar marrón…").
- **Aplican a documentos crudos (autónomas)**: :108-166 (identificación visual de muebles/paredes), :168-246 (reglas de taller y casos), :248-325 (geometría 2D y convención de ejes), :80-89 (**ingletes implícitos "aunque el operador no los marque"** — regla geométrica pura), :65-78 (cotas rotadas 180°, geometría del plano).
- Además, cuando hay anotaciones, `build_claude_content` inyecta bloques ADICIONALES de contexto: jerarquía de fuentes con "AUTORIDAD ABSOLUTA" del rotulador (file_readers.py:446-499), pilares (:546-563), paredes/muebles altos (:565-589) y **polilíneas exactas en píxeles con instrucciones de escalado** (:591-612). En modo autónomo ninguno de estos bloques existe.

---

## 3. Tabla de funciones de postprocesado

Convención de inputs: **(a)** solo JSON de Claude · **(b)** anotaciones del operador (tipo de trazo) · **(c)** cotas de documentos · **(d)** otros archivos.

| Función (línea) | Qué cambia | Inputs | Comportamiento SIN anotaciones | Advertencias que emite |
|---|---|---|---|---|
| `json_to_trabajo` (claude_extractor.py:1088) | Dict→`TrabajoExtraido`; valida `aristas_contacto`/`aristas_cota` malformadas; orquesta toda la cadena | (a) JSON de Claude; (d) `folder` (para anotaciones y divergencia) | Funciona; se salta el paso 2 de la cadena | "error cargando paredes/muebles_altos", "error volcando divergencia" (:1225, :1252) |
| `_cargar_paredes_y_muebles_altos` (:1615) | Lee `anotaciones.json`, desinclina página (`_desinclinar_pagina` :1569) y asigna `pieza._anot_reg` con trazos en píxeles (pared, muebles_altos, copete, pulido, inglete, frontal, zocalo, costado, pilar, hueco + polilínea de encimera) | (b) TODOS los tipos de trazo; (a) `vertices_mm` de encimeras para el matching por área | **Se sale inmediatamente**: `if not anot_path.exists(): return` (:1626-1628). Sin esto, ningún downstream tiene `_anot_reg` | "anotaciones.json corrupto…", "esquema inesperado…", "polilínea de N puntos asignada por área…" (:1632-1639, :1777-1780) |
| `_espejar_opciones` (:3947) | Clona piezas físicas entre opciones de material (`_opcion1/2/3/_a/_b`) para igualar presupuestos | (a) `material_rol`, `tipo`, dims, `vertices_mm` | **Activo** (autónomo) | "pieza X espejada de _opcionN a _opcionM" (:4003-4006) |
| `_ajustar_costado_cascada` (:1985) | Reescribe `largo_mm` de costados que miden el lado LARGO de la isla → cabeza (lado corto); ajusta ingletes 1×/2× | (a) piezas `isla`/`costado`, `cantos` | **Activo** (autónomo) | "costado(s) ajustado(s) a cabeza isla", "inglete ajustado" (:2012-2028) |
| `_reconciliar_geometria_encimera` (:2191) | Núcleo: `_dedup_frontales` (:2032/:2213), `_escuadrar_poligono` (:2142/:2248), reescalado y `_snap_vertices_a_cotas` (:1397/:2310), `_ajustar_fondos_a_cotas` (:2086/:2342), validación/reconstrucción contra polilínea (`_alinear_poligono_con_polilinea` :3117, `_reconstruir_desde_polilinea` :3291, `_resolver_poligono_por_cierre` :3162), clasificación de aristas (pixel: `_clasificar_aristas_pixel` :3446 > mm: trazos :2462-2478 > `aristas_contacto` de Claude :2479-2493 > heurística cóncava `_clasificar_aristas_encimera` :1478), pulido por exclusión (:2611-2625), ingletes del operador (:2572-2603), copete por exclusión + subset-sum (:2686-2884), inglete de esquina refleja (:2910-2975), pulido cabeza vista de chapeado (:2982-3001), aviso reparto frontal alto (:3007-3036) | (a) `vertices_mm`, `aristas_contacto`, `aristas_cota`, piezas/cantos/copetes; (b) trazos pared/muebles_altos/copete/pulido/inglete/frontal/zocalo; (c) cotas del JSON y texto (`_recolectar_cotas` :1340) | **Degradado**: sin `_anot_reg` se salta validación contra polilínea y reconstrucción; clasificación cae a `aristas_contacto` (fuente `'claude'`) o a heurística cóncava con aviso ⚠ (:2498-2501); **copete por exclusión solo con fuente `operador` o `claude`** (`if fuente_clasificacion in ('operador','claude')` :2639); el "paño corrido" es **exclusivo del modo autónomo** (`fuente_clasificacion == 'claude'` :2821); eliminación de copetes fantasma solo con `fuente == 'operador'` (:2889) | Muchas: "reescalado a cotas propias", "snap revertido — polígono degenerado", "fondo(s) ajustado(s)", "⚠ GEOMETRÍA DUDOSA…", "⚠ trazos del operador NO aplicados (geometría no casa)", "aristas por trazos…/aristas_contacto de Claude…/⚠ heurística SIN confirmar", "N pulidos + M ingletes operador", "añadido copete…", "eliminado copete fantasma", "inglete de esquina…", "pulido en cabeza vista del chapeado", "⚠ VERIFICAR reparto" (:2305-2308, :2326-2329, :2350-2352, :2385-2390, :2400-2403, :2458-2501, :2627-2630, :2827-2830, :2864-2867, :2881-2884, :2896-2899, :2972-2975, :2999-3001, :3028-3035) |
| `_completar_piezas_desde_trazos` (:4011) | Crea piezas copete/zócalo/frontal/costado desde trazos del operador que Claude no emitió; consume pool de piezas de Claude ±20%; retira copetes auto sustituidos por chapeado marcado (:4287-4297); red anti-invención de frontales (:4304-4325) | (b) trazos copete/zocalo/frontal/costado + polilínea; (a) piezas existentes, materiales, `_aristas_tipos` | **Se salta por encimera**: `if not regs or getattr(enc,'_anot_no_fiable',False): continue` (:4087-4089); también `if getattr(enc,'_sin_piezas_desde_trazos',False)` (:4090-4095, geometrías resueltas por cierre) | "añadido {tipo} {L}ml desde trazo del operador", "⚠ Frontal auto… ¿tramo del mismo paño? VERIFICAR", "copete auto retirado", "⚠ Frontal SIN trazo del operador que lo respalde" (:4267-4269, :4279-4283, :4294-4297, :4322-4325) |
| `_reposicionar_huecos_desde_trazos` (:3560) | Marcas rojas del operador = posición autoritativa de huecos; asignación global óptima grandes↔placa/fregadero (:3689-3725), ticks→enchufes (:3727-3749), giro 90°, grifo recolocado detrás del fregadero hacia la pared clasificada (:3655-3687) | (b) trazos `hueco` + polilínea; (a) `huecos` (tipo, dims), `_aristas_tipos` | **Se salta**: `if (not regs or not regs.get('huecos_pix') or getattr(enc,'_anot_no_fiable',False)): continue` (:3574-3577) | "hueco reposicionado por trazo del operador", "girado 90° según la marca", "marca roja ignorada — no casa con ningún hueco", "marca puntual sin hueco libre — ¿enchufe adicional? VERIFICAR" (:3634-3652, :3720-3723, :3746-3749) |
| `_ajustar_profundidad_huecos` (:3752) | Regla de taller: borde delantero de placa/fregadero a **80mm** del frente (solo coordenada perpendicular) | (a) `huecos` con centro, `_aristas_tipos` (necesario, :3762-3764); (b) indirecto: respeta posición si nota contiene `'trazo rojo'` (:3778, :3799-3804) | **Activo solo si hay `_aristas_tipos`** (que en modo autónomo requiere `aristas_contacto` válido); si Claude no emitió `aristas_contacto`, se salta silenciosamente | "hueco llevado a 80mm del frente" (:3812-3814) |
| `_validar_huecos_dentro` (:3817) | Invariante: placa/fregadero íntegros dentro del polígono; prueba giro 90°, deslizamiento por el frente; grifo acompaña al fregadero (:3937-3944) | (a) `huecos`, `vertices_mm`, `pieza_zona`; (b) marca roja = autoritativa (solo empujón ≤30mm permitido, :3856-3883) | **Activo** (autónomo); la rama "marca del operador" no aplica | "hueco (marca del operador) ajustado Nmm para caber", "⚠ NO cabe — revisar geometría o marca", "hueco girado/deslizado para caber", "— VERIFICAR posición lateral" (:3871-3935) |
| `_completar_pulidos_pata` (:1880) | Pulidos verticales de la pata/cascada: 2 si isla libre, 1 si toca pared; con `_aristas_tipos` usa esa verdad (:1917-1929) | (a) piezas `costado`/`isla`; (b) `paredes_pix` para el fallback "pared cerca" (:1940-1957) | **Activo con heurística**: sin trazos asume isla libre → 2 pulidos (:1930-1958); dedup contra cantos con nota "operador cian" (:1962-1971, sin efecto sin anotaciones) | "Postproc pata: N pulido(s) vertical(es)" (:1979-1981) |
| `_completar_inglete_pata` (:1830) | Inglete pata-encimera automático (longitud = cabeza de la pata) | (a) piezas `costado`, `cantos` | **Activo** (autónomo por diseño, "no requiere anotación magenta", :1831-1834) | "N inglete(s) pata-encimera auto-generados" (:1876-1877) |
| `_completar_copete_principal` (:1256) | Añade el copete de pared trasera si solo hay copetes de cabeza y no hay chapeado | (a) piezas copete/encimera/frontal; (b) **gate**: si cualquier pieza tiene `_anot_reg.copetes_pix` se aborta (`if any((getattr(p,'_anot_reg',None) or {}).get('copetes_pix')…): return` :1273-1275); también salta encimeras con `_anot_reg` o `aristas_contacto` (:1287) | **Activo** (heurística pensada para omisiones del LLM); sin anotaciones actúa en más encimeras | Ninguna (solo `notas` en la pieza creada, :1335) |
| `_completar_ingletes_implicitos` (:3045) | Inglete frontal-cabeza por cada pulido cuyo texto mencione "cabeza"/"lateral"; fondo ref = ancho más común de encimeras (capado a 0.7m) | (a) `cantos` (por el TEXTO de sus notas) | **Activo**; excluye pulidos "auto exclusión" (:3080) — es decir, en modo autónomo puro casi todos los pulidos son de exclusión y este paso apenas genera nada | Ninguna |
| `_verificar_muesca_pilar` (:4328) | Red de seguridad: pilar marcado dentro/pegado al polígono sin vértices cóncavos cerca → falta muesca | (b) trazos `pilar` + polilínea; (a) `vertices_mm` | **Se salta**: `if (not regs or not regs.get('pilares_pix') or …): continue` (:4358-4361) | "⚠ PILAR sin muesca… revisar geometría" (:4383-4386) |
| `_volcar_divergencia` (:4389) | Escribe `<carpeta>/divergencia.json` comparando snapshot Claude vs resultado postproc (métrica % cantos acertados, piezas añadidas/eliminadas) | (a) snapshot; (d) carpeta de salida | **Activo** (siempre que `folder` no sea None, :1248-1252) | Ninguna (efecto: archivo escrito) |
| `_limpiar_trabajo` (:4634) | Filtra materiales vacíos, simplifica `material_rol` con "/", descarta piezas sin tipo/material y pilares (:4651-4667), normaliza tipos de pieza y de canto, mueve no-cantos a observaciones | (a) JSON de Claude | **Activo** | "Postproc: descartadas N pieza(s) pilar/pilastra" (:4665-4667) |
| Helpers geométricos: `_recolectar_cotas` (:1340), `_snap_vertices_a_cotas` (:1397), `_signed_area_2d` (:1468), `_clasificar_aristas_encimera` (:1478), `_trazos_a_mm_local` (:1520), `_desinclinar_pagina` (:1569), `_dist_punto_a_segmento` (:1794), `_dist_minima_arista_a_trazos` (:1807), `_path_length_mm` (:1818), `_escuadrar_poligono` (:2142), `_punto_en_poligono` (:3102), `_alinear_poligono_con_polilinea` (:3117), `_resolver_poligono_por_cierre` (:3162), `_mapa_aristas_por_cercania` (:3404), `_clasificar_aristas_pixel` (:3446) | Utilidades puras del reconciliador (mm↔px, snapping, cierre por cotas, cobertura de trazos) | Mezcla (a)/(b)/(c) según helper | Varios solo tienen sentido con trazos (`_trazos_a_mm_local`, `_desinclinar_pagina`, `_clasificar_aristas_pixel`, `_mapa_aristas_por_cercania`); los de cotas son autónomos | Los que aplican se listan en la fila del reconciliador |

---

## 4. Dependencia de anotaciones

### Capacidades que SOLO existen hoy con `anotaciones.json` (se saltan o degradan sin él)

- **Carga de trazos**: `_cargar_paredes_y_muebles_altos` retorna si no existe el archivo (claude_extractor.py:1626-1628) → ninguna pieza tiene `_anot_reg`.
- **Clasificación de aristas pared/vista con verdad de operador** (fuente `'operador'`, :2449-2478) y su variante en píxel (`_clasificar_aristas_pixel` :3446).
- **Validación de forma y reconstrucción del polígono** desde la polilínea del operador (:2354-2411, `_reconstruir_desde_polilinea` :3291, solver de cierre `_resolver_poligono_por_cierre` :3162) — caza escalones con cotas intercambiadas.
- **Reposicionamiento autoritativo de huecos** (marcas rojas): `_reposicionar_huecos_desde_trazos` (:3574-3577) — sin anotaciones la posición del hueco es la que dice Claude + regla 80mm.
- **Creación de piezas desde trazos** (copete/zócalo/frontal/costado que Claude omitió): `_completar_piezas_desde_trazos` (:4087-4089).
- **Verificación de muesca de pilar**: `_verificar_muesca_pilar` (:4358-4361).
- **Eliminación de copetes fantasma** (solo fuente `'operador'`, :2889) e ingletes/pulidos explícitos cian/magenta (:2522-2603).
- **Corrección de inclinación del escaneo**: `_desinclinar_pagina` (:1569) solo opera sobre polilíneas de anotación — en modo autónomo ningún documento se endereza.
- **Bloques de prompt** con polilíneas exactas, paredes, muebles altos, pilares y jerarquía "AUTORIDAD ABSOLUTA" (file_readers.py:446-612), y las imágenes de overlay (original + overlay-solo-blanco, file_readers.py:683-705, :713-727).

### Capacidades que ya funcionan en modo autónomo

- Todo lo que depende solo del JSON de Claude: parseo, `_limpiar_trabajo`, `_espejar_opciones`, `_dedup_frontales`, `_ajustar_costado_cascada`, escuadrado/snap a cotas/ajuste de fondos (`_escuadrar_poligono`, `_snap_vertices_a_cotas`, `_ajustar_fondos_a_cotas`) y `_recolectar_cotas`.
- Clasificación de aristas vía `aristas_contacto` de Claude (fuente `'claude'`, :2479-2493) → habilita copete por exclusión (:2639) y **el "paño corrido", que está explícitamente limitado a modo autónomo** (`fuente_clasificacion == 'claude'`, :2821).
- Pulido por exclusión (:2611-2625) con cualquier fuente; inglete de esquina refleja (:2910-2975) y pulido de cabeza vista de chapeado (:2982-3001) — dependen de que haya clasificación (`claude` u `operador`).
- `_ajustar_profundidad_huecos` (:3752) y `_validar_huecos_dentro` (:3817) — el primero requiere que `_aristas_tipos` exista (o sea, `aristas_contacto` válido o trazos).
- `_completar_copete_principal` (:1256), `_completar_ingletes_implicitos` (:3045), `_completar_inglete_pata` (:1830), `_completar_pulidos_pata` (:1880, con fallback heurístico "isla libre → 2 pulidos" :1930-1958).
- Clasificación previa de documentos (`clasificacion.json`) y lectura/OCR de PDFs — no dependen de anotaciones.

**En resumen**: en modo autónomo, la geometría final depende de `vertices_mm`+`aristas_contacto`+`aristas_cota` de Claude corregidos por cotas; sin `aristas_contacto` y con polígono convexo el reconciliador no hace nada y solo avisa (:2494-2501). Copetes/zócalos/frontales no emitidos por Claude no tienen ninguna vía de recuperación (la única, `_completar_copete_principal`, exige que ya exista al menos un copete :1276-1278).

---

## 5. Lectura de documentos

### Qué produce `file_readers.py` por tipo de documento

| Tipo | Función (línea) | Producto | Notas |
|---|---|---|---|
| Imagen (jpg/png/…) | `image_to_base64` (file_readers.py:32) | Imagen base64 para Claude | ≤3.5MB; si excede, reescala LANCZOS + JPEG q85→55 |
| PDF "manuscrito" (nombre contiene `plantilla`, `presupuesto encimera`, `encimera `) | `pdf_pages_to_base64` (:132) + `run_easyocr_on_image` (:85) | Imágenes base64 **a 250 dpi** (máx. 5 páginas, JPEG q85, reducción 75%/50% si >4.5MB) + bloque de texto con OCR EasyOCR (es+en, conf>0.1) como pista | El flag se decide SOLO por el nombre de archivo (:641, :661-662) |
| PDF digital (pdfplumber ≥80 chars/pág) | `pdf_extract_text` (:104) | Bloque de **texto** (máx. 5 páginas) | Se envía como texto, sin imágenes |
| PDF escaneado no-manuscrito | `pdf_pages_to_base64` | Imágenes base64 **a 200 dpi** | |
| Excel | `read_excel_as_text` (:173) | Texto tabulado (todas las hojas) | openpyxl `data_only=True` |
| TXT | `read_txt` (:194) | Texto (utf-8/latin-1/cp1252…) | |

`collect_files` (file_readers.py:261) solo recorre la **carpeta raíz** (las subcarpetas Segundas/Terceras se listan como nota, no se procesan, :287-290 y :440-443), limita a **5 PDFs** priorizados por `_score_pdf` (:205: 'plantilla'/'presupuesto' +100, 'planta/encimera/diseño/cocina' +80, `PR\d+`/`F\d+` +50+N/100, fechas +60) y **excluye el MGR** con `_es_mgr_marmolista` (:230: PDFs `PR*`/`F*`, Excels con hoja "Presupuesto") — `mgr_excluidos` solo se informa (:420, :294-296).

### Uso de `clasificacion.json` para routing

- La genera `clasificador.clasificar_proyecto` (clasificador.py:94) con `claude-haiku-4-5`, PDFs a **140 dpi** máx. 10 páginas (:128), una llamada por página con `PROMPT` de 9 categorías (`CATEGORIAS` :29-39: `plano-planta-2D-cad`, `plano-manuscrito`, `plano-anotado`, `perspectiva-3D`, `plantilla-texto`, `MGR-tabla`, `foto-cocina`, `whatsapp-plantilla`, `otro`) + `cotas_visibles_mm`.
- `extract_trabajo` la crea si falta (claude_extractor.py:4475-4489) o la carga si existe (:4490-4496).
- En `build_claude_content`, `_tag` (file_readers.py:397-412) añade a cada imagen/página una etiqueta `[TIPO:<cat> cotas:[…]]`, y el bloque de contexto (:427-436) ordena el enrutamiento: forma→solo planos, precios→MGR-tabla, huecos/material→plantilla-texto, 3D/fotos→solo contexto sin cotas.
- ⚠ En el reintento con menos PDFs NO se pasa `clasificacion` (claude_extractor.py:4613) → el segundo intento pierde todas las etiquetas `[TIPO]`.

### Qué llega a Claude como imagen vs texto

- **Como imagen**: PDFs escaneados/manuscritos (200/250 dpi), fotos/renders (extensiones de imagen). Con anotaciones, cada página anotada viaja DOBLE: original + overlay-solo-sobre-blanco (file_readers.py:683-699 para PDFs, :713-727 para imágenes), más el texto de polilíneas/paredes/pilares (:501-612).
- **Como texto**: TXT (:617-623), Excel (:626-632), PDFs con capa de texto (:648-656), OCR EasyOCR de plantillas manuscritas (:669-679).
- **No llegan**: MGR (PR*/F*, Excels de presupuesto), PDFs fuera del top-5, subcarpetas de revisión (solo se citan sus nombres).

---

## 6. Observaciones (debilidades para el modo autónomo)

1. **Código muerto**: `_imagen_anotada_b64` (file_readers.py:358) se define y nunca se llama — el composite anotado que documenta (:359-361) no se usa.
2. **Variable muerta**: `hay_anotacion_pulido_global` (claude_extractor.py:2218-2219) se calcula y jamás se lee — la lógica de "pulidos del operador autoritativos globalmente" quedó a medias.
3. **Reintento inconsistente**: al reintentar con `max_pdfs=3` no se pasa `clasificacion` (claude_extractor.py:4613 vs :4499-4500) → el segundo intento va sin etiquetas `[TIPO]` y con distinto contexto que el primero.
4. **Modelos por defecto contradictorios**: `extract_trabajo` usa `claude-sonnet-4-6` (claude_extractor.py:4455) pero `main.py` inyecta `claude-fable-5` (main.py:148) y su ayuda menciona `claude-opus-4-8` (main.py:150).
5. **`--guardar` innegable**: `argparse` con `action='store_true', default=True` (clasificador.py:154-155) hace imposible NO escribir `clasificacion.json` desde CLI.
6. **Contratos entre funciones por texto libre de `notas`**: el gating depende de subcadenas: `'trazo rojo'` (claude_extractor.py:3778, :3856), `'auto exclusión'` (:3080), `'cabeza'/'lateral'` (:3074), `'operador cian'` (:1891, :2562), `'postproc'/'espejad'` (:4310), `'Auto exclusión'` en zona (:4292). Cualquier cambio de wording rompe silenciosamente la lógica.
7. **Afinidad por zona duplicada e inconsistente**: el matching pieza↔encimera por "2 primeros tokens de zona" se reimplementa con variantes en claude_extractor.py:2670-2678, :3578-3580, :3770-3774, :3832-3846 — frágil y difícil de mantener.
8. **En modo autónomo sin `aristas_contacto` y polígono convexo no hay reconciliación** (claude_extractor.py:2494-2501) y el copete por exclusión está vetado a fuentes `operador|claude` (:2639): copetes, zócalos y frontales omitidos por Claude no tienen recuperación (el único salvavidas, `_completar_copete_principal` :1276-1278, exige que Claude ya emitiera algún copete).
9. **Heurísticas de nombre de archivo hard-coded**: la decisión "manuscrito→imágenes+OCR" depende de las subcadenas `'plantilla'/'presupuesto encimera'/'encimera '` (file_readers.py:641) y la priorización/exclusión MGR de patrones `PR/F\d+` (:219-223, :244) — un renombrado de archivo cambia el comportamiento del pipeline.
10. **Resoluciones incoherentes y sin deskew autónomo**: el clasificador rasteriza a 140 dpi (clasificador.py:128), el extractor a 200/250 dpi (file_readers.py:661) — las cotas del clasificador se leen sobre otra imagen; y `_desinclinar_pagina` (claude_extractor.py:1569) solo endereza escaneos si HAY anotaciones, por lo que en modo autónomo un plano torcido entra tal cual a Claude.

---

*Documento generado solo-lectura; único archivo creado: `ARQUITECTURA.md`.*
