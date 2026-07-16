"""
Extracción de datos usando Claude con visión.
"""
import json
import os
import re
from pathlib import Path
from typing import Optional

import anthropic

from models import TrabajoExtraido, MaterialSpec, Pieza, Hueco, Canto
from file_readers import build_claude_content, collect_files

SYSTEM_PROMPT = """Eres un experto en la extracción de datos de proyectos de encimeras y revestimientos de piedra para cocinas.
Tu tarea es analizar los documentos de medición de una carpeta de trabajo y extraer TODA la información relevante para fabricar las piezas reales.

## 🎨 PRIORIDAD ABSOLUTA — ANOTACIONES DEL OPERADOR (LEER PRIMERO)

Si junto al plano recibes una **imagen de overlay** (capa con trazos de colores sobre fondo blanco o transparente), esa anotación es la **VERDAD GROUND-TRUTH**. El operador ya identificó cada pieza por ti. Tu trabajo NO es clasificar ni inventar piezas, es **MEDIR** lo que él ya marcó.

**Regla número 1**: ni añadir piezas que no estén anotadas, ni eliminar piezas que sí estén anotadas. Una pieza anotada = una pieza emitida.

**Tipos de trazo y cómo interpretarlos**:

1. **CONTORNO CERRADO (forma 2D real)** — usado en `encimera`, `frontal/chapeado`, `costado/cascada`:
   - Cada blob de color cerrado = UNA pieza. El contorno define el polígono.
   - Lee los vértices del trazo y emítelos como `vertices_mm` en orden CCW.
   - La forma puede ser rectángulo, L, U, con muesca de pilar — respeta exactamente lo dibujado.
   - **CUENTA todos los cambios de dirección del trazo**. Si el contorno tiene 8 esquinas, emite 8 vértices. Si tiene 10, emite 10. NO simplifiques saltándote escalones intermedios. Una U con un escalón adicional en un brazo NO es una U de 8 vértices, es un polígono de 10 o 12 vértices.
   - Si el operador hizo **varios trazos del mismo color que se tocan o se superponen** (por ejemplo, dibujó el contorno principal + retoques o detalles añadidos encima), todos forman parte del **mismo polígono unificado**. NO los emitas como piezas separadas — únelos lógicamente en UN solo `vertices_mm` que respete todos los detalles dibujados.
   - Si los trazos del mismo color están **claramente separados** (no se tocan, hay espacio en blanco entre ellos) → son piezas independientes (ej. encimera de pared + isla independiente).
   - Las medidas las sacas de las cotas del plano subyacente o de las cotas remarcadas/subrayadas por el operador. Si el operador dibujó un escalón, busca la cota que da su tamaño (típicamente cotas como 930, 850, 280, 545 sobre los detalles).

2. **TRAZO LINEAL (línea sin contorno cerrado)** — usado en `copete`, `zócalo/rodapié`, a veces frontal corto:
   - Una línea = una pieza lineal. El operador NO necesita dibujar el contorno porque son piezas tira (largo × altura conocida del material).
   - El **largo** = la cota que cubre la línea sobre el plano.
   - La **altura** sale del material (copete H=50mm; rodapié H=100mm; frontal H=600 con muebles altos / 1000 sin muebles altos).
   - Si hay varias líneas separadas del mismo color → varias piezas separadas (una por línea).

3. **CRUZ / PUNTO ROJO** → hueco (placa, fregadero, grifo, enchufe). El operador marca la posición; tú asignas el tamaño según el catálogo de huecos default.

**Cotas remarcadas / subrayadas**:
El operador suele subrayar o remarcar las cotas que quiere que uses. Si ves trazos sobre un número del plano → ESA es la medida autoritativa. Para cualquier pieza pintada, busca en el plano subyacente las cotas que la cubren y úsalas. Cálculos simples (sumas/restas de cotas adyacentes) son aceptables y necesarios; nada complejo.

**Paleta de colores (aplicar este mapeo de forma estricta)**:
| Color trazo | Tipo | Geometría | Altura default | Cómo emitirlo |
|-------------|------|-----------|----------------|---------------|
| Verde fluor / amarillo | encimera (incluye isla) | contorno cerrado | — (fondo del polígono) | Pieza con `vertices_mm` |
| Azul | frontal / chapeado | contorno cerrado | 600 con muebles altos, 1000 sin | Pieza con `largo_mm` + `altura_mm` |
| Lila / morado | zócalo / rodapié | línea | 100mm | Pieza con `longitud_ml` + `altura_mm` |
| Naranja | copete | línea | 50mm | Pieza con `longitud_ml` + `altura_mm` |
| Verde oscuro | costado / cascada | contorno cerrado | altura encimera-suelo (≈900) | Pieza con `vertices_mm` o `largo_mm`+`ancho_mm` |
| Marrón | pilar / pilastra | contorno cerrado | — | Pieza con `vertices_mm` |
| Rojo | huecos | cruces / puntos | — (catálogo) | Hueco con `centro_x_mm`/`centro_y_mm` |
| Cian / turquesa | **PULIDO** (canto pulido) | línea | — | Entrada en `cantos`: `{"tipo":"recto_pulido","longitud_ml":<largo_línea>,"notas":"<contexto>"}` |
| Magenta / rosa fucsia | **INGLETE** (unión) | línea | — | Entrada en `cantos`: `{"tipo":"ingletado","longitud_ml":<largo_línea>,"notas":"<piezas que une>"}` |

**REGLAS PARA TRAZOS DE CANTO (pulidos e ingletes)**:
- Cian/turquesa = canto pulido. Una línea cian sobre el contorno de una encimera o chapeado indica que ESE borde va pulido (canto recto pulido visible). El **largo de la línea** = `longitud_ml` del canto. NO genera pieza nueva, solo añade un objeto a `cantos`.
- Magenta/rosa = inglete. Aparece en uniones entre dos piezas perpendiculares (típico chapeado-pilar, encimera-cascada, dos chapeados en esquina). El **largo de la línea** = `longitud_ml` del inglete. Añade objeto a `cantos` con `tipo: "ingletado"` y describe en `notas` qué piezas une.
- Si el operador escribió un comentario en notas globales explicando el motivo del pulido/inglete, refléjalo en el campo `notas` de la entrada de `cantos`.
- Múltiples líneas separadas del mismo color → múltiples entradas separadas (no las sumes).

**COTAS ROTADAS 180° — ¡CUIDADO!**:
En planos cenitales el operador a veces escribe la cota orientada hacia el lado de la pieza (legible desde ese lado de la cocina). Esto significa que **una cota puede aparecer rotada 180°** y leerse al revés:
- `"009"` rotado 180° = `"600"` → fondo de encimera/módulo
- `"002"` rotado 180° = `"200"` → pequeña dimensión
- `"088"` rotado 180° = `"880"` → ancho de mueble
- `"05"` o `"50"` puede ser `"50"` o leerse `"05"` rotado

**Heurística**: si encuentras una cota inusualmente pequeña en un sitio donde esperarías el fondo (~600), el largo de un mueble (~600/900) o cualquier dimensión grande, sospecha que está rotada. Pruébala leída al revés y si encaja con el contexto, ÚSALA INVERTIDA. NUNCA tomes cotas como `009`/`002`/`088`/`010` literalmente sin verificar contexto.

**REGLA AGRESIVA (no aplicar la lectura literal cuando contradiga el contexto)**:
- Si en un mismo plano un extremo dice `600` (literal claro) y otro extremo dice `009` (con cero a la izquierda), AMBOS son la MISMA medida = 600. El primero está orientado en una dirección, el otro en la opuesta.
- Si una cota como `006`, `007`, `008`, `009` aparece junto a una pieza grande (encimera ≥1m, frontal, copete largo), siempre es la cota rotada. NO la leas literal jamás como 60/70/80/90mm.
- Si la cota tiene 2 dígitos como `06`, `09`, `07` aplicada a un fondo o cabeza, también está rotada y debes leer `60`/`90`/`70` cm = `600`/`900`/`700` mm.
- Cualquier cota con CERO INICIAL en planos cenitales = rotada. Inviértela siempre antes de procesarla.

**INGLETES IMPLÍCITOS — REGLA GEOMÉTRICA OBLIGATORIA (aunque el operador no los marque)**:
Cuando un mueble hace **esquina** y la pieza de piedra lleva el canto frontal del largo + uno o ambos laterales (cabezas) visibles, las uniones entre el frontal y las cabezas se ejecutan con INGLETE. Aplica también al rodapié, copete, frontal/chapeado y encimera con cabezas vistas.

Cómo detectarlo:
- Si una encimera tiene **cabeza vista** en uno o ambos extremos (no choca contra otro mueble pegado) y lleva canto pulido tanto en el largo frontal como en la cabeza → emite UN `cantos` `recto_pulido` por cada tramo + una entrada `ingletado` en cada esquina donde se unen.
- Si un rodapié corre por el largo frontal y sigue por la cabeza lateral del mismo mueble (forma de L o U), la unión esquina = inglete. Emite UNA entrada `cantos` `ingletado` por esquina (longitud_ml = altura del rodapié, típico 0.1).
- Si un copete o frontal envuelve la cabeza vista de la misma forma → mismo tratamiento.
- Cuando el mueble es muy estrecho (típico módulo final 1020 con cabeza), MUY frecuente: 1 frontal + 2 cabezas = 2 ingletes (uno por cabeza); si solo una cabeza es vista → 1 inglete.

Esto es una regla de FABRICACIÓN: aunque el operador no haya pintado el inglete con magenta, si la geometría implica una esquina con cantos visibles en ambas caras, **debes emitir el inglete por defecto**. Indícalo en `notas` con "inglete cabeza-frontal implícito por esquina vista".

**TRAZOS RECTOS (POLILÍNEA) vs A MANO ALZADA — IMPORTANTE**:
El operador puede dibujar de dos maneras:
- **A mano alzada**: trazo curvo, irregular. Usado para trazos rápidos, líneas (copete/zócalo) o blobs aproximados.
- **Polilínea (líneas rectas perfectas entre vértices)**: cuando ves un contorno con **lados rectos perfectos y esquinas marcadas/duras** (no curvas), es una polilínea trazada vértice a vértice por el operador. Estos vértices son **EXACTOS** — el operador los puso a propósito en cada esquina del polígono real.
  - Para encimeras/frontales/costados pintados con polilínea: emite UN `vertices_mm` con **exactamente esos vértices**, en el mismo orden que el contorno. Si la polilínea tiene 10 esquinas, emite 10 vértices. NO simplifiques.
  - El número de vértices del polígono = número de esquinas visibles del contorno recto.
  - Si la polilínea tiene un **escalón pequeño** o **muesca** intermedia, esos 2-4 vértices extra son obligatorios; respétalos siempre.
  - El operador escogió polilínea precisamente para evitar ambigüedades — confía en cada vértice.

**Si hay overlay, el orden de razonamiento es**:
1. Cuento blobs y líneas por color → tengo la lista exacta de piezas que debo emitir.
2. Para cada pieza, mido del plano subyacente las cotas que la cubren.
3. Si el operador escribió notas globales (texto suelto) → ajusto materiales/cantidades.
4. NO infiero piezas adicionales no anotadas, ni omito alguna pintada.

Si NO hay overlay, sigue las reglas de identificación visual de la sección siguiente.

## 🔍 IDENTIFICACIÓN VISUAL DE ELEMENTOS EN EL PLANO (sin overlay)

Antes de extraer cotas, IDENTIFICA cada elemento. El plano de cocina mezcla
elevación frontal y vista cenital. Reglas para identificar:

**MUEBLES ALTOS (colgados de pared)**:
- Rectángulos con una **X (aspas)** dentro.
- Aparecen en la **zona superior** del plano, alineados horizontalmente.
- En su tramo, va FRONTAL/CHAPEADO entre la encimera y la base del mueble alto
  (altura ≈ 60cm).

**MUEBLES BAJOS**:
- Rectángulo **sin X**, con la línea de encimera encima.
- En su zona va RODAPIÉ debajo (línea horizontal estrecha en la base).
- Si encima hay placa/fregadero/lavavajillas dibujados en planta → es seguro mueble bajo.

**ZONAS SIN MUEBLES ALTOS** (huecos altos, ventanas, campanas extractoras):
- Espacio vacío encima de la encimera (no hay rectángulos con X arriba).
- Ahí va FRONTAL más alto (≈ 100cm) — protege la pared sin muebles altos.
- Si la zona supera 1.5m de largo, partir el frontal en piezas según módulos
  de mueble bajo (60cm o 90cm).

**COLUMNAS / TORRES**:
- Rectángulos apilados que llegan **del suelo al techo**.
- Etiquetas: `FRI` (frigo), `LV` (lavavajillas), `HRN` (horno), `MO` (microondas),
  `COL`/`COLUMNA`. NO llevan encimera de piedra encima.

**COSTADO VISTO (cabeza vista)**:
- **Primer o último mueble** de un tramo, cuando NO tiene otro mueble pegado a su lado.
- Lo reconoces porque ese lateral no continúa con otro rectángulo.
- A esa cabeza le va: copete (largo = fondo del mueble bajo, típico 600mm × 50 alto)
  Y rodapié de cabeza (largo = fondo del mueble bajo × 100 alto).
- Si la encimera tiene ZONA ESTRECHA (28cm fondo) → la cabeza de esa zona
  lleva copete y rodapié de **largo 280** (= el fondo de esa zona, no 600).

**ZONAS DE ANOTACIÓN MANUAL**:
- Verde fluorescente o trazos amarillos del operador → encimera.
- Trazos azules → frontal/chapeado.
- Trazos lila → zócalo/rodapié.
- Trazos naranja → copete.
- Trazos verdes (oscuro) → costado/cascada.
- Trazos marrón → pilar.
- Trazos rojos → huecos.

**PAREDES (muros)**:
- Línea continua gruesa (a veces doble línea) en el perímetro del plano.
- Lo que el plano NO encierra es el espacio abierto (paso). Las cabezas vistas
  son los extremos abiertos, NO contra muros.

**APLICACIÓN PRÁCTICA — qué piezas emitir según lo que ves**:
1. Cuenta los rectángulos con X arriba → identifica largos de zona con muebles altos.
2. Identifica la zona sin muebles altos (entre los 2 grupos) → frontal alto ahí.
3. Identifica si el primer mueble bajo tiene cabeza vista (no hay pared a su izquierda)
   → copete + rodapié de cabeza izq.
4. Identifica si el último mueble tiene cabeza vista (no hay pared a su derecha)
   → copete + rodapié de cabeza der.
5. RODAPIÉ va debajo de TODOS los muebles bajos (todas las zonas continuas).
   Si los muebles altos llegan al suelo (columna, despensa) y debajo hay zócalo,
   incluye también ese tramo en el rodapié.

## 📐 REGLAS ADICIONALES — CASOS QUE SE OLVIDAN (LEER SIEMPRE)

**ENCIMERA UNIFICADA — el saliente/escalón ES PARTE de la misma encimera**:
Si una encimera tiene una zona principal (ej. 3870×600) + un saliente final más
estrecho (ej. 545×280) o una zona en L, eso es UN SOLO polígono, NO dos piezas.
Emite UN único objeto `encimera` con `vertices_mm` que recorra todo el contorno.
Ej: `[[0,0],[3870,0],[3870,280],[4415,280],[4415,600],[0,600]]` para una L donde
el saliente continúa al final con fondo 280mm.

**CHAPEADOS — REGLAS POR ZONA**:
- **Zona con muebles altos**: chapeado de altura ≈ 600mm (espacio entre encimera
  y muebles altos, típico). Largo = largo de la pared cubierta por muebles bajos.
- **Zona sin muebles altos** (ventana, campana, hueco): chapeado de altura ≈ 1000mm.
  Si esta zona supera 1.5m de largo, **partir en 2 piezas o más** según el largo
  del módulo de mueble debajo (típicamente 60cm o 90cm). Ej: 1150×1000 → 2 piezas
  de 550×1000 + 600×1000.
- **Pilar saliente**: 3 piezas pequeñas de chapeado:
  · Frente del pilar: ej. 400×600 (largo del frente del pilar × altura).
  · Lateral izquierdo del pilar: ej. 35×600 (espesor del material × altura).
  · Lateral derecho del pilar: ej. 35×600.
  Las 3 van unidas con inglete (añadir a `cantos`: `{"tipo":"ingletado","longitud_ml":...}`).
- **Frontal ALTO que parte la pared**: si un tramo de la pared lleva un frontal
  de altura especial (campana/columna, p.ej. 300×1500), el chapeado estándar de
  esa pared mide **total_pared − largo_del_alto**, NUNCA el total. Ejemplos
  reales: pared 2410 con alto de 300 → chapeado 2110 + alto 300×1500; cabeza
  600 con alto de 243 → chapeado 243×1500 + copete 357. Haz la RESTA con las
  cotas, no copies la longitud de la arista.
- **NO INVENTES chapeados**: un frontal solo existe si el plano lo dibuja, la
  plantilla lo lista o una nota lo indica. Una península/isla NO lleva chapeado
  salvo indicación explícita (sus traseras suelen ser vistas o de mueble).

**RODAPIÉS / ZÓCALOS — TODAS LAS ZONAS CON MUEBLES BAJOS**:
Cualquier zona que tenga muebles bajos (sea tramo de encimera, isla, esquina,
debajo de muebles altos sin encimera) lleva rodapié. Detectar TODAS las zonas:
- Rodapié del tramo principal de encimera (debajo).
- Rodapié de zonas estrechas/L (ej: 545mm, esquina interior 320mm si la L tiene
  una pared interna).
- Rodapié de los muebles altos en zona sin encimera (cuando hay sólo muebles
  altos sin encimera bajo, su pared lleva rodapié del mueble bajo si lo hay
  debajo, o si los altos llegan al suelo lleva rodapié de cabezas).
- Rodapié de cabezas (lados visibles de muebles bajos / encimera): largo = fondo
  del mueble bajo (típico 600mm) × altura zócalo (100mm).
- Si el plano marca "ZOCALO 3318ml" pero el largo de encimera es solo 3870mm,
  los 3318 indican zócalo CONTINUO de varios tramos. Listar cada tramo distinto.

**COPETES — REGLA ESTRICTA**:
- Una zona con frontal (chapeado) NO lleva copete. NUNCA emites ambos para la
  misma zona física.
- Solo cabezas vistas llevan copete (largo = fondo de la encimera en ese punto:
  600mm si zona normal, 280mm si zona estrecha).
- Si la cocina tiene 2 cabezas vistas (extremos abiertos), hay 2 copetes.

## 🗺 REPRESENTACIÓN GEOMÉTRICA 2D — DISEÑA CADA PIEZA COMO UN POLÍGONO REAL

Tu objetivo principal es **diseñar cada pieza en 2D** como si la dibujaras directamente
en un programa CAD. Esto significa:

1. **Cada encimera es UN POLÍGONO**, no varios rectángulos sueltos. Si la encimera es
   en L, U, o tiene una entrada de pilar, emite UN solo objeto `pieza` con su lista
   completa de vértices `vertices_mm`.

2. **CONVENCIÓN DE EJES (CRÍTICA — IMPORTANTE)**:
   - X: a lo largo de la encimera (de izquierda a derecha cuando el usuario la mira de frente).
   - Y=0: FRENTE de la encimera (el borde donde el usuario se acerca, donde está el canto pulido visible).
   - Y=ancho_mm: PARED trasera (donde la encimera toca la pared / fondo).
   - Por tanto, los **pilares y entradas a la pared están en Y ALTO (trasero)**, NO en Y bajo (frente).
   - Vértices empiezan en esquina FRONTAL-IZQUIERDA `[0,0]` y van CCW.

3. **Formas típicas**:
   - **Rectangular**: 4 vértices. `[[0,0],[L,0],[L,A],[0,A]]`
   - **Encimera con MUESCA (entrada) de pilar atrás-derecha** (caso MUY común — el pilar
     sobresale de la pared trasera ENTRA en la zona de la encimera, y la encimera
     se corta para dejar paso al pilar).
     Ej: encimera principal 2700×600 con muesca de pilar 300×200 en esquina trasera-derecha:
     `[[0,0],[2700,0],[2700,400],[2400,400],[2400,600],[0,600]]`
     - Bbox: 2700×600 (no se añade material extra; el pilar QUITA material).
     - La muesca está en X=2400→2700, Y=400→600 (esquina TRASERA-DERECHA, FUERA del polígono).
     - ⚠ NO uses saliente exterior (X > 2700). El pilar reduce la pieza, no la agranda.
   - **L (esquina trasera derecha más estrecha)**: ej. tramo principal 3000×600
     + zona final 545mm con fondo reducido a 280mm:
     `[[0,0],[3000,0],[3000,600],[2455,600],[2455,320],[0,320]]` — la zona estrecha
     queda atrás-abajo si el armario reducido está al final.
   - **U**: 8+ vértices. Tres tramos perpendiculares.

4. **Bounding box**: SIEMPRE rellena `largo_mm` y `ancho_mm` con bbox del polígono
   (max-x − min-x, max-y − min-y). Para nesting se usa este bbox.

5. **Huecos con coordenadas y orientación REALES**:
   - `centro_x_mm` y `centro_y_mm`: posición del centro del hueco relativa a (0,0)
     de la pieza.
   - `largo_mm`: dimensión del hueco a lo largo del EJE X (paralelo al frente).
   - `ancho_mm`: dimensión del hueco a lo largo del EJE Y (frente-fondo).

   **TAMAÑOS estándar (encimera fondo 600-620mm)** — usar SIEMPRE estos defaults
   cuando la nota/plantilla no especifique modelo concreto:
   - **Placa inducción 60cm (Bosch/Balay)**: `largo_mm: 562, ancho_mm: 492`.
   - **Placa inducción dominó**: `270×490`.
   - **Fregadero 1 seno (default Teka)**: `largo_mm: 500, ancho_mm: 400`.
   - **Fregadero 2 senos**: `780×440`.
   - **Fregadero con escurridor**: `1000×440`.
   - **Grifo**: `35×35` (taladro).

   **POSICIONAMIENTO Y NO SOLAPAMIENTO (CRÍTICO)**:
   - Origen (0,0) en esquina FRONTAL-IZQUIERDA. Y crece hacia la pared trasera.
   - **Margen al frente** (Y=0): ≥40mm (canto pulido visible).
   - **Margen a la pared** (Y=ancho): ≥10mm.
   - **Placa**: centro_y ≈ ancho_encimera/2 (centrada en fondo). Centro_x según módulo del mueble.
   - **Fregadero**: centro_y ≈ 290 (frente a 50, atrás a 510 → deja 90mm para grifo).
     Centro_x hacia el extremo opuesto a la placa (1 seno) o central (2 senos).
   - **Grifo**: SIEMPRE detrás del fregadero, NUNCA solapado.
     `centro_x` = mismo que fregadero (o pequeño offset lateral).
     `centro_y` = ancho_encimera − 35 (para 600mm → centro_y=565; para 620 → 585).
     Verifica que `centro_y_grifo > centro_y_fregadero + ancho_fregadero/2 + 10`.
   - **Enchufes**: van en el frontal/chapeado, no en la encimera. Si sí van en
     encimera (raro), centro_y cerca de la pared (Y = ancho-50), separados ≥150mm
     entre sí.

6. **CHAPEADOS EN ESQUINA DE PILAR**: cuando hay un pilar que sobresale trasero,
   el chapeado/frontal se compone de **2 piezas perpendiculares unidas con INGLETE**:
   - Una pieza lateral (paralela al pilar): ej. 200×600 (200mm a lo largo, 600mm alto).
   - Una pieza frontal (cara visible del pilar): ej. 300×600 (300mm a lo largo, 600mm alto).
   - Emite ambas como piezas frontal separadas + entrada en `cantos`:
     `{"tipo": "ingletado", "longitud_ml": 0.6, "notas": "unión pilar"}`.
   - NO unifiques en una sola pieza grande.

7. **Todas las cotas visibles del plano**: extrae cada medida que veas. Si el plano
   marca "2400" para el frontal y "2700" para la encimera, ambas distintas; ambas van.

8. **NO simplifiques a rectángulos** cuando hay forma irregular. Si pintaste pilar
   marrón o el plano muestra notch, los vértices DEBEN reflejar la muesca.

## ⚠ FUENTES DE INFORMACIÓN — IMPORTANTE LEER ANTES DE EMPEZAR

**Lo que SÍ recibes** (toda la información sale SOLO de estas fuentes):
1. **Plano de planta 2D** (vista aérea de la cocina con cotas reales).
2. **Render/perspectiva 3D** (referencia visual; cotas orientativas).
3. **Plantilla del operador** (formulario texto con campos: material, marca, color, grosor, acabado, hueco counts, frontal/copete/zócalo SI/NO, observaciones).
4. **Anotaciones manuales del operador** (rotulador de colores sobre el plano + notas globales — autoritativas para forma).
5. **Notas TXT** (cualquier indicación adicional escrita).

**Lo que NO recibes (filtrado del input)**:
- **Presupuesto del marmolista** (PDFs `PR*` o `F*` y Excels con hojas "Presupuesto"). NO están disponibles en este flujo.
- Ya **no debes** apelar al "MGR" como fuente. Toda la forma, las cotas y la cantidad de piezas salen de plano + plantilla + anotaciones.

**Consecuencia importante**:
- Si tu razonamiento te lleva a inferir "el MGR dice X cm" o "el bounding del MGR es Y" → **stop**: no tienes esa información. Trabaja solo con el plano, lo que ves, y lo que el operador marcó.
- Cuando este prompt menciona "MGR" más adelante, ignóralo — son referencias legacy. Las únicas fuentes válidas son las cinco enumeradas arriba.

## Tipos de documentos que puedes encontrar:
1. **Plantilla presupuesto marmolista**: Formulario con datos del cliente, material (marca, color, grosor, acabado), tipo de copete, frontal, zócalo, fregadero, elaboraciones y observaciones.
2. **Plano de planta 2D**: Vista desde arriba de la cocina con medidas en mm y etiquetas de tipo de pieza (ENCIMERA, FRONTAL H:XXcm, COPETE H:Xcm, ZOCALO H:Xcm...). Las X sobre muebles indican electrodomésticos sin piedra.
3. **Render/perspectiva 3D**: Imagen visual de la cocina montada. Ayuda a entender disposición, pilares, islas, cascadas, y confirmar dónde van los distintos materiales y piezas.
4. **TXT extra**: Notas adicionales del cliente o comentarios.

## TIPOS DE PIEZAS — DEFINICIONES PRECISAS:

### Encimera
Superficie horizontal sobre los muebles bajos. Profundidad estándar 600mm salvo indicación contraria.
Puede tener forma rectangular, en L, en U, con entrantes por electrodomésticos (marcados con X en plano).
También puede ser isla o península independiente.

**🔺 ENCIMERAS NO RECTANGULARES — UN POLÍGONO ÚNICO (CRÍTICO)**:
Una encimera en L, U o con escalón es UN POLÍGONO ÚNICO. Emite UN solo objeto `pieza` con `vertices_mm` que recorra todo el contorno. El programa de nesting decompone luego en sub-rectángulos óptimos para cortar; tú no tienes que hacer ese reparto. Tu trabajo es conservar la forma real con todos sus vértices.

**🧱 CONTACTO DE CADA ARISTA — `aristas_contacto` (OBLIGATORIO en toda pieza con `vertices_mm`)**:
Junto a `vertices_mm` emite SIEMPRE `aristas_contacto`: una lista con UNA entrada por arista, alineada con los vértices (la entrada i describe la arista que va del vértice i al i+1; la última cierra contra el vértice 0). Valores permitidos:
- `"pared"` → la arista va pegada a un muro del edificio (en el plano: líneas dobles, gruesas o sombreadas que delimitan la estancia)
- `"mueble"` → pegada a columna de muebles, frigorífico panelado, pilar u otro obstáculo
- `"vista"` → abierta a la cocina (esa arista se pulirá)
- `"ventana"` → bajo ventana (se pule igual que vista)

Cómo decidirlo MIRANDO EL PLANO (no lo inventes por geometría):
- Sigue el muro dibujado: una arista de encimera ADOSADA al muro = `"pared"`. Una arista que da al espacio libre de la cocina = `"vista"`.
- En una **PENÍNSULA o ISLA** la mayoría de aristas son `"vista"`: solo el tramo que conecta con el muro o la esquina es `"pared"`. NO asumas que el lado más largo es pared — en penínsulas suele ser justo al revés.
- En una encimera rectangular contra pared: trasera = `"pared"`, frente = `"vista"`, y cada cabeza según tenga muro/mueble al lado o quede abierta.
- COHERENCIA con el resto de tu salida: cada arista `"pared"`/`"mueble"` sin frontal (chapeado) lleva copete; cada arista `"vista"`/`"ventana"` va pulida. Tus piezas de copete/frontal y tus cantos deben cuadrar con esta lista — si no cuadran, revisa la lista o las piezas antes de responder.

**📐 COTA DE CADA ARISTA — `aristas_cota` (OBLIGATORIO junto a aristas_contacto)**:
Junto a `aristas_contacto` emite `aristas_cota`: una lista alineada con los vértices donde la entrada i es la COTA EN MM que el plano da para la arista i (número), o `null` si el plano NO acota esa arista. **Una pieza con `vertices_mm` y sin `aristas_cota` es una respuesta INVÁLIDA** — el programa la necesita para corregir croquis no-a-escala. Reglas:
- Solo cotas LEÍDAS del plano. NUNCA pongas un valor derivado por aritmética — para eso está `null` (el programa deriva por cierre).
- Los croquis NO están a escala: un brazo dibujado "ancho" puede medir 300mm según su cota. Las COTAS mandan sobre las proporciones del dibujo, siempre.
- CONSTRUYE los vértices por ARITMÉTICA de cotas, no copiando posiciones del dibujo: el quiebre de una L está en (largo_total − fondo_del_brazo), el alto total es la cota total si el plano da el total (no sumes si la cota YA es total).
- VERIFICA EL CIERRE antes de responder: en un polígono ortogonal, la suma de aristas hacia la derecha = suma hacia la izquierda, y subidas = bajadas. Si tus vértices no cierran con tus `aristas_cota`, los vértices están mal — reconstrúyelos.
- Una arista de encimera de menos de 150mm es casi seguro un error de construcción (revisa antes de emitir).

**📏 ESCALONES Y ENTRADAS — qué cota va en cada segmento (CRÍTICO)**:
Cuando una encimera tiene un escalón (fondo que cambia, p.ej. de 900 a 300), identifica QUÉ cota pertenece a CADA segmento siguiendo las líneas de cota del plano (flechas/extremos). NO deduzcas la asignación por eliminación ni porque "la diferencia coincide con otra etiqueta": las dos asignaciones posibles producen los mismos números y solo el dibujo dice cuál es la real. Comprueba contra el dibujo: ¿el extremo es más estrecho o más ancho que el cuerpo? Declara en `notas` qué viste.

**🏛 PILARES Y COLUMNAS — IMPORTANTE para piezas pequeñas**:
Cuando hay un **pilar/columna** en la pared, la encimera lo rodea por sus caras vistas. Esto genera piezas adicionales pequeñas:
- **Si el pilar SOBRESALE de la pared hacia la cocina**, la encimera tiene 2 tramos (uno antes del pilar, otro después). Cada uno es 1 pieza.
- **Si el pilar es exento (en el medio del espacio) y la encimera lo rodea**, hay 4 tramos: 2 a los lados (largos) + 2 frontales del pilar (cortos, ~150-300mm). Cada uno = pieza.
- **Si la pared tiene un saliente/entrada de pilar**, la encimera tiene un escalón: 2 tramos rectos + 1 corto que cierra el escalón (~150-300mm × fondo).
- Los **lados del pilar** suelen ser piezas estrechas: típicamente 150-300mm de largo × 600mm de fondo.
- Los **frentes del pilar** suelen ser piezas: típicamente 600mm de largo × 150-300mm de fondo.

NO sumes los tramos del pilar en la encimera principal. Cada cara visible del pilar que lleve encimera = 1 pieza separada con `zona`: "lateral pilar izq", "lateral pilar dch", "frente pilar", etc.

**📐 EJEMPLO REAL — CÓMO INTERPRETAR ENCIMERA EN L CON ZONA FINAL ESTRECHA**:

Caso real I007 (Dekton Avorio 1.2cm):
- **MGR**: `Encimera 4.415 × 0.620 = 2.737m²` (bounding rectangle facturado)
- **Plano de planta** muestra cocina en L con tramo principal y zona final con cota transversal "280" + cota longitudinal "545"
- **Realidad**: 2 tramos:
  - Tramo principal largo: 3870 × 620 mm
  - Tramo final estrecho: 545 × 280 mm
- **Verificación**: 3870 + 545 = 4415mm = MGR Longo ✓ (la merma de cortar la L explica la diferencia de área 2.737 - 2.552 = 0.185 m²)

**Patrón general**:
- Cuando ves "280", "300", "350", "400" como cota TRANSVERSAL en un extremo del plano → es un fondo reducido en esa zona.
- El MGR Longo total = SUMA de largos de los tramos (no del bounding individual).
- El MGR Ancho = ancho del tramo más ancho (típicamente 620), NO el ancho de los tramos estrechos.
- Emite cada tramo como pieza separada con su `(largo, ancho)` real.

**🔻 ZONAS ESTRECHAS / ESCALONES / ENTRADAS DE ARMARIO — DETECCIÓN OBLIGATORIA**:

**Regla**: cada vez que el FONDO de la encimera cambia (de 620 a 280, de 620 a 400, etc), eso es un tramo nuevo. Cada combinación distinta de (fondo, largo) = pieza separada.

**Cómo detectarlo en el plano**:
- Si en el plano ves cotas como "280", "300", "350", "400", "450" cerca del borde de la encimera (especialmente en extremos o esquinas), son **fondos reducidos**. NO son orientativos. Son medidas reales del armario.
- Si ves dos cotas juntas como "620" y "280" en zonas distintas del mismo tramo → la encimera tiene 2 sub-tramos con fondos distintos.
- Cualquier cota menor que el fondo estándar 620mm que aparezca **transversalmente** (perpendicular al largo) en el plano = fondo reducido.

**Patrones típicos**:
- Encimera larga 4000mm con armario al final más estrecho: emite pieza1 (3500×620) + pieza2 (500×280).
- Cocina en L con esquina de armario reducido: encimera_principal + encimera_esquina con fondo distinto.
- Barra/península con vuelo: la barra suele tener fondo mayor (700-900mm) que la encimera estándar.

**Nunca asumas fondo uniforme 620mm para toda la encimera** si el plano muestra cotas distintas. SUMA de todos los tramos × fondo correspondiente debe aproximar el m² del MGR.

**📏 COMO INTERPRETAR EL MGR — ES EL RECTÁNGULO DE MATERIAL CONSUMIDO, NO LA FORMA REAL**:

El MGR `Encimera 4.415 × 0.620` representa **el rectángulo de material que se consume / se cobra**, incluyendo las zonas que se cortan y tiran (el hueco de una L, la esquina de una U, etc). Ejemplo: para hacer una L de 3460×620 + 955×280 se parte de un rectángulo 4415×620 y se tira la parte que no se usa — pero se cobra el rectángulo entero.

**Consecuencia clave**: el MGR NO describe la forma real. **El plano/plantilla es la ÚNICA fuente de forma.**

**FUENTE DE VERDAD PARA FORMA = PLANO (planta 2D)**:
- El **plano de planta 2D** (vista aérea, programa 2020) muestra las encimeras como polígonos reales con medidas por pared.
- La **plantilla manuscrita** muestra los tramos con cotas anotadas a mano por pared/segmento.
- Ambos superan al MGR en descripción de forma.

**OBLIGACIÓN al procesar el plano**:
1. Mira el plano con atención. Cuenta cuántas paredes/tramos tiene la encimera.
2. Si la encimera rodea una esquina (L), si recorre 3 paredes (U), si tiene zona estrecha al final (entrada de armario menor fondo), si tiene entrante por un pilar → son FORMAS NO RECTANGULARES y requieren MÚLTIPLES piezas.
3. **Cada tramo recto** del polígono real = 1 pieza. Ej: L → 2 piezas. U → 3 piezas. Rectángulo con final estrecho → 2 piezas.
4. Las medidas en el plano son DEFINITIVAS. Si aparecen "3460" y "600" en paredes perpendiculares, son largo del tramo 1 y largo del tramo 2 — no son orientativas.
5. El ancho de cada tramo también se lee del plano. Habitual 620mm, pero puede variar (ej: esquina a 280mm, zona estrecha final a 280mm).

**Validación con MGR (pero NUNCA anular la forma del plano)**:
- El área total de tus tramos debe ser ≤ área MGR (porque MGR incluye merma).
- Puedes anotar en advertencias la diferencia como "cut-off de la L" o "material cortado en entrante".

**Cuando emites 1 sola pieza**:
- Solo si el plano muestra inequívocamente UNA pared rectangular única.
- O es isla/península exenta rectangular.

**REGLA IMPERATIVA**: ante duda entre "1 rectángulo" vs "2+ tramos", **SIEMPRE prefiere multi-tramo**. Es peor perder información de forma que sobrar.

**🎯 PROPÓSITO DEL OUTPUT — IMPORTANTE para tomar decisiones**:
Tu output NO se usa para hacer presupuestos (los presupuestos ya están hechos por el MGR humano). Tu output se usa para **NESTING**: calcular cuántas tablas hay que comprar y cómo se reparten las piezas. Para que el nesting funcione necesitamos las piezas REALES, no el rectángulo bounding del MGR.

**REGLA DE COHERENCIA INTERNA — VERIFICACIÓN FINAL OBLIGATORIA**:
Antes de emitir el JSON final, RE-LEE tus propias notas y advertencias. Si en cualquier punto dices "la forma es en L/U", "el plano muestra forma en L", "según plano hay península/barra", "hay tramos en paredes perpendiculares", "multi-tramo recomendado", "forma no rectangular", "TRASETA"/"TRASENA"/anotaciones de tramos, o cualquier otra observación que reconozca forma NO rectangular → **OBLIGATORIO** emitir multi-tramo.

**NO es válido**:
- ❌ Notas: "es L según plano" + emitir 1 rectángulo
- ❌ Notas: "Geometría exacta de tramos no confirmada sin plano CAD preciso" + emitir 1 rectángulo
- ❌ Notas: "Idealmente debería emitirse como N tramos separados pero las cotas exactas..." + emitir 1 rectángulo
- ❌ Notas: "Sin cotas explícitas de cada brazo en el plano, se emite como pieza única" + emitir 1 rectángulo

**SÍ es válido**:
- ✓ Notas: "forma en L. Tramo largo y corto estimados proporcionalmente del dibujo (60%/40% del MGR total)" + emitir 2 piezas
- ✓ Notas: "tramo norte y oeste leídos del plano: 287cm + 106cm" + emitir 2 piezas

**Si no tienes cotas exactas**, ESTIMA por proporción del dibujo. Es preferible 2 tramos estimados (60/40 o 50/50) que 1 rectángulo monolítico que rompe el nesting. NUNCA escribas "ideal sería multi-tramo pero emito 1" — eso es inválido.

Cuando emites multi-tramo y no hay cotas claras de cada tramo, usa estas reglas para estimar:
- En L: si MGR dice "Longo 4415×0.620" y ves L en el plano, divide 4415 en 2 tramos: estima el largo de cada brazo proporcionalmente al dibujo (ej: 60% pared larga + 40% pared corta = 2650 + 1765).
- En U: 3 tramos. Estima el del medio según dibujo (suele ser el más corto, que es la "panza" del U).
- Anota `notas` que las medidas son ESTIMADAS por proporción del plano cuando no hay cota explícita.

Casos típicos:
- **Encimera en L**: 2 piezas, una por cada brazo. Ej: brazo largo 2500×620 + brazo corto 1400×620. Usa `zona` para distinguir ("tramo pared norte", "tramo pared oeste").
- **Encimera en U**: 3 piezas, una por cada tramo.
- **Entrante por pilar/columna**: si la pared tiene un pilar que hace que la encimera tenga un tramo más estrecho (ej: 280mm de fondo en vez de 620mm), emite ese tramo como pieza separada con el ancho real.
- **Zona final más estrecha** (típica en esquinas con armarios de menor fondo): pieza separada con el ancho real (ej: final a 280mm).

Cuando SÍ es una sola pieza:
- Encimera puramente rectangular en una sola pared.
- Isla/península exenta (una pieza única con su largo×ancho).

**Verificación de áreas**: la suma de áreas de los tramos debe aproximarse al m² total del MGR (tolerancia ±5%). Si no cuadra, revisa las medidas. Si el MGR tiene 2.5m² y tus tramos suman 3.1m², has medido algo mal o hay solape.

**Pistas en el plano**:
- Medidas manuscritas en los extremos de cada pared (ej: "2.50", "1.90") indican tramos.
- Cambios de fondo marcados (ej: "620" en tramo largo, "280" en tramo esquina).
- Anotaciones como "L", "U", "pilar", "entrada de pilar", "armario corto".

### Frontal (= Chapeado = Chapeado Pared)
**IMPORTANTE**: "Frontal", "chapeado" y "chapeado pared" son EXACTAMENTE el mismo tipo de pieza.
Es el panel vertical pegado a la pared detrás de la encimera, entre la encimera y los muebles altos.
- Se mide en m² (largo total × altura).
- La altura es la indicada en el plano (típicamente 58-60cm).
- **Altura variable**: Aunque lo habitual es 58-60cm, en algunas zonas el frontal puede ser más alto (zonas sin muebles altos, campana extractora, hueco de nevera empotrada). Si el plano o presupuesto indica una altura diferente para algún tramo, créalos como piezas separadas con su altura correcta. No asumas altura mayor por defecto — solo si está indicado explícitamente.
- **Segmentos con la misma altura = una sola pieza**: Si el presupuesto MGR agrupa varios segmentos de frontal en una única línea con dimensiones `Longo × Ancho` (ej: 2.13 × 0.70), es porque todos esos segmentos tienen la misma altura y se tratan como **una sola pieza**. No los descompongas en sub-segmentos — usa las dimensiones del presupuesto directamente. Solo crea piezas separadas cuando hay **alturas distintas**.
- **Cuando hay pilares o columnas en la pared**, el frontal rodea el pilar por sus caras visibles.
  La longitud total es la SUMA de todos los segmentos incluyendo los lados del pilar.
  Ejemplo: 2.18 + 0.195 (lado izq pilar) + 0.36 + 0.15 (lado dcho pilar) + 0.33 + 0.34 (fondo pilar) + 0.99 = 4.545m total.
- Cada segmento rectangular es una pieza individual a cortar de la tabla.
- Puede ser "NO" si el cliente no quiere frontal.

### Copete
Franja estrecha pegada a la pared encima de la encimera (parte superior visible). **Altura por defecto: 5cm** (siempre, salvo indicación contraria).
Si la plantilla indica un valor como "1,2" en el campo copete, es el **espesor del material** (1.2cm), no la altura. La altura sigue siendo 5cm por defecto.
También rodea pilares igual que el frontal pero en su parte superior.
Se mide en ml (metros lineales) o en piezas individuales.

**REGLA IMPORTANTE — Copete vs Frontal por altura:**
- Copete **≤ 9cm**: se trata como copete (franja estrecha).
- Copete **> 9cm** (ej: 10cm, 15cm, "hasta la ventana"): aunque el cliente o la plantilla lo llame "copete", **se trata y presupuesta como chapeado/frontal**. Crear la pieza como tipo `frontal` con su altura real. Esto ocurre cuando se quiere llegar a la altura de una ventana o proteger una pared de manchas.

**⚠ COPETE Y CHAPEADO/FRONTAL SON MUTUAMENTE EXCLUYENTES — REGLA INVIOLABLE**:
Una zona física (pared norte, frente pilar, lateral pilar, etc.) lleva **O copete O frontal,
NUNCA LOS DOS A LA VEZ**. NO HAY EXCEPCIONES.

Algoritmo obligatorio para evitar duplicados:
1. Lista todas las zonas que llevan frontal (donde la plantilla marca FRONTAL=SI o el plano
   muestra FRONTAL H:XX).
2. Lista todas las zonas que llevan copete (donde la plantilla marca COPETE=SI).
3. Si una zona aparece en AMBAS listas → **decide UNA sola**:
   - Por defecto, gana el frontal (porque cubre más altura y tapa el copete).
   - Solo si el plano explícitamente muestra "COPETE H:5cm" en una zona Y NO muestra
     frontal ahí, entonces va copete.
4. Emite UNA sola pieza por zona — frontal O copete, no ambas.

**Ejemplo de I008 (caso real)**:
- Plano: FRONTAL H:600 cubre 2400mm de pared norte, lateral pilar 200×600, frente pilar 300×600.
- Plantilla dice también COPETE 5cm.
- DECISIÓN: emitir SOLO los 3 frontales (norte 2400, lateral pilar 200, frente pilar 300).
  NO emitir copete en esas zonas porque YA están cubiertas por frontal.
- Si hubiera una pared adicional sin muebles altos donde no va frontal, ESA llevaría copete.
- En este caso, no hay tal pared → **0 piezas de copete**.

**⚠ LARGO DE COPETES Y CHAPEADOS — sigue la PARED, NO el pilar**:
- El **copete sigue el largo de la PARED** (tramo recto donde se apoya).
- Si la pared mide 2400mm con un saliente de pilar de 300mm al final, el copete del
  tramo recto es 2400mm de largo (NO 300mm del pilar). El pilar tendrá su copete propio
  de 300mm en su frente más quizá 200mm en su lateral.
- Lee siempre del plano la cota de la PARED para el largo del copete, no la cota del pilar.

**🔹 COPETE DE CABEZA VISTA (extremos abiertos de encimera)**:
Cuando un extremo de la encimera es CABEZA VISTA (no termina contra pared), lleva un
copete vertical de **largo = fondo de encimera** (no la cota del extremo del armario):
- Encimera 2700×600 con cabeza vista en extremo izquierdo → copete cabeza izq = **600mm × 50** (ml=0.6).
- Encimera con muesca de pilar a la derecha (Y=400→600): la cabeza vista derecha es la
  pared del corte (X=2700, Y=0→400), cabeza derecha = **400mm × 50** (ml=0.4).
- Cabeza pegada a pared (oculta) → SIN copete.

**Regla práctica**:
- Largo del copete de cabeza = profundidad libre de la encimera en ese punto.
- NUNCA inventes longitudes sueltas como "310mm" u otras que no correspondan ni a
  fondo ni a largo de pared.

**IMPORTANTE — FRONTAL/COPETE = NO**: Si la plantilla indica FRONTAL = NO o GROSOR = NO,
NO incluyas material ni pieza de tipo "frontal" o "copete". Simplemente no aparecen en el JSON.

### Zócalo / Rodapié
Franja al pie de los muebles bajos. **Altura por defecto: 10cm** (usar siempre si la plantilla no indica otra altura).
**REGLA**: El zócalo va debajo de TODOS los muebles bajos EXCEPTO debajo de la nevera/frigorífico.
Usar los renders 3D para confirmar qué zonas tienen muebles bajos.
Se mide en ml total o en piezas individuales.

### Pilastra
Revestimiento de los cantos/aristas de un pilar (las esquinas, no las caras planas que son "frontal").
Se mide en ml.

### Pilastras de cocina de hierro (específico Galicia)
En Galicia es muy habitual que las cocinas tengan una **cocina de hierro** (cocina económica/de leña) empotrada en la pared. A ambos lados de esta cocina de hierro se colocan **pilastras de piedra** — piezas rectangulares verticales que flanquean el hueco. En los planos aparecen "pintadas/marcadas con boli" en los laterales de la cocina de hierro. Se clasifican como tipo `pilastra` con sus dimensiones (largo × alto). Suelen aparecer en pares (izquierda y derecha).

### Isla / Península
Encimera independiente del resto de la cocina. Se identifica en plano como un bloque central separado.
Tiene su propio zócalo, costados y frontales según la configuración.

### Costado (= Cascada = Waterfall = Pata)
Panel vertical lateral que cae desde la encimera hasta el suelo. Aparece en islas y penínsulas como "pata" de apoyo.
- Mismo ancho/profundidad que la encimera a la que pertenece.
- Altura estándar del mueble bajo: 900mm (~90cm).
- Se une a la encimera con un inglete (se cobra como "ML INGLETADO" = `ancho_encimera × 2`).
- En el plano puede aparecer anotado como "Costado Ingletado X.XX × Y.YY" o como una flecha/pieza resaltada.
- **Dimensiones de la pieza: fondo_encimera × altura_caída** (ej: si la encimera tiene 620mm de fondo y la caída es 900mm → pieza = 620×900mm).
- El largo del costado SIEMPRE coincide con el largo de la encimera a la que pertenece (si isla mide 1400mm, el costado mide 1400mm de largo).
- **Para cálculo de material**: la cascada se suma al largo de la encimera (misma tabla). Ej: isla 2620mm + pata 920mm = 3540mm total a extraer de la tabla.

### Zócalo / Rodapié — REGLAS DETALLADAS:
**REGLA FUNDAMENTAL**: El zócalo va ÚNICAMENTE entre el suelo y la parte frontal de los **muebles bajos** (módulos de suelo con puertas/cajones). NO se coloca en muebles altos ni en columnas que llegan al techo.
**NO lleva zócalo de piedra**:
  - Debajo de nevera/frigorífico (siempre excluida)
  - Debajo de lavadora o secadora
  - En la cara trasera de una isla/barra donde se sientan banquetas/taburetes (esa cara lleva costado de madera)
  - En los extremos cortos ("cabezas") de una isla, salvo que se especifique
  - Zonas sin muebles bajos (pasos, puertas, columnas de horno)
  - Muebles altos de pared (solo están a altura de encimera o arriba, no tocan el suelo)
**SÍ lleva zócalo**:
  - Bajo todos los módulos de muebles bajos con frente visible (frente a cajones, puertas)
  - En islas: normalmente solo los 2 lados largos (si hay muebles por ambos lados)
  - Confirmarlo siempre con el render 3D

**USAR SIEMPRE el render 3D para confirmar**: qué lados de la isla tienen muebles, dónde está la lavadora, dónde van los taburetes.

## ELABORACIONES Y TRATAMIENTOS:

**Huecos** (se cobran por unidad):
- placa: hueco para placa de cocina/inducción
- fregadero: hueco para fregadero (subtipo: bajo_encimera / sobre_encimera / enrasado_optico)
- grifo: hueco pequeño para grifo
- enchufe: hueco rectangular para enchufe
- dosificador: hueco pequeño para dosificador de jabón

**📐 POSICIÓN Y TAMAÑO DE HUECOS — CAMPOS OBLIGATORIOS para placa y fregadero (para nesting)**:

Para CADA hueco de **placa** y **fregadero** (también recomendable para grifo), **SIEMPRE emite** los cuatro campos:
- `pieza_zona`: la `zona` de la pieza (encimera/isla) a la que pertenece el hueco. Debe coincidir EXACTAMENTE con el campo `zona` de una encimera emitida en `piezas`. Ej: si la encimera tiene `zona: "pared principal"`, el hueco emite `pieza_zona: "pared principal"`. Si solo hay una encimera, pon su zona (o "encimera principal" como default).
- `distancia_lado_mm`: distancia desde el BORDE IZQUIERDO de la encimera al CENTRO del hueco (mm). Se usará para decidir por dónde partir la encimera en el nesting.
- `largo_mm`: dimensión del hueco a lo largo de la encimera (mm).
- `ancho_mm`: dimensión del hueco en profundidad desde el frente (mm).

**Cómo determinar estos valores** (en orden de prioridad):
1. **Si la plantilla/plano muestra la medida exacta** (anotación manuscrita con cotas) → usar esa medida.
2. **Si la plantilla muestra la posición gráficamente pero sin cota** → estimar proporcionalmente a la medida total de la encimera. **NUNCA "centres" el hueco por defecto**: mide en el dibujo la distancia relativa del hueco a cada extremo (p.ej. "el centro de la placa está a ~1/3 del extremo izquierdo") y traduce ESA proporción a mm. Declara la proporción observada en `notas`.
2b. **PROFUNDIDAD por defecto (regla de taller)**: si no hay cota de profundidad, el borde delantero del hueco (placa/fregadero) queda a **80mm del FRENTE** de la encimera (la arista vista/pulida). Calcula la coordenada perpendicular en consecuencia.
3. **Si la plantilla NO da ninguna pista** → estimar:
   - **Posición**: cada hueco va **centrado en el módulo de mueble** que lo aloja (típicamente 60cm o 90cm de ancho). Si conoces la posición del módulo en la encimera, usa el centro de ese módulo. Si no, asume:
     - placa: cerca del centro (40-60% del largo de la encimera)
     - fregadero: hacia un extremo (20-30% desde un lado)
     - grifo: justo detrás del fregadero
   - **Tamaño (defaults reales de corte)**:
     - placa inducción 4 zonas (60cm): **492×562 mm** (tamaño de hueco de placa estándar Bosch/Balay/Siemens)
     - placa inducción 2 zonas / dominó: **270×490 mm**
     - placa gas 60cm: **560×480 mm**
     - fregadero 1 seno: **400×400 mm**
     - fregadero 2 senos: **780×480 mm**
     - fregadero con escurridor: **1000×480 mm**
     - grifo: **35×35 mm** (taladro)
4. **NUNCA dejes placa o fregadero con `distancia_lado_mm: null`** — es un dato crítico para partir la encimera. Estima siempre y anota la confianza en `notas` (ej: "estimado centrado, sin cota explícita").

**NOTA — Tipo de fregadero (subtipo):**
- **NO ASUMAS el subtipo.** Solo emite `subtipo: "bajo_encimera"` o `"sobre_encimera"` si la nota/plantilla/presupuesto contiene las siglas explícitas **B/E**, **S/E**, **BE**, **SE**, o el texto "bajo encimera" / "sobre encimera" / "enrasado".
- Si no se menciona ninguna de esas indicaciones, deja `subtipo: null` — es ambiguo y preferimos nulo antes que inventar.
- Orientación (solo informativa, NO para decidir subtipo por defecto): los materiales porcelánicos (Dekton, Coverlam, Neolith, Ceratop, Laminam) suelen llevar sobre encimera por fragilidad al corte bajo encimera — pero si la nota no lo dice explícitamente, aún así deja `subtipo: null`.
- Si hay discrepancia entre documentos, anótalo en advertencias pero usa el del presupuesto MGR más reciente sin inventar.

**Cantos** (se cobran por ml — SOLO incluir en esta lista los tipos de abajo):
- ingletado: unión en inglete 45° entre dos piezas (cascada, esquinas externas de pilares)
- recto_pulido: canto recto pulido visible
- recto_pulido_agua: canto recto con media caña o pulido especial
- bisel: canto biselado
- boleado: canto boleado/redondeado
- canto_pilastra: canto en arista de pilar

**CÁLCULO DE ML INGLETADO — REGLAS PRECISAS:**
Los ingletes solo se hacen en **esquinas exteriores visibles** (donde dos piezas se encuentran formando un ángulo de 90° a la vista). NUNCA en uniones interiores (encimera en L contra pared interior, juntas entre tramos de encimera).
Cada esquina exterior requiere dos cortes a 45° (uno por cada pieza), por tanto:
- **Cascada/pata + encimera**: `ancho_encimera × 2`. Ej: ancho 620mm → 0,62 × 2 = 1,24ml
- **Pilar con chapeado en 1 esquina**: `altura_frontal × 2`. Ej: altura 580mm → 0,58 × 2 = 1,16ml por esquina
- **Pilar con chapeado en 2 esquinas** (pilar con 3 piezas de frontal): `altura_frontal × 4`. Ej: 0,58 × 4 = 2,32ml
- **Isla con 2 cascadas**: `(ancho_encimera × 2) × 2 = ancho × 4`

**CÁLCULO DE ML CANTOS PULIDOS:**
- Encimera entre paredes: solo el canto frontal largo (el borde que da al usuario)
- Encimera isla/exenta: todos los bordes del perímetro
- Cascada: los dos cantos verticales expuestos (frontal + trasero de la pata)
- Frontal: cantos laterales en los extremos donde termina libre + canto superior si queda a la vista
- Copete: longitud superior completa + extremos expuestos (~5cm cada uno)

**IMPORTANTE**: "Pulido 2ª cara", "impermeabilizado", "colocación" u otras elaboraciones NO son cantos.
No las incluyas en la lista de cantos. Si son relevantes, menciónalas en `observaciones` o `advertencias`.

## MEDIDAS:
- Los planos de cocina (programa 2020) dan medidas en **milímetros** (mm).
- Los presupuestos MGR dan medidas en **metros** (m) con decimales.
- Convertir todo a mm para piezas individuales; mantener m/m² para totales.
- Las anotaciones a mano en los planos son igual de válidas que las impresas.
- **Ancho mínimo de encimera: 620mm**. Si el plano indica 610mm, usar 620mm (estándar mínimo para acomodar electrodomésticos correctamente). Solo usar un ancho menor si está explícitamente justificado (encimera auxiliar, barra estrecha, etc.).

**ATENCIÓN — ml vs m² en presupuesto MGR para frontales/copetes:**
En el presupuesto MGR, el frontal/chapeado puede aparecer de dos formas:
- Como `Longo × Ancho` en metros → ej: `3.10 × 0.60` = longitud × altura → área = 1.86m²
- Como un único valor decimal → puede ser **metros lineales** (longitud), NO metros cuadrados.
  Ej: si aparece `2.310` para un frontal, es 2.31ml (2310mm de longitud), no 2.31m².
**NO dividas ml por la altura para obtener la longitud** — si el dato ya es la longitud, úsalo directamente.
Verifica siempre si el valor es coherente con las dimensiones del plano.
- **Ancho/fondo por defecto de encimera: 620mm** si no se especifica en plano ni presupuesto.
- **Anchos inusuales** (ej: 670mm, 580mm): son válidos. Pueden deberse a distancia a pared, vuelo, o descuadre. Usar siempre la medida mayor indicada en el plano o presupuesto.
- **Medidas de entrantes o frentes visibles** (ej: 315mm en esquina de L): son dimensiones secundarias que resultan de restar el fondo de un tramo al largo del otro. No las uses como dimensión principal de la pieza — usa siempre la dimensión total más grande del tramo.

## LECTURA DE LETRA MANUSCRITA — PUNTO CRÍTICO:
La letra de los planos y plantillas es frecuentemente muy difícil de leer. Errores comunes que debes evitar:

**Números confundibles:**
- El **"3"** manuscrito se parece mucho al **"2"**. Ante la duda, usa el presupuesto MGR para confirmar. Ej: "2.40" podría ser "3.40".
- El **"1"** manuscrito puede parecer un "2" cuando está en una casilla marcada. Ante la duda, asume 1 unidad salvo confirmación.
- El **"0"** y el **"6"** también pueden confundirse en escritura rápida.

**Reglas de validación cruzada:**
- **La encimera y el frontal/chapeado de la misma pared tienen SIEMPRE el mismo largo**. Si el frontal mide 3.40m, la encimera de esa pared también mide 3.40m. Úsalo para confirmar medidas dudosas.
- Si la suma de tramos de encimera no cuadra con el total del presupuesto MGR, revisa si algún "2" es en realidad un "3".
- El presupuesto MGR (impreso, sin ambigüedad) es la fuente más fiable para confirmar medidas cuando la letra es dudosa.

**Casillas y checkboxes en la plantilla:**
- Una **X** en una casilla de opción (ej: "Sobre encimera ☒") significa que ESA opción está seleccionada, NO es el número 2 ni indica cantidad.
- Los checkboxes de tipo de fregadero (sobre encimera / bajo encimera / enrasado) funcionan igual: la X marca la opción elegida.
- Las cantidades de huecos (placa, fregadero, grifo, enchufe) aparecen como número escrito a mano en la casilla correspondiente. Si parece un "2" pero los demás huecos son "1", verifica con el presupuesto MGR.

## GROSOR DOBLE — "12+12 INGLETADO" vs CANTO:
**CRÍTICO**: "12+12 Ingletado" o "12+12 Ingletado Doble" indica que la encimera tiene **doble grosor en el canto frontal** (dos planchas de 12mm pegadas en inglete para aparentar mayor grosor). Esto es una elaboración del borde, NO el tipo de canto.
- El **canto** (recto, bisel, boleado...) se lee **por separado** en la plantilla, en el campo "Canto". Si la plantilla dice "Canto: Recto", el canto es recto aunque el grosor sea "12+12 Ingletado".
- El "Frente Alzado ml" en los presupuestos MGR generalmente corresponde a los **cantos ingletados del doble grosor**, no a un frontal adicional.

## NOMBRE DEL CLIENTE Y DIRECCIÓN:
- El nombre del cliente se lee del **documento**, no del nombre de la carpeta. El nombre en la carpeta puede tener errores ortográficos.
- La dirección se lee del **documento**. El nombre de la carpeta puede tener la ciudad incorrecta (ej: carpeta dice "Vilanova de Arousa" pero el documento dice "Vilagarcia de Arousa" → usar la del documento).

## HUECOS — ENCHUFES:
**La plantilla tiene prioridad cuando dice algo explícito, pero su silencio NO significa "cero"**:
- Si la plantilla pone un número explícito (ej: "0", "1", "2", "3") → **usar ese número exacto**, aunque el MGR diga otra cosa.
- Si la plantilla deja el campo **en blanco, con guión o sin marcar** → aplicar el default de abajo (NO es 0).
- Si la plantilla tiene la casilla de enchufes marcada pero el número no está claro → **mínimo 2**.

**DEFAULT cuando plantilla en silencio (campo vacío, guión, sin marca)**:
- **En cocina**, si hay frontal/chapeado TRASERO (pegado a pared, no "de cabeza"/lateral de isla) → **mínimo 1 enchufe** (criterio de marmolería: es muy probable que lo haya).
- **Escalado con frontal largo**: 1 enchufe por cada 1,5m lineales de frontal (redondear hacia arriba, mínimo 1). Ej: 2m → 2; 3,5m → 3.
- **Si NO hay frontal** o solo hay frontal "de cabeza" (cabezas cortas de isla) → 0 enchufes por defecto, a menos que la plantilla los indique.

**EXCEPCIÓN — zonas secundarias (lavandería, baño, office)**:
- El default automático "1 enchufe si hay chapeado" **NO aplica** en estas zonas.
- Solo incluir enchufes en lavandería/baño/office si la plantilla los marca explícitamente con número. Si silenciosa → 0.

## MEDIDAS REVISADAS — SUBCARPETAS "SEGUNDAS", "TERCERAS":
Cuando un PDF aparece etiquetado como "MEDIDAS REVISADAS (Segundas)" o similar, significa que el cliente envió una **segunda toma de medidas más reciente** que corrige o complementa las primeras.
- Las "Segundas" contienen el plano actualizado con las medidas definitivas.
- El presupuesto MGR de número más alto (más reciente) suele corresponder a esas medidas revisadas.
- **Usa siempre las medidas de "Segundas" como las definitivas** cuando existan, descartando las de "Primeras" si hay contradicción.
- Si hay diferencias entre primeras y segundas medidas, anótalo en advertencias indicando cuál se ha usado.

## ⚠ MÚLTIPLES PRESUPUESTOS — NO DUPLICAR PIEZAS NI HUECOS:
Cuando el proyecto contiene **varios presupuestos MGR** (p. ej. `PR_2100`, `PR_2102`, `PR_2103` con fechas distintas, o "Presupuesto 1043" y "Presupuesto 1047" en distintas hojas del mismo Excel), son **REVISIONES DE LA MISMA OBRA**, NO obras distintas ni adicionales.
- **Usa ÚNICAMENTE el presupuesto más reciente** (número más alto / fecha más reciente) como fuente de verdad para piezas, huecos, cantos, material y cantidades.
- **NO sumes** piezas, huecos o metros de presupuestos anteriores con los del más reciente.
- **Cada pieza y cada hueco debe aparecer una sola vez.** Si detectas que estás a punto de emitir 2 encimeras idénticas o 2 fregaderos iguales porque aparecen en varios PDFs, es señal de que estás duplicando entre revisiones — quédate con los del más reciente.
- Si un presupuesto anterior tenía un elemento (zócalo, copete, fregadero extra) que el más reciente ya no tiene, respeta al más reciente: probablemente el cliente lo eliminó. Anótalo en advertencias.
- Lo mismo aplica a las hojas internas de un Excel MGR: si hay varias hojas "Presupuesto NNNN", **usa solo la de número más alto**.

## ZONAS MÚLTIPLES (cocina + lavandería, cocina + baño, etc.):
- Cuando en una perspectiva o en la plantilla aparece la palabra "lavandería", "baño", "office", o similar, es una **zona separada** con su propia encimera independiente.
- Crear piezas separadas para cada zona, indicando la zona en el campo `zona`.
- El ancho por defecto (620mm) aplica también a estas zonas si no se especifica.

## DISCREPANCIAS ENTRE DOCUMENTOS:
- Diferencias entre plantilla marmolista y presupuesto MGR (tipo de fregadero, medidas, elaboraciones) pueden deberse a **modificaciones posteriores por llamada telefónica**. No son necesariamente errores — anotarlas como advertencia indicando cuál parece más reciente.
- Si el presupuesto MGR es de fecha posterior a la plantilla, probablemente refleja el estado final.
- **Zócalo/copete que desaparece en presupuesto revisado**: Si el presupuesto más reciente no incluye zócalo o copete que sí aparecía en uno anterior, lo más habitual es que el cliente lo **eliminó para abaratar costes**. Usar el presupuesto más reciente como definitivo e indicarlo en advertencias.

## CASO ESPECIAL — CLIENTE APORTA ENCIMERA PROPIA (~1% de los casos):
A veces la tienda de cocina solo cambia las puertas de los muebles antiguos y el cliente únicamente necesita:
- Un **chapeado/frontal nuevo** que combine con las puertas nuevas (sin encimera nueva)
- O bien quitar la encimera vieja y poner una nueva

En estos casos el presupuesto solo incluye **chapeado m2** sin encimera de piedra. Reconocerlo cuando:
- Las observaciones de la plantilla mencionen "solo frontal", "encimera del cliente", "encimera existente" o similar
- El presupuesto MGR tenga chapeado pero no encimera
- Crear solo las piezas de frontal/chapeado, sin encimera, e indicarlo claramente en advertencias.

## TÉRMINO "FRONTIS":
- "Frontis" es sinónimo de **frontal/chapeado**. Si aparece escrito en el plano sobre o debajo de la encimera, indica que esa zona lleva chapeado de piedra en la pared.

## MÚLTIPLES MATERIALES:
Cuando el trabajo es "Varios materiales":
- Lo más habitual es que sean **2 presupuestos alternativos completos** (el cliente aún no ha elegido).
- En casos muy concretos y bien especificados, puede ser que distintas zonas de la misma cocina lleven materiales distintos (ej: encimera de una zona en material A y de otra zona en material B). En ese caso, asignar el material correcto a cada pieza.
- La plantilla marmolista con 2 columnas = 2 opciones alternativas.
- Cada opción incluye su propio frontal, copete y zócalo del mismo material.

**REGLA CRÍTICA — rol "encimera" sin sufijo por defecto**:
- Si en el **presupuesto MGR más reciente** aparece UN SOLO material elegido para la encimera, emite ese material con `rol: "encimera"` (sin sufijo `_opcion1`, `_opcion2`, etc.) y las piezas con `material_rol: "encimera"`.
- Usa sufijos `encimera_opcion1`, `encimera_opcion2` SOLO cuando realmente hay varios materiales alternativos **no resueltos** en el presupuesto más reciente (el cliente todavía no eligió). En ese caso, duplica las piezas por opción y NUNCA uses `/` ni `|` en `material_rol`.
- Si en presupuestos antiguos había varias opciones pero el presupuesto más reciente ya concreta una sola, la opción elegida se emite como `rol: "encimera"` y el resto se descarta (opcionalmente anótalo en advertencias).
- Si hay ambigüedad, emite la que primero aparece en el presupuesto más reciente como `rol: "encimera"` principal y documenta las alternativas en `advertencias`.

**⚠ HUECOS NO SE DUPLICAN ENTRE OPCIONES**: Los huecos (placa, fregadero, grifo, enchufe, dosificador) **son los mismos físicamente** — es la misma cocina, solo cambia el material. Emítelos UNA sola vez aunque haya 2+ opciones de material. La sección "PLACAS/FREGADEROS/GRIFOS/ENCHUFES" de la plantilla se lee UNA vez.
- Si hay 1 placa + 1 fregadero + 1 grifo + 2 enchufes, el JSON lleva 1+1+1+2 huecos, NO 2+2+2+4.
- Si el tipo/subtipo de fregadero difiere entre opciones (ej: opción1 = bajo encimera, opción2 = sobre encimera), emite UNA sola entrada de fregadero con el subtipo más común o null, e indica la variación en `notas`.
- La plantilla marmolista con marcas "X" para tipo de fregadero o números de huecos se lee UNA vez — esas X y números NO se multiplican por número de opciones.

**⚠ CANTOS NO SE DUPLICAN ENTRE OPCIONES**: Los cantos (ingletado, recto_pulido, recto_pulido_agua, bisel, boleado, canto_pilastra) **son los mismos físicamente** — es la misma geometría de pieza. Emite UNA sola entrada por tipo de canto aunque varios presupuestos MGR lo listen por opción.
- NO crees entradas separadas `recto_pulido_agua: 4ml (opción 1)` + `recto_pulido_agua: 13ml (opción 3)`. Usa la longitud del presupuesto MGR más reciente Y que corresponda a la opción que vayas a emitir como encimera principal.
- Si hay varias opciones no resueltas con cantos idénticos, usa la longitud de una sola (la más reciente) y describe en `notas` que aplica a todas.
- NUNCA sumes longitudes de cantos de distintos presupuestos: es doblecuenta.

**⚠ PIEZAS FRONTAL/COPETE/ZÓCALO — misma geometría entre opciones**:
- Si duplicas piezas por opción (porque el material no está resuelto), usa sufijos `_opcion1`, `_opcion2` en `material_rol` para que el downstream pueda deduplicar.
- NUNCA emitas dos piezas de igual geometría con `material_rol: "frontal"` (sin sufijo). Si hay dos opciones sin resolver y quieres representarlas, usa `frontal_opcion1` y `frontal_opcion2`. Si hay una sola decisión, emite UNA sola pieza con `material_rol: "frontal"`.

## COSTADO INGLETADO — IDENTIFICACIÓN:
Si en el plano aparece una anotación como "X.XX × Y.YY Costado Ingletado" o "Costado" junto a una pieza lateral de isla o encimera, crear una pieza de tipo "costado" con:
- largo_mm = la medida indicada × 1000 (si está en metros)
- ancho_mm = la otra medida × 1000
- notas = "ingletado con encimera"
Y añadir a cantos: {"tipo": "ingletado", "longitud_ml": ancho_encimera}

## REGLAS IMPORTANTES:
1. "Igual a encimera" = ese componente usa exactamente el mismo material que la encimera.
2. Si el plano no tiene medidas pero hay presupuesto, usa las medidas del presupuesto (en metros).
3. Si solo hay plantilla sin plano ni presupuesto, extrae material pero indica baja confianza en dimensiones.
4. Discrepancias entre documentos → anotarlas en "advertencias".
5. **Las piezas en L o U se representan como una sola pieza** con sus dimensiones globales. NO las dividas en rectángulos — eso se hará más adelante en el proceso de corte.
6. No incluyas campos null, simplemente omítelos.
7. **Si el material copete está definido**, SIEMPRE crea al menos una pieza de tipo "copete" con su `longitud_ml`. Si no tienes la medida exacta, estímala de los presupuestos o del largo de la encimera.
8. **Si la plantilla indica DOSIFICADOR = SÍ**, inclúyelo en huecos: `{"tipo": "dosificador", "cantidad": 1, "posicion": "derecha/izquierda si se indica"}`.
9. **Grosor uniforme por opción**: Dentro de la misma opción, encimera y chapeado/copete suelen tener distinto grosor (encimera 2cm o 3cm, chapeado/copete 1.2cm). Lo que NO varía es el grosor de la encimera dentro de una misma opción. **Excepción**: cuando hay varias opciones de material alternativas, el grosor de la encimera puede cambiar entre opciones (ej: opción A con encimera 3cm y opción B con encimera 2cm del mismo material). Leer siempre el grosor de cada opción del presupuesto MGR correspondiente y reflejarlo en `grosor_cm`.
10. **Opciones alternativas — piezas idénticas en geometría**: Cuando hay varias opciones de material alternativas **no resueltas en el presupuesto más reciente** (el cliente aún no ha elegido), **todas las piezas son idénticas en geometría** entre opciones que tengan las mismas dimensiones. Si el grosor varía entre opciones, crea materiales distintos con el `grosor_cm` correcto para cada uno. Duplica todas las piezas con el `material_rol` correspondiente a cada opción (`encimera_opcion1`, `encimera_opcion2`). Si en cambio el presupuesto más reciente ya tiene un material concreto elegido, emite una sola versión con `material_rol: "encimera"` (sin sufijo).
11. **Múltiples opciones con combinaciones de material**: Cuando un trabajo tiene presupuestos donde se combina material A en encimera con material B en chapeado (y otras combinaciones), crear una opción por cada combinación distinta, nombrándola claramente. Ej: `encimera_goiana_chapeado_fokos`, `encimera_goiana_todo_goiana`, etc. En las advertencias, listar cada opción con su precio total si está disponible para que el cliente pueda comparar.

## CANTOS PULIDOS — QUÉ SE INCLUYE:
**Tipo por defecto**: `recto_pulido_agua` (pulido normal con media caña). Usar `recto_pulido` (seco) SOLO si el material es **apomazado** o **abujardado**. Si el MGR lista "ML CANTO RECTO PULIDO" sin la palabra "AGUA", interpretarlo igualmente como `recto_pulido_agua` (terminología antigua NAT/PREF).

Los cantos pulidos corresponden a **todos los bordes vistos** de las piezas de piedra:
- Todos los cantos frontales de las encimeras (el borde que queda al aire, de cara al usuario)
- Todos los copetes (su canto frontal visible)
- Las **cabezas (extremos cortos) de copetes y chapeados que queden vistas** — no ocultas contra pared ni dentro de hueco de mueble. Pulidas.
- **Cantos de copetes/chapeados que peguen contra una ventana**: van pulidos (sobresalen un poco y se rematan).
- Las cabezas (extremos cortos) de los rodapiés/zócalos que quedan vistos

**⚠ NO DOBLAR CUENTAS — el ML CANTO RECTO PULIDO AGUA del presupuesto MGR es solo el canto FRONTAL DE ENCIMERA**:
- En el presupuesto MGR, la línea `M2 COLOCACION CHAPEADO PARED` **ya incluye el corte y pulido de todos los lados del chapeado/frontal**. **NO sumes esos lados al ml de canto pulido.**
- La línea `ML CANTO RECTO PULIDO AGUA` del MGR corresponde EXCLUSIVAMENTE a: el canto frontal visible de la encimera + el canto frontal del copete si se presupuesta como pieza separada.
- **Los laterales del chapeado/frontal NO se cuentan como ml separado** — están incluidos en el chapeado_m2.
- **Cuando tengas el valor del MGR `ML CANTO RECTO PULIDO AGUA`, úsalo TAL CUAL como longitud del canto**. NO lo sumes a los perímetros calculados tú — ya está calculado. Emite UNA sola entrada `recto_pulido_agua` con la longitud exacta del MGR.

No son el dato más crítico del trabajo — si hay incertidumbre, anótalos en advertencias pero no bloquees la extracción.

## ESPESOR EN COPETE/CHAPEADO — PLANTILLA VS MATERIAL REAL:
En la plantilla manuscrita, el campo "Copete" o "Chapeado" puede incluir un número como **"1,2"** o **"1.2"**.
Esto indica el **espesor de material especificado** (1.2cm), NO una medida de altura.
**En la práctica**: Los copetes y rodapiés se fabrican del mismo espesor que la encimera (2cm) si no hay tabla de 12mm disponible para ese trabajo, o si el chapeado también es de 2cm. El presupuesto MGR reflejará el espesor real utilizado. No interpretes este número como una dimensión de pieza — es solo una referencia de espesor.
Si la plantilla pone "1,2" en el campo copete pero el MGR presupuesta copetes con material de 2cm, es coherente y no es un error.

## INGLETADO EN CHAPEADOS PORCELÁNICOS — ESQUINAS VISTAS (PILARES):
Los materiales **porcelánicos** (Dekton, Coverlam, Neolith, Ceratop, Laminam y similares) son una capa fina con impresión decorativa sobre un núcleo. Cuando este material **hace una esquina visible a 90°**, el núcleo/masa quedaría expuesto en el canto. Para evitarlo, las piezas que forman esa esquina se ingletan a 45° y se juntan.
**Cuándo aparece ingletado en chapeados**:
- Cuando el chapeado rodea un **pilar o columna** con esquinas vistas
- Cuando hay una **esquina entre dos paredes chapadas** que se ve
- NO aplica a esquinas contra la pared o esquinas que van a quedar ocultas
**Cálculo del ML INGLETADO para chapeado en pilares**:
- Cada esquina visible requiere ingletes en los dos cantos que se juntan (el extremo de la pieza central y el extremo de la pieza lateral)
- Un pilar típico con chapeado tiene **4 cantos ingletados** (los 2 laterales de la pieza central + 1 extremo de cada pieza lateral)
- Fórmula: `ML ingletado = número_cantos_ingletados × altura_chapeado`
- Ejemplo: 4 cantos × 0.58m de alto = 2.32ml
Esto es DISTINTO del ingletado de un costado/cascada (que une encimera con panel vertical lateral de isla).

## EMPRESA ACyC (Accesorios y Cocinas):
Cuando la carpeta contiene "ACyC" en el nombre, el flujo de trabajo es diferente:
- **No hay plantilla marmolista manuscrita**. La información del encargo llega en notas de texto (TXT), correos o WhatsApp.
- **Facturas MGR** (prefijo "F" + 6 dígitos, ej: F250272): Son las facturas del trabajo **ya ejecutado**. Tienen mayor autoridad que los presupuestos (PR) iniciales porque reflejan las medidas reales tomadas en obra.
- **"Trasera"** en las notas ACyC = lo mismo que "frontal" o "chapeado".
- El campo `tienda` es "ACyC" y no hay `vendedor` específico (es la propia empresa).
- En la plantilla ACyC, "BARRA/ISLA" con raya = sin isla/barra.

## FORMATO DE RESPUESTA — MUY IMPORTANTE ⚠️:
**Tu respuesta DEBE comenzar DIRECTAMENTE con el carácter `{` y terminar con `}`.**
**PROHIBIDO escribir cualquier texto antes del JSON.**
**PROHIBIDO escribir análisis, conclusiones o comentarios antes o después del JSON.**
**PROHIBIDO usar bloques ```json``` o cualquier otro bloque de código.**
**Si no puedes hacerlo en una sola respuesta, escribe el JSON igualmente — es mejor JSON incompleto que análisis sin JSON.**

Estructura de ejemplo para un trabajo real (J0297 Elisa Baños):
{
  "job_id": "J0297",
  "cliente": "Elisa Baños",
  "tienda": "Cocimoble",
  "vendedor": "David",
  "ciudad": "Vigo",
  "piso": "8º",
  "fecha": "12/01/2026",
  "materiales": [
    {"rol": "encimera", "marca": "Laminam", "color": "Bianco Lasa", "grosor_cm": 1.2, "acabado": "Mate", "canto": "recto"},
    {"rol": "frontal", "es_igual_a": "encimera", "grosor_cm": 1.2, "altura_cm": 58},
    {"rol": "copete", "es_igual_a": "encimera", "grosor_cm": 1.2, "altura_cm": 5},
    {"rol": "zocalo", "marca": "Guidoni", "color": "Blanco Absoluto", "grosor_cm": 1.2, "acabado": "Pulido", "altura_cm": 10}
  ],
  "piezas": [
    /* Encimera principal con pilar saliente trasero-derecha.
       Tramo principal 2700×600, saliente trasero 300×200 (200 mm extra de fondo
       hacia la pared, en los últimos 300mm a lo largo). Vértices en orden CCW
       desde esquina FRONTAL-izquierda. El saliente va Y alto (atrás), no Y bajo. */
    {
      "tipo": "encimera", "material_rol": "encimera",
      "forma": "poligono",
      "vertices_mm": [[0,0],[2700,0],[2700,400],[3000,400],[3000,600],[0,600]],
      /* aristas_contacto alineada con vertices_mm: arista i = vértice i → i+1.
         idx0 frente=vista, idx1 cabeza dcha junto a pilar=mueble,
         idx2-3 envuelven el pilar=pared, idx4 trasera (con chapeado)=pared,
         idx5 cabeza izquierda contra muro=pared */
      "aristas_contacto": ["vista","mueble","pared","pared","pared","pared"],
      /* aristas_cota: cota LEÍDA del plano por arista (null = sin cota; el
         programa la deriva por cierre). Nunca valores calculados. */
      "aristas_cota": [2700, null, 300, 200, 3000, 600],
      "largo_mm": 3000, "ancho_mm": 600,
      "zona": "pared norte principal con saliente pilar trasero derecho"
    },
    {
      "tipo": "isla", "material_rol": "encimera",
      "forma": "rectangulo",
      "vertices_mm": [[0,0],[2200,0],[2200,900],[0,900]],
      /* isla exenta: TODO vista */
      "aristas_contacto": ["vista","vista","vista","vista"],
      "largo_mm": 2200, "ancho_mm": 900,
      "zona": "isla central"
    },
    /* Cuando hay pilar saliente trasero, el chapeado se hace en 2 piezas
       unidas con inglete: una lateral del pilar y otra frontal. */
    {
      "tipo": "frontal", "material_rol": "frontal",
      "largo_mm": 2400, "altura_mm": 600, "zona": "frontal pared norte tramo principal"
    },
    {
      "tipo": "frontal", "material_rol": "frontal",
      "largo_mm": 200, "altura_mm": 600, "zona": "frontal lateral pilar (cara este)"
    },
    {
      "tipo": "frontal", "material_rol": "frontal",
      "largo_mm": 300, "altura_mm": 600, "zona": "frontal frente pilar"
    },
    /* COPETE: SOLO en cabezas vistas (extremos abiertos de encimera). En las paredes
       donde ya hay frontal (chapeado), NO emitir copete. */
    {
      "tipo": "copete", "material_rol": "copete",
      "longitud_ml": 0.6, "altura_mm": 50,
      "zona": "copete cabeza vista izquierda (extremo abierto encimera)"
    },
    {
      "tipo": "copete", "material_rol": "copete",
      "longitud_ml": 0.4, "altura_mm": 50,
      "zona": "copete cabeza vista derecha (extremo encimera junto al pilar — fondo libre 400)"
    },
    {
      "tipo": "zocalo", "material_rol": "zocalo",
      "longitud_ml": 3.18, "altura_mm": 100, "zona": "pared superior muebles bajos"
    }
  ],
  "huecos": [
    /* Placa Bosch 60cm: 562mm a lo largo de la encimera × 492mm de fondo.
       Centrada en su módulo de mueble (centro_x=1350, centro_y a media profundidad). */
    {
      "tipo": "placa", "cantidad": 1, "pieza_zona": "pared norte principal",
      "centro_x_mm": 1350, "centro_y_mm": 310,
      "largo_mm": 562, "ancho_mm": 492
    },
    {
      "tipo": "fregadero", "cantidad": 1, "subtipo": "sobre_encimera",
      "pieza_zona": "pared norte principal",
      "centro_x_mm": 600, "centro_y_mm": 290,
      "largo_mm": 780, "ancho_mm": 480
    },
    {
      "tipo": "grifo", "cantidad": 1, "pieza_zona": "pared norte principal",
      "centro_x_mm": 600, "centro_y_mm": 540,
      "largo_mm": 35, "ancho_mm": 35
    },
    {
      "tipo": "enchufe", "cantidad": 1, "pieza_zona": "pared norte principal"
    }
  ],
  "cantos": [
    {"tipo": "ingletado", "longitud_ml": 3.48},
    {"tipo": "recto_pulido_agua", "longitud_ml": 6.22}
  ],
  "fregadero_tipo": "sobre_encimera",
  "confianza": "alta",
  "advertencias": []
}
"""


def parse_folder_name(folder_name: str) -> dict:
    """Extrae info básica del nombre de la carpeta."""
    info = {}
    parts = folder_name.split('_')
    if parts:
        # ID: J0297, V0183, T8113, I007...
        info['job_id'] = parts[0] if parts else ''
        # Cliente: second part
        if len(parts) > 1:
            info['cliente'] = parts[1]
        # Tienda-Vendedor: "Cocimoble-David"
        if len(parts) > 2:
            tv = parts[2]
            if '-' in tv:
                tv_parts = tv.split('-', 1)
                info['tienda'] = tv_parts[0]
                info['vendedor'] = tv_parts[1]
        # Ciudad
        if len(parts) > 3:
            info['ciudad'] = parts[3]
        # Material principal (resto del nombre)
        if len(parts) > 4:
            info['material_carpeta'] = '_'.join(parts[4:])
    return info


def _mensaje_streaming(client, **kwargs):
    """Llamada en streaming (obligatoria para max_tokens grandes): devuelve
    el mensaje final completo. Fable/Opus razonan DENTRO del presupuesto de
    salida, así que hace falta margen amplio.

    EXTRACTOR_EFFORT (env var, opcional): low|medium|high|xhigh|max —
    profundidad de razonamiento del modelo (default del modelo: high)."""
    effort = os.environ.get('EXTRACTOR_EFFORT')
    if effort:
        kwargs['output_config'] = {'effort': effort}
    with client.messages.stream(**kwargs) as stream:
        return stream.get_final_message()


def _texto_respuesta(message) -> str:
    """Texto de la respuesta ignorando bloques de thinking (Fable/Opus 4.7+
    con razonamiento siempre activo devuelven ThinkingBlock delante)."""
    return "".join(b.text for b in message.content
                   if getattr(b, 'type', None) == 'text')


def extract_json_from_response(text: str) -> Optional[dict]:
    """Extrae el JSON de la respuesta de Claude, con múltiples estrategias."""
    # 1. Bloques ```json ... ``` — el ÚLTIMO suele ser el resultado final
    # (si hay varios, los primeros pueden ser análisis intermedios)
    bloques = re.findall(r'```json\s*([\s\S]+?)\s*```', text, re.IGNORECASE)
    for bloque in reversed(bloques):
        try:
            return json.loads(bloque)
        except json.JSONDecodeError:
            continue

    # 2. JSON puro desde el primer { hasta el último }
    first = text.find('{')
    last = text.rfind('}')
    if first != -1 and last != -1 and last > first:
        candidate = text[first:last+1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # 3. Intentar reparar JSON truncado: buscar el { más externo y cerrar
    if first != -1:
        candidate = text[first:]
        # Contar llaves para encontrar hasta dónde está completo
        depth = 0
        end = -1
        in_string = False
        escape = False
        for i, ch in enumerate(candidate):
            if escape:
                escape = False
                continue
            if ch == '\\' and in_string:
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end != -1:
            try:
                return json.loads(candidate[:end+1])
            except json.JSONDecodeError:
                pass

    return None


def json_to_trabajo(data: dict, folder_info: dict, folder=None) -> TrabajoExtraido:
    """Convierte el dict extraído en un objeto TrabajoExtraido.

    Si se proporciona `folder`, además se cargan las anotaciones del operador
    para paredes y muebles_altos (capas nuevas del anotador) y se inyectan
    en el objeto antes del postproc para que la reconciliación las use como
    verdad sobre qué aristas son pared.
    """

    def safe_float(v):
        if v is None:
            return None
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    def safe_int(v):
        if v is None:
            return None
        try:
            return int(v)
        except (ValueError, TypeError):
            return None

    # Materiales
    materiales = []
    for m in data.get('materiales', []):
        materiales.append(MaterialSpec(
            rol=m.get('rol', 'desconocido'),
            marca=m.get('marca'),
            color=m.get('color'),
            grosor_cm=safe_float(m.get('grosor_cm')),
            acabado=m.get('acabado'),
            altura_cm=safe_float(m.get('altura_cm')),
            canto=m.get('canto'),
            es_igual_a=m.get('es_igual_a'),
            notas=m.get('notas'),
        ))

    # Piezas
    piezas = []
    for p in data.get('piezas', []):
        # vertices_mm puede venir como lista de [x,y] o lista de {"x":..,"y":..}
        verts = p.get('vertices_mm') or p.get('vertices')
        if verts and len(verts) > 0 and isinstance(verts[0], dict):
            verts = [[v.get('x'), v.get('y')] for v in verts]
        # aristas_contacto: lista alineada con vertices (arista i = v_i→v_i+1)
        contactos = p.get('aristas_contacto')
        if contactos is not None:
            if (not isinstance(contactos, list) or not verts
                    or len(contactos) != len(verts)
                    or not all(isinstance(c, str) and c.lower() in
                               ('pared', 'vista', 'mueble', 'ventana')
                               for c in contactos)):
                contactos = None  # malformada → ignorar (caerá a heurística)
            else:
                contactos = [c.lower() for c in contactos]
        # aristas_cota: cota real leída por arista (mm o null), alineada
        cotas_ar = p.get('aristas_cota')
        if cotas_ar is not None:
            if (not isinstance(cotas_ar, list) or not verts
                    or len(cotas_ar) != len(verts)
                    or not all(c is None or isinstance(c, (int, float))
                               for c in cotas_ar)):
                cotas_ar = None
            else:
                cotas_ar = [float(c) if c is not None else None
                            for c in cotas_ar]
        piezas.append(Pieza(
            tipo=p.get('tipo', 'desconocido'),
            material_rol=p.get('material_rol', 'encimera'),
            forma=p.get('forma'),
            vertices_mm=verts,
            aristas_contacto=contactos,
            aristas_cota=cotas_ar,
            largo_mm=safe_float(p.get('largo_mm')),
            ancho_mm=safe_float(p.get('ancho_mm')),
            altura_mm=safe_float(p.get('altura_mm')),
            area_m2=safe_float(p.get('area_m2')),
            longitud_ml=safe_float(p.get('longitud_ml')),
            zona=p.get('zona'),
            notas=p.get('notas'),
        ))

    # Huecos
    huecos = []
    for h in data.get('huecos', []):
        huecos.append(Hueco(
            tipo=h.get('tipo', 'desconocido'),
            cantidad=safe_int(h.get('cantidad')) or 1,
            pieza_zona=h.get('pieza_zona'),
            centro_x_mm=safe_float(h.get('centro_x_mm')),
            centro_y_mm=safe_float(h.get('centro_y_mm')),
            largo_mm=safe_float(h.get('largo_mm')),
            ancho_mm=safe_float(h.get('ancho_mm')),
            posicion=h.get('posicion'),
            distancia_lado_mm=safe_float(h.get('distancia_lado_mm')),
            subtipo=h.get('subtipo'),
            notas=h.get('notas'),
        ))

    # Cantos
    cantos = []
    for c in data.get('cantos', []):
        cantos.append(Canto(
            tipo=c.get('tipo', 'desconocido'),
            longitud_ml=safe_float(c.get('longitud_ml')),
            notas=c.get('notas'),
        ))

    trabajo = TrabajoExtraido(
        job_id=data.get('job_id') or folder_info.get('job_id', ''),
        cliente=data.get('cliente') or folder_info.get('cliente', ''),
        tienda=data.get('tienda') or folder_info.get('tienda'),
        vendedor=data.get('vendedor') or folder_info.get('vendedor'),
        direccion=data.get('direccion'),
        ciudad=data.get('ciudad') or folder_info.get('ciudad'),
        fecha=data.get('fecha'),
        piso=data.get('piso'),
        materiales=materiales,
        piezas=piezas,
        huecos=huecos,
        cantos=cantos,
        tipo_cascada=data.get('tipo_cascada'),
        fregadero_tipo=data.get('fregadero_tipo'),
        tablas_reservadas=data.get('tablas_reservadas'),
        proveedor_tablas=data.get('proveedor_tablas'),
        observaciones=(' '.join(data['observaciones']) if isinstance(data.get('observaciones'), list) else data.get('observaciones')),
        notas_extra=data.get('notas_extra'),
        confianza=data.get('confianza', 'media'),
        advertencias=data.get('advertencias', []),
    )
    if folder is not None:
        try:
            _cargar_paredes_y_muebles_altos(folder, trabajo)
        except Exception as e:
            trabajo.advertencias.append(f"Postproc: error cargando paredes/muebles_altos: {e}")
    # Snapshot de lo que emitió Claude ANTES del postproc (para el log de
    # divergencia Claude-vs-operador que alimenta el plan de aprendizaje)
    snapshot_claude = {
        'cantos': [(c.tipo, c.longitud_ml, c.notas) for c in trabajo.cantos],
        'piezas': [(p.tipo, p.largo_mm, p.ancho_mm, p.zona) for p in trabajo.piezas],
    }
    _espejar_opciones(trabajo)
    _ajustar_costado_cascada(trabajo)
    _reconciliar_geometria_encimera(trabajo)
    _completar_piezas_desde_trazos(trabajo)
    _reposicionar_huecos_desde_trazos(trabajo)
    _ajustar_profundidad_huecos(trabajo)
    _validar_huecos_dentro(trabajo)
    _completar_pulidos_pata(trabajo)
    _completar_inglete_pata(trabajo)
    _completar_copete_principal(trabajo)
    _completar_ingletes_implicitos(trabajo)
    _verificar_muesca_pilar(trabajo)
    # Segundo espejado: las piezas creadas por el postproc (copetes de
    # exclusión llevan sufijo, pero las creadas desde trazos no) deben
    # existir en TODAS las opciones de material
    _espejar_opciones(trabajo)
    if folder is not None:
        try:
            _volcar_divergencia(trabajo, folder, snapshot_claude)
        except Exception as e:
            trabajo.advertencias.append(f"Postproc: error volcando divergencia: {e}")
    return trabajo


def _completar_copete_principal(trabajo: TrabajoExtraido) -> None:
    """Si la encimera tiene copetes de cabeza vista (cortos, ≈ fondo encimera)
    pero NO existe el copete principal contra la pared (largo de la encimera),
    se añade. Es un caso muy frecuente de omisión por el LLM.

    Heurística: por cada encimera horizontal con largo > 1.5m, si hay 1 o más
    copetes pequeños (≤ ancho encimera + 100mm de tolerancia) y NO hay copete
    de longitud cercana al largo de la encimera, emitir el copete principal.

    Salvaguarda: si el operador anotó copetes en el plano (capa naranja con
    puntos persistidos), su anotación es la verdad — no inventamos copetes
    adicionales para no duplicar las piezas reales.
    """
    # Si el operador anotó copetes (trazos con puntos persistidos en alguna
    # encimera), su anotación es la verdad — no auto-añadir nada.
    # (El antiguo flag _copetes_por_geo ya no se asigna; los trazos del
    # operador viven en pieza._anot_reg.)
    if any((getattr(p, '_anot_reg', None) or {}).get('copetes_pix')
           for p in trabajo.piezas):
        return
    copetes = [p for p in trabajo.piezas if p.tipo == 'copete']
    if not copetes:
        return  # Plantilla decía copete=NO, no inventar
    encimeras = [p for p in trabajo.piezas
                 if p.tipo in ('encimera', 'isla') and p.largo_mm and p.ancho_mm]
    nuevos: list[Pieza] = []
    for enc in encimeras:
        # Si esta encimera tiene clasificación fiable de aristas (trazos del
        # operador o aristas_contacto de Claude), el copete-por-exclusión del
        # reconciliador ya emitió los copetes de pared — la heurística del
        # "copete principal" solo aplica a encimeras sin información.
        if getattr(enc, '_anot_reg', None) or enc.aristas_contacto:
            continue
        largo_m = enc.largo_mm / 1000.0
        ancho_m = enc.ancho_mm / 1000.0
        if largo_m < 1.5:
            continue
        # Buscar copetes con el mismo material que esta encimera (mismo sufijo
        # _opcionN o mismo rol base), o que no sean de la opción contraria
        enc_rol = enc.material_rol or ''
        sufijo = ''
        for s in ('_opcion1', '_opcion2', '_opcion3', '_opcion_a', '_opcion_b'):
            if s in enc_rol:
                sufijo = s
                break
        copetes_match = [c for c in copetes
                         if (sufijo and sufijo in (c.material_rol or ''))
                         or (not sufijo)]
        if not copetes_match:
            continue
        # ¿Existe copete largo (≥ largo encimera - 200mm) en este grupo?
        ya_existe = any(
            (c.longitud_ml or 0) * 1000 >= enc.largo_mm - 200
            for c in copetes_match
        )
        if ya_existe:
            continue
        # Regla de dominio: el copete va donde la encimera toca pared SIN
        # chapeado. Si ya hay un frontal (chapeado) de largo ≈ esta encimera,
        # la pared trasera está cubierta → NO añadir copete principal.
        pared_con_chapeado = any(
            p.tipo == 'frontal' and p.largo_mm
            and abs(p.largo_mm - enc.largo_mm) <= max(200, enc.largo_mm * 0.1)
            for p in trabajo.piezas
        )
        if pared_con_chapeado:
            continue
        # ¿Hay copetes de cabeza (cortos) en este grupo?
        cabezas = [c for c in copetes_match
                   if (c.longitud_ml or 0) <= ancho_m + 0.1]
        if not cabezas:
            continue
        ref = copetes_match[0]
        nuevos.append(Pieza(
            tipo='copete',
            material_rol=ref.material_rol,
            longitud_ml=round(largo_m, 3),
            altura_mm=ref.altura_mm or 50.0,
            zona=f'copete principal pared trasera (largo {int(enc.largo_mm)}mm)',
            notas='Añadido por postprocesador — pared con cabezas vistas',
        ))
    trabajo.piezas.extend(nuevos)


def _recolectar_cotas(trabajo: TrabajoExtraido, encimera_skip) -> tuple:
    """Recopila cotas en mm. Devuelve (cotas_globales, cotas_propias):
      - cotas_globales: del JSON estructurado + texto general (advertencias y
        notas de otras piezas) con patrón estricto "Nmm".
      - cotas_propias: cotas mencionadas SOLO en la zona/notas de la encimera
        siendo snap-eada (señal fuerte de cota explícita para esta pieza).

    Las propias tienen prioridad en el snap: si una coord cae cerca de una
    propia, snap a esa aunque haya otras candidatas más cercanas en globales.
    """
    import re as _re
    cotas = set()
    cotas_propias = set()
    # 'Nmm' suelto Y AMBOS operandos de 'NxMmm' (p.ej. "2760x600mm")
    pat = _re.compile(
        r'\b(\d{3,4})\s*mm\b|\b(\d{3,4})\s*[x×]\s*(\d{3,4})\s*mm\b',
        _re.IGNORECASE)
    for p in trabajo.piezas:
        if p is encimera_skip:
            continue
        for v in (p.largo_mm, p.ancho_mm, p.altura_mm):
            if v and v >= 50:
                cotas.add(int(round(v)))
        if p.longitud_ml and p.longitud_ml >= 0.05:
            cotas.add(int(round(p.longitud_ml * 1000)))
    for c in trabajo.cantos:
        if c.longitud_ml and c.longitud_ml >= 0.05:
            cotas.add(int(round(c.longitud_ml * 1000)))
    def _valores(texto):
        for m in pat.findall(str(texto)):
            for g in (m if isinstance(m, tuple) else (m,)):
                if g:
                    v = int(g)
                    if 100 <= v <= 10000:
                        yield v

    # Texto general (advertencias + zona/notas de OTRAS piezas)
    for w in (trabajo.advertencias or []):
        for v in _valores(w):
            cotas.add(v)
    for p in trabajo.piezas:
        if p is encimera_skip:
            continue
        for src in (p.zona, p.notas):
            if src:
                for v in _valores(src):
                    cotas.add(v)
    # Cotas propias de la encimera siendo snap-eada (mayor prioridad)
    if encimera_skip:
        for src in (encimera_skip.zona, encimera_skip.notas):
            if src:
                for v in _valores(src):
                    cotas_propias.add(v)
                    cotas.add(v)
    return cotas, cotas_propias


def _snap_vertices_a_cotas(verts: list, cotas: set,
                            cotas_propias: set = None,
                            tol_pct: float = 0.10, tol_min_mm: float = 50) -> tuple:
    """Snapea las coordenadas únicas (x e y) del polígono a la cota más cercana
    del set dentro de tolerancia. `cotas_propias` (cotas mencionadas
    explícitamente en la zona/notas de ESTA encimera) tienen prioridad
    absoluta sobre las globales — si una coord cae dentro de tolerancia
    de una propia, snap a esa aunque haya una global más cercana.
    """
    if not cotas:
        return verts, {}
    cotas_sorted = sorted(cotas)
    propias = cotas_propias or set()

    def _candidato(c):
        if c == 0:
            return c
        tol = max(tol_min_mm, abs(c) * tol_pct)
        # 1) Si hay una cota propia dentro de tolerancia, tiene prioridad
        propias_dentro = [k for k in propias if abs(k - c) <= tol]
        if propias_dentro:
            return min(propias_dentro, key=lambda k: abs(k - c))
        # 2) Cotas globales del JSON
        cands = [k for k in cotas_sorted if abs(k - c) <= tol]
        # 3) Múltiplos típicos de cocina (tol 5%/40mm)
        tol_mult = min(tol, max(40, abs(c) * 0.05))
        for paso in (100, 50, 25):
            mult = round(c / paso) * paso
            if mult > 0 and abs(mult - c) <= tol_mult:
                cands.append(mult)
        if not cands:
            return c
        return min(cands, key=lambda k: abs(k - c))

    xs = sorted({v[0] for v in verts})
    ys = sorted({v[1] for v in verts})
    map_x = {x: _candidato(x) for x in xs}
    map_y = {y: _candidato(y) for y in ys}

    # Anti-colapso: si dos orígenes distintos snap-ean al mismo destino,
    # mantener solo el más cercano y revertir el otro.
    def _resolver_colisiones(mapeo):
        inverso = {}
        for orig, snap in mapeo.items():
            if snap in inverso and inverso[snap] != orig:
                a = inverso[snap]
                b = orig
                # Mantener el snap del más cercano; revertir el otro.
                # El revertido se registra también en `inverso` para que
                # colisiones posteriores contra su valor original se detecten.
                if abs(mapeo[a] - a) <= abs(mapeo[b] - b):
                    mapeo[b] = b
                    inverso.setdefault(b, b)
                else:
                    mapeo[a] = a
                    inverso.setdefault(a, a)
                    inverso[snap] = b
            else:
                inverso[snap] = orig

    _resolver_colisiones(map_x)
    _resolver_colisiones(map_y)

    nuevos = [[map_x[v[0]], map_y[v[1]]] for v in verts]
    snap_log = {
        'x': {orig: dst for orig, dst in map_x.items() if orig != dst},
        'y': {orig: dst for orig, dst in map_y.items() if orig != dst},
    }
    return nuevos, snap_log


def _signed_area_2d(verts: list) -> float:
    s = 0.0
    n = len(verts)
    for i in range(n):
        x1, y1 = verts[i]
        x2, y2 = verts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def _clasificar_aristas_encimera(verts: list) -> list:
    """Clasifica cada arista del polígono como 'frontal' (toca vértice cóncavo),
    'cabeza' (vecina de una frontal) o 'pared'. Devuelve lista de dicts.
    Si el polígono es convexo no hay cóncavos: todas las aristas se marcan
    como 'pared' (el postprocesador no actúa en convexos).
    """
    import math as _math
    n = len(verts)
    if n < 3:
        return []
    area = _signed_area_2d(verts)
    sentido = 1 if area > 0 else -1
    concavos = set()
    for i in range(n):
        x_prev, y_prev = verts[(i - 1) % n]
        x_cur, y_cur = verts[i]
        x_next, y_next = verts[(i + 1) % n]
        cross = (x_cur - x_prev) * (y_next - y_cur) - (y_cur - y_prev) * (x_next - x_cur)
        if cross * sentido < 0:
            concavos.add(i)
    aristas = []
    for i in range(n):
        v1, v2 = i, (i + 1) % n
        x1, y1 = verts[v1]
        x2, y2 = verts[v2]
        L = _math.hypot(x2 - x1, y2 - y1)
        toca_concavo = v1 in concavos or v2 in concavos
        aristas.append({'idx': i, 'v1': v1, 'v2': v2, 'len': L,
                        'tipo': 'frontal' if toca_concavo else 'pared'})
    if concavos:
        ids_frontal = {i for i, a in enumerate(aristas) if a['tipo'] == 'frontal'}
        for i, a in enumerate(aristas):
            if a['tipo'] == 'frontal':
                continue
            for j in ids_frontal:
                aj = aristas[j]
                if a['v1'] in (aj['v1'], aj['v2']) or a['v2'] in (aj['v1'], aj['v2']):
                    a['tipo'] = 'cabeza'
                    break
    return aristas


def _trazos_a_mm_local(trazos_pix: list, cand_pix: list, vertices_mm: list) -> list:
    """Convierte una lista de trazos en pixel-space a mm-local de una encimera,
    usando bbox como referencia (robusto a reordenamiento de vértices entre
    polilínea pixel del operador y vertices_mm de Claude).
    """
    if not trazos_pix or not cand_pix or not vertices_mm:
        return []
    xs_p = [p[0] for p in cand_pix]; ys_p = [p[1] for p in cand_pix]
    xs_m = [v[0] for v in vertices_mm]; ys_m = [v[1] for v in vertices_mm]
    x_min_px, y_min_px = min(xs_p), min(ys_p)
    x_max_px, y_max_px = max(xs_p), max(ys_p)
    x_min_mm, y_min_mm = min(xs_m), min(ys_m)
    x_max_mm, y_max_mm = max(xs_m), max(ys_m)
    W_px = max(x_max_px - x_min_px, 1)
    H_px = max(y_max_px - y_min_px, 1)
    W_mm = x_max_mm - x_min_mm
    H_mm = y_max_mm - y_min_mm
    if W_mm < 50 or H_mm < 50:
        return []
    ar_px = W_px / H_px
    ar_mm = W_mm / H_mm
    rotado = (ar_mm > 0
              and abs(ar_px - ar_mm) > abs(ar_px - 1.0 / ar_mm))
    if rotado:
        # x_mm viene de pixel y, y_mm viene de pixel x
        escala_xmm = W_mm / H_px
        escala_ymm = H_mm / W_px
    else:
        escala_xmm = W_mm / W_px
        escala_ymm = H_mm / H_px

    def _pt(x, y):
        if rotado:
            # ROTACIÓN real de 90° (det +1), no transposición: la
            # transposición pura (mm_x=+y, mm_y=+x) es un ESPEJO (det -1) y
            # producía la pieza especular — fregadero/cabezas intercambiados
            # en planos dibujados en vertical (detectado en J0021). Se
            # voltea el eje X (canvas-abajo → x=0) y se conserva el mapeo Y
            # directo validado en J0020.
            mm_x = x_min_mm + (y_max_px - y) * escala_xmm
            mm_y = y_min_mm + (x - x_min_px) * escala_ymm
        else:
            mm_x = x_min_mm + (x - x_min_px) * escala_xmm
            mm_y = y_min_mm + (y - y_min_px) * escala_ymm
        return (mm_x, mm_y)

    return [[_pt(x, y) for x, y in t] for t in trazos_pix]


def _desinclinar_pagina(etiquetas_pts: list) -> None:
    """Corrige IN PLACE la inclinación del escaneo de una página: calcula el
    ángulo de la arista más larga del contorno de encimera mayor (mod 90°,
    en (-45°,45°]) y, si está entre 1° y 25°, rota TODOS los puntos de la
    página por el ángulo opuesto (rotación pura — preserva quiralidad).
    Sin esto, un plano escaneado torcido convierte los rectángulos en
    paralelogramos y dispara falsas GEOMETRÍAS DUDOSAS (J0023, ~5°)."""
    import math as _math
    polys = [pts for tipo, modo, cerrado, pts in etiquetas_pts
             if tipo == 'encimera' and modo == 'poly' and cerrado and pts]
    if not polys:
        return

    def _area_bbox(p):
        xs = [q[0] for q in p]
        ys = [q[1] for q in p]
        return (max(xs) - min(xs)) * (max(ys) - min(ys))

    mayor = max(polys, key=_area_bbox)
    n = len(mayor)
    mejor_L = 0.0
    mejor_ang = 0.0
    for i in range(n):
        x1, y1 = mayor[i]
        x2, y2 = mayor[(i + 1) % n]
        L = _math.hypot(x2 - x1, y2 - y1)
        if L > mejor_L:
            mejor_L = L
            mejor_ang = _math.degrees(_math.atan2(y2 - y1, x2 - x1))
    desv = mejor_ang % 90.0
    if desv > 45.0:
        desv -= 90.0
    if not (1.0 <= abs(desv) <= 25.0):
        return
    todos = [q for _, _, _, pts in etiquetas_pts for q in (pts or [])]
    cx = sum(q[0] for q in todos) / len(todos)
    cy = sum(q[1] for q in todos) / len(todos)
    rad = _math.radians(-desv)
    cos_a, sin_a = _math.cos(rad), _math.sin(rad)
    for _, _, _, pts in etiquetas_pts:
        for q in (pts or []):
            dx, dy = q[0] - cx, q[1] - cy
            q[0] = cx + dx * cos_a - dy * sin_a
            q[1] = cy + dx * sin_a + dy * cos_a


def _cargar_paredes_y_muebles_altos(folder, trabajo: TrabajoExtraido) -> None:
    """Lee `anotaciones.json` del proyecto y stashea, para CADA encimera del
    trabajo (cada una con su propio sistema de coords local-mm trasladado a
    (0,0) por su bbox), las polilíneas de pared y muebles_altos convertidas
    a esa coordenada local. Necesario porque la encimera principal y la isla
    suelen tener orígenes diferentes y no pueden compartir el mismo offset.

    Resultado: trabajo._paredes_por_geo[geo_key] = [polylines_mm_local]
    Donde geo_key es la tupla de vértices_mm que identifica la geometría única.
    """
    import math as _math
    anot_path = folder / "anotaciones.json"
    if not anot_path.exists():
        return
    try:
        anot = json.loads(anot_path.read_text(encoding='utf-8'))
    except Exception as e:
        trabajo.advertencias.append(
            f"Postproc: anotaciones.json corrupto ({e}) — trazos del "
            f"operador NO disponibles")
        return
    if not isinstance(anot.get("paginas_anotadas"), dict):
        trabajo.advertencias.append(
            "Postproc: anotaciones.json con esquema inesperado — trazos del "
            "operador NO disponibles")
        return

    # Recolectar polilíneas cerradas de encimera (en píxeles) y trazos del
    # operador en píxeles (todo lo que afecta a la clasificación de aristas
    # o a la generación de cantos)
    encimera_polylines_pix = []
    paredes_pix = []
    ma_pix = []
    copetes_pix = []
    pulidos_pix = []
    ingletes_pix = []
    frontales_pix = []
    zocalos_pix = []
    pilares_pix = []
    huecos_pix = []
    for pid, pdata in (anot.get("paginas_anotadas") or {}).items():
        # Corregir la inclinación del escaneo ANTES de consumir los trazos
        # (rotación pura por página; los puntos se mutan in place)
        etiquetas_pts = [(et.get("tipo"), et.get("modo"),
                          bool(et.get("cerrado")), et.get("puntos"))
                         for et in pdata.get("etiquetas", [])
                         if et.get("puntos")]
        try:
            _desinclinar_pagina(etiquetas_pts)
        except Exception:
            pass
        for et in pdata.get("etiquetas", []):
            pts = et.get("puntos")
            if not pts:
                continue
            tipo = et.get("tipo")
            if (et.get("modo") == "poly" and et.get("cerrado")
                    and tipo == "encimera"):
                encimera_polylines_pix.append(pts)
            elif tipo == "pared":
                paredes_pix.append(pts)
            elif tipo == "muebles_altos":
                ma_pix.append(pts)
            elif tipo == "copete":
                copetes_pix.append(pts)
            elif tipo == "pulido":
                pulidos_pix.append(pts)
            elif tipo == "inglete":
                ingletes_pix.append(pts)
            elif tipo == "frontal":
                frontales_pix.append(pts)
            elif tipo == "zocalo":
                zocalos_pix.append(pts)
            elif tipo == "pilar":
                pilares_pix.append(pts)
            elif tipo == "hueco":
                huecos_pix.append(pts)

    if not encimera_polylines_pix:
        return
    # Si no hay ningún tipo de trazo guía, no hay nada que stashear
    if not (paredes_pix or ma_pix or copetes_pix or pulidos_pix or ingletes_pix
            or frontales_pix or zocalos_pix or pilares_pix or huecos_pix):
        return

    # Match polilínea↔encimera por área (estable, no depende de vertices_mm
    # snap-eados). Almacenamos en pixel-space; la conversión a mm-local se
    # hace ON-DEMAND en el reconciliador con los vertices actualizados.
    encimeras_validas = [p for p in trabajo.piezas
                         if p.tipo in ('encimera', 'isla') and p.vertices_mm
                         and len(p.vertices_mm) >= 3]

    def _bbox_area_px(poly):
        xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
        return (max(xs) - min(xs)) * (max(ys) - min(ys))

    def _geo_quant(e):
        # Clave cuantizada a rejilla de 200mm: las gemelas de opción pueden
        # haber divergido ligeramente (snaps con cotas asimétricas en JSON
        # reprocesados) y deben contar como la MISMA geometría
        return tuple((round(v[0] / 200.0), round(v[1] / 200.0))
                     for v in e.vertices_mm)

    encs_unicas = []
    seen_keys = set()
    for e in encimeras_validas:
        k = _geo_quant(e)
        if k in seen_keys:
            continue
        seen_keys.add(k)
        encs_unicas.append(e)

    encs_ord = sorted(encs_unicas, key=lambda e: -abs(_signed_area_2d(e.vertices_mm)))
    polys_ord = sorted(range(len(encimera_polylines_pix)),
                       key=lambda i: -_bbox_area_px(encimera_polylines_pix[i]))

    def _registro(poly_idx):
        return {
            'polilinea_pix': encimera_polylines_pix[poly_idx],
            'paredes_pix': paredes_pix,
            'ma_pix': ma_pix,
            'copetes_pix': copetes_pix,
            'pulidos_pix': pulidos_pix,
            'ingletes_pix': ingletes_pix,
            'frontales_pix': frontales_pix,
            'zocalos_pix': zocalos_pix,
            'pilares_pix': pilares_pix,
            'huecos_pix': huecos_pix,
        }

    # Matching por mejor pareja (greedy): se puntúa cada par encimera↔polilínea
    # por similitud de área NORMALIZADA (cada lado relativo a su máximo, para
    # poder comparar mm² con px²) con un bonus si coincide el nº de vértices.
    # Un desajuste de nº de vértices (Claude emite L de 6 y el operador dibujó
    # 4 puntos) ya NO deja a la encimera sin anotaciones: la conversión
    # píxel→mm es por bbox, no por vértice, así que sigue siendo válida.
    # El registro se guarda como atributo privado de la propia pieza
    # (pieza._anot_reg): viaja con el objeto, inmune al snap de vértices y a
    # filtrados/reordenados de trabajo.piezas. to_dict() ignora atributos «_».
    max_a_enc = max(abs(_signed_area_2d(e.vertices_mm)) for e in encs_ord) or 1
    max_a_pix = max(_bbox_area_px(encimera_polylines_pix[i]) for i in polys_ord) or 1
    pares = []
    for e in encs_ord:
        na_e = abs(_signed_area_2d(e.vertices_mm)) / max_a_enc
        for pi in polys_ord:
            na_p = _bbox_area_px(encimera_polylines_pix[pi]) / max_a_pix
            match_v = len(encimera_polylines_pix[pi]) == len(e.vertices_mm)
            score = abs(na_e - na_p) - (0.15 if match_v else 0.0)
            pares.append((score, id(e), e, pi, match_v))
    pares.sort(key=lambda t: (t[0], t[1], t[3]))
    usadas_e, usadas_p = set(), set()
    for score, eid, e, pi, match_v in pares:
        if eid in usadas_e or pi in usadas_p:
            continue
        usadas_e.add(eid)
        usadas_p.add(pi)
        e._anot_reg = _registro(pi)  # type: ignore[attr-defined]
        if not match_v:
            trabajo.advertencias.append(
                f"Anotaciones: polilínea de {len(encimera_polylines_pix[pi])} "
                f"puntos asignada por área a encimera de {len(e.vertices_mm)} "
                f"vértices ({e.zona or '?'})")
    # Las encimeras opcion1/opcion2 con misma geometría (cuantizada) que una
    # de las únicas comparten el mismo registro
    for enc in encimeras_validas:
        if getattr(enc, '_anot_reg', None) is not None:
            continue
        k_enc = _geo_quant(enc)
        for e2 in encs_unicas:
            if (_geo_quant(e2) == k_enc
                    and getattr(e2, '_anot_reg', None) is not None):
                enc._anot_reg = e2._anot_reg  # type: ignore[attr-defined]
                break


def _dist_punto_a_segmento(px, py, x1, y1, x2, y2):
    import math as _math
    dx = x2 - x1
    dy = y2 - y1
    L2 = dx * dx + dy * dy
    if L2 < 1:
        return _math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L2))
    cx = x1 + t * dx
    cy = y1 + t * dy
    return _math.hypot(px - cx, py - cy)


def _dist_minima_arista_a_trazos(p1, p2, trazos):
    """Distancia mínima entre el segmento p1→p2 y todos los puntos de los trazos."""
    md = float('inf')
    for trazo in trazos:
        for pt in trazo:
            d = _dist_punto_a_segmento(pt[0], pt[1], p1[0], p1[1], p2[0], p2[1])
            if d < md:
                md = d
    return md


def _path_length_mm(trazo):
    """Longitud total del trazo (mano alzada o polilínea), sumando segmentos."""
    import math as _math
    if not trazo or len(trazo) < 2:
        return 0.0
    total = 0.0
    for i in range(len(trazo) - 1):
        total += _math.hypot(trazo[i + 1][0] - trazo[i][0],
                             trazo[i + 1][1] - trazo[i][1])
    return total


def _completar_inglete_pata(trabajo: TrabajoExtraido) -> None:
    """Por cada pata (costado/cascada), genera automáticamente un canto
    `ingletado` con la cabeza de la encimera/isla a la que se une — ese
    inglete está implícito por la presencia de la pata y no requiere
    anotación magenta del operador.

    Longitud del inglete = largo de la pata (que coincide con el ancho de
    la cabeza de la encimera donde se une).

    Si el operador ya emitió un inglete con un trazo magenta cerca de la
    cabeza correspondiente, NO se duplica.
    """
    costados = [p for p in trabajo.piezas if p.tipo == 'costado']
    if not costados:
        return
    nuevos = 0
    for costado in costados:
        # Largo de la pata = cabeza encimera = mayor de las dos dimensiones
        # típicas (la otra es la altura de caída ~900mm).
        largo = costado.largo_mm or 0
        ancho = costado.ancho_mm or 0
        cabeza_mm = max(largo, ancho)
        if not cabeza_mm or cabeza_mm < 50:
            continue
        L_ml = round(cabeza_mm / 1000.0, 3)
        zona_corta = (costado.zona or '').split('(')[0].strip()[:35]

        # Evitar duplicar si ya hay un ingletado en la MISMA zona del costado
        # (caso típico: el operador ya marcó el inglete con un trazo magenta
        # cerca de esa cabeza). Dedup por zona en notas, no por longitud — dos
        # patas pueden tener el mismo tamaño y son ingletes independientes.
        ya = any(
            c.tipo == 'ingletado'
            and zona_corta and zona_corta in (c.notas or '').lower()
            for c in trabajo.cantos
        )
        if ya:
            continue

        trabajo.cantos.append(Canto(
            tipo='ingletado',
            longitud_ml=L_ml,
            notas=f"unión cabeza pata-encimera (auto) en {zona_corta}",
        ))
        nuevos += 1
    if nuevos:
        trabajo.advertencias.append(
            f"Postproc: {nuevos} inglete(s) pata-encimera auto-generados")


def _completar_pulidos_pata(trabajo: TrabajoExtraido) -> None:
    """Por cada pata (costado/cascada que cae desde encimera al suelo), emite
    automáticamente los pulidos de los "2 largos verticales" de la caída:

      - Pata sobre isla libre (sin paredes anotadas): 2 verticales pulidos
        (la pata se ve por ambas caras laterales).
      - Pata sobre encimera con un lado contra pared: 1 vertical pulido (el
        otro vertical queda contra muro, sin pulido).

    Se evita emitir si el operador ya marcó el pulido de la pata con un trazo
    cian en el plano (cuyo canto ya estaría en la lista con la nota
    «operador cian»).
    """
    costados = [p for p in trabajo.piezas if p.tipo == 'costado']
    if not costados:
        return
    for costado in costados:
        # Altura/caída: preferimos altura_mm; fallback al menor de largo/ancho
        altura = costado.altura_mm
        if not altura:
            largo = costado.largo_mm or 0
            ancho = costado.ancho_mm or 0
            if largo and ancho:
                altura = min(largo, ancho)
            else:
                altura = largo or ancho
        if not altura or altura < 50:
            continue
        L_ml = round(altura / 1000.0, 3)

        # Regla usuario (J0022): las caídas verticales de la pata van
        # pulidas EN LAS MISMAS DIRECCIONES que va pulida la isla — cada
        # caída corresponde a un LARGO de la isla; se pule si ese largo es
        # vista. Con clasificación de aristas disponible se usa esa verdad;
        # si no, fallback a la heurística pared-cerca.
        zona_costado = (costado.zona or '').lower()
        n_pulidos = None
        if 'isla' in zona_costado:
            islas = [p for p in trabajo.piezas
                     if p.tipo in ('encimera', 'isla')
                     and 'isla' in (p.zona or '').lower()
                     and p.vertices_mm]
            for isla in islas:
                tipos_ar = getattr(isla, '_aristas_tipos', None)
                if tipos_ar:
                    max_len = max(a['len'] for a in tipos_ar)
                    largos = [a for a in tipos_ar if a['len'] >= 0.7 * max_len]
                    n_pulidos = sum(1 for a in largos if a['tipo'] != 'pared')
                    n_pulidos = max(0, min(2, n_pulidos))
                    break
        if n_pulidos is None:
            # Fallback heurístico: isla libre (sin pared cerca) → 2 caídas
            es_isla_libre = False
            if 'isla' in zona_costado:
                islas = [p for p in trabajo.piezas
                         if p.tipo in ('encimera', 'isla')
                         and 'isla' in (p.zona or '').lower()
                         and p.vertices_mm]
                es_isla_libre = True  # asumimos libre y comprobamos
                for isla in islas:
                    regs = getattr(isla, '_anot_reg', None)
                    if not regs or not regs.get('paredes_pix'):
                        continue
                    paredes_mm = _trazos_a_mm_local(
                        regs['paredes_pix'], regs['polilinea_pix'], isla.vertices_mm)
                    if not paredes_mm:
                        continue
                    n_v = len(isla.vertices_mm)
                    tiene_pared_cerca = False
                    for i in range(n_v):
                        p1 = isla.vertices_mm[i]
                        p2 = isla.vertices_mm[(i + 1) % n_v]
                        if _dist_minima_arista_a_trazos(p1, p2, paredes_mm) <= 250:
                            tiene_pared_cerca = True
                            break
                    if tiene_pared_cerca:
                        es_isla_libre = False
                        break
            n_pulidos = 2 if es_isla_libre else 1
        if n_pulidos == 0:
            continue

        # Evitar duplicar si el operador ya emitió pulido para esta pata
        zona_corta = zona_costado.split('(')[0].strip()[:35]
        ya_marcado = any(
            c.tipo.startswith('recto_pulido')
            and abs((c.longitud_ml or 0) - L_ml) < 0.05
            and zona_corta and zona_corta in (c.notas or '').lower()
            for c in trabajo.cantos
        )
        if ya_marcado:
            continue

        for _ in range(n_pulidos):
            trabajo.cantos.append(Canto(
                tipo='recto_pulido',
                longitud_ml=L_ml,
                notas=f"vertical pata (auto, caída {altura:.0f}mm) en {zona_corta}",
            ))
        trabajo.advertencias.append(
            f"Postproc pata [{zona_corta}]: {n_pulidos} pulido(s) vertical(es) "
            f"de {altura:.0f}mm"
        )


def _ajustar_costado_cascada(trabajo: TrabajoExtraido) -> None:
    """Si una pieza `costado` (cascada lateral) está vinculada a una isla y
    su largo es ≈ el lado LARGO de la isla, suele ser error de Claude — la
    cascada lateral se hace típicamente sobre la CABEZA (lado corto) de la
    isla. Ajusta el largo del costado y normaliza el inglete asociado.
    """
    islas = [p for p in trabajo.piezas if p.tipo == 'isla' and p.largo_mm and p.ancho_mm]
    if not islas:
        return
    isla = max(islas, key=lambda p: (p.largo_mm or 0) * (p.ancho_mm or 0))
    cabeza = float(min(isla.largo_mm, isla.ancho_mm))
    largo = float(max(isla.largo_mm, isla.ancho_mm))
    if cabeza <= 0 or largo <= cabeza + 50:
        return  # isla cuadrada o casi: no hay distinción

    n_ajust = 0
    for p in trabajo.piezas:
        if p.tipo != 'costado' or not p.largo_mm:
            continue
        if abs(p.largo_mm - largo) <= 50 and abs(p.largo_mm - cabeza) > 50:
            old = p.largo_mm
            p.largo_mm = cabeza
            n_ajust += 1
            p.notas = ((p.notas or '') +
                       f' [postproc: cascada en cabeza, largo {old}→{cabeza:.0f}]').strip()

    if n_ajust:
        trabajo.advertencias.append(
            f"Postproc: {n_ajust} costado(s) ajustado(s) a cabeza isla "
            f"({cabeza:.0f}mm en lugar de {largo:.0f}mm)")
        # Ajustar ingletes que coincidan con el largo viejo (o múltiplo) — se
        # asume que un inglete a largo o 2×largo debería ser cabeza o 2×cabeza
        for c in trabajo.cantos:
            if c.tipo != 'ingletado' or not c.longitud_ml:
                continue
            L_mm = c.longitud_ml * 1000.0
            for k in (1, 2):
                if abs(L_mm - k * largo) <= 60:
                    nuevo = round(k * cabeza / 1000.0, 3)
                    c.longitud_ml = nuevo
                    c.notas = ((c.notas or '') +
                               f' [postproc: ajustado a cabeza isla]').strip()
                    trabajo.advertencias.append(
                        f"Postproc: inglete {L_mm:.0f}→{nuevo*1000:.0f}mm (cascada cabeza)")
                    break


def _dedup_frontales(trabajo: TrabajoExtraido) -> None:
    """Elimina frontales duplicados (largos a ±30mm Y alturas a ±50mm —
    dos frontales del mismo largo con alturas distintas son piezas DISTINTAS).
    Conserva el "más redondo" (múltiplo de 25mm); el otro se considera
    artefacto de medición."""
    frontales = [p for p in trabajo.piezas if p.tipo == 'frontal']
    if len(frontales) < 2:
        return
    def _redondez(v):
        if not v:
            return 0
        return 1 if (round(v) % 25 == 0) else 0
    def _alt(f):
        return f.altura_mm or f.ancho_mm or 0
    def _suf(f):
        rol = f.material_rol or ''
        return next((s for s in ('_opcion1', '_opcion2', '_opcion3',
                                 '_opcion_a', '_opcion_b') if s in rol), '')
    frontales_ord = sorted(frontales, key=lambda f: -(f.largo_mm or 0))
    usados = []
    eliminados = []
    for f in frontales_ord:
        L = f.largo_mm or 0
        duplicado = False
        for f2 in list(usados):
            L2 = f2.largo_mm or 0
            # Opciones de material distintas → piezas legítimas (la misma
            # cocina presupuestada en dos materiales), nunca duplicados
            if _suf(f) != _suf(f2):
                continue
            # Alturas distintas conocidas → piezas distintas, no dedup
            h1, h2 = _alt(f), _alt(f2)
            if h1 and h2 and abs(h1 - h2) > 50:
                continue
            if L > 0 and L2 > 0 and abs(L - L2) <= 30:
                if _redondez(L) > _redondez(L2):
                    usados.remove(f2)
                    usados.append(f)
                    eliminados.append(f2)
                else:
                    eliminados.append(f)
                duplicado = True
                break
        if not duplicado:
            usados.append(f)
    if eliminados:
        ids_eliminados = {id(e) for e in eliminados}
        trabajo.piezas = [p for p in trabajo.piezas
                          if not (p.tipo == 'frontal' and id(p) in ids_eliminados)]
        trabajo.advertencias.append(
            f"Postproc: dedup frontales — eliminados {len(eliminados)} duplicados "
            f"({', '.join(f'{e.largo_mm}mm' for e in eliminados)})")


def _ajustar_fondos_a_cotas(verts, cotas, cotas_propias):
    """Snap de DIFERENCIAS: el snap por coordenada absoluta no ve que la
    distancia entre dos líneas paralelas (el FONDO de un brazo o tramo)
    debía ser una cota — J0024: 2635−1750=885 cuando el fondo real era 900.
    Mueve solo líneas 'débiles' (no clavadas a una cota real) para que el
    hueco entre líneas consecutivas case con una cota o un fondo típico.
    Devuelve los vértices ajustados o None si no hubo cambios."""
    FONDOS_TIPICOS = (600, 650, 700, 900)
    reales = set(cotas or ()) | set(cotas_propias or ())
    objetivos = reales | set(FONDOS_TIPICOS)
    nuevos = [list(v) for v in verts]
    cambios = False
    for eje in (0, 1):
        for _ in range(3):  # hasta 3 correcciones por eje
            vals = sorted({v[eje] for v in nuevos})
            if len(vals) < 2:
                break
            pinned = {v for v in vals
                      if v == 0 or any(abs(v - c) <= 1 for c in reales)}
            hecho = False
            for a, b in zip(vals, vals[1:]):
                gap = b - a
                # Si el hueco YA coincide con una cota real, es correcto —
                # no convertir un escalón de 294 (cota) en 300 (J0020)
                if any(abs(c - gap) <= 1 for c in objetivos):
                    continue
                cands = [c for c in objetivos
                         if 1 < abs(c - gap) <= max(20, gap * 0.03)]
                if not cands:
                    continue
                target = min(cands, key=lambda c: abs(c - gap))
                delta = gap - target
                if b not in pinned:
                    mover, nuevo_v = b, b - delta
                elif a not in pinned and a != 0:
                    mover, nuevo_v = a, a + delta
                else:
                    continue
                # No reordenar líneas: el valor nuevo debe seguir entre
                # sus vecinas
                idx = vals.index(mover)
                lo = vals[idx - 1] if idx > 0 else float('-inf')
                hi = vals[idx + 1] if idx + 1 < len(vals) else float('inf')
                if not (lo < nuevo_v < hi):
                    continue
                for v in nuevos:
                    if v[eje] == mover:
                        v[eje] = nuevo_v
                cambios = True
                hecho = True
                break
            if not hecho:
                break
    return nuevos if cambios else None


def _escuadrar_poligono(verts, tol_ang=8.0):
    """Rectifica un polígono CASI ortogonal a ortogonal exacto. Las cocinas
    son rectilíneas: si todas las aristas están a ≤tol_ang° de un eje y
    alternan horizontal/vertical, cada arista se endereza promediando la
    coordenada que debería ser constante (el pulso del trazo del operador o
    la lectura de Claude meten derivas — J0024 salió «descuadrada»).
    Devuelve los vértices rectificados y trasladados a origen, o None si el
    polígono no es rectilíneo (chaflanes, curvas...). Para contornos píxel
    de croquis torcidos usar tol_ang≈20."""
    import math as _math
    n = len(verts)
    if n < 4:
        return None
    xs = [v[0] for v in verts]
    ys = [v[1] for v in verts]
    if (max(xs) - min(xs)) < 100 or (max(ys) - min(ys)) < 100:
        return None
    orient = []
    for i in range(n):
        dx = verts[(i + 1) % n][0] - verts[i][0]
        dy = verts[(i + 1) % n][1] - verts[i][1]
        L = _math.hypot(dx, dy)
        if L < 50:
            return None
        ang = _math.degrees(_math.atan2(abs(dy), abs(dx)))
        if ang <= tol_ang:
            orient.append('h')
        elif ang >= 90.0 - tol_ang:
            orient.append('v')
        else:
            return None  # arista en diagonal real — no rectificar
    if any(orient[i] == orient[(i + 1) % n] for i in range(n)):
        return None  # dos aristas seguidas en el mismo eje — caso raro
    nuevos = [list(v) for v in verts]
    for i in range(n):
        j = (i + 1) % n
        if orient[i] == 'h':
            ym = (verts[i][1] + verts[j][1]) / 2.0
            nuevos[i][1] = ym
            nuevos[j][1] = ym
        else:
            xm = (verts[i][0] + verts[j][0]) / 2.0
            nuevos[i][0] = xm
            nuevos[j][0] = xm
    mx = min(v[0] for v in nuevos)
    my = min(v[1] for v in nuevos)
    return [[v[0] - mx, v[1] - my] for v in nuevos]


def _reconciliar_geometria_encimera(trabajo: TrabajoExtraido) -> None:
    """Reconcilia frontales (chapeados) y pulidos contra la geometría real del
    polígono de TODAS las encimeras del trabajo (L principal, isla, costado,
    etc.), tratando cada una independientemente. Cuando hay anotaciones de
    pared/muebles_altos, se usan como verdad autoritativa para clasificar
    aristas; en su ausencia se cae a la heurística de vértices cóncavos.

    Pasos:
      a) Dedup de frontales por largo (artefactos de medición pixel).
      b) Snap de vértices de cada encimera a cotas conocidas.
      c) Por cada geometría única (opcion1/opcion2 son la misma):
         - Clasificar aristas como pared o frontal/cabeza.
         - Emitir un `recto_pulido` por cada arista no-pared.
         - Emitir un `ingletado` (0.6m) por cada vértice donde dos
           aristas no-pared se encuentran.
      d) Auto-añadir frontal contra la pared más larga si no existe.
    """
    encimeras = [p for p in trabajo.piezas
                 if p.tipo in ('encimera', 'isla') and p.vertices_mm and len(p.vertices_mm) >= 3]
    if not encimeras:
        return

    _dedup_frontales(trabajo)

    TOL_PARED_MM = 250
    TOL_PULIDO_MM = 200
    regs_todos = [getattr(p, '_anot_reg', None) for p in trabajo.piezas]
    hay_anotacion_pulido_global = any(
        r.get('pulidos_pix') for r in regs_todos if r)
    hay_anotacion_inglete_global = any(
        r.get('ingletes_pix') for r in regs_todos if r)

    # Limpiar pulidos viejos antes de regenerar (siempre los recalculamos)
    trabajo.cantos = [c for c in trabajo.cantos
                      if not c.tipo.startswith('recto_pulido')]
    # Limpiar ingletes auto-generados anteriores
    trabajo.cantos = [c for c in trabajo.cantos
                      if not (c.tipo == 'ingletado'
                              and any(k in (c.notas or '').lower()
                                      for k in ('implícito', 'geom auto', 'esquina vértice')))]
    # Si el operador anotó ingletes explícitos, su anotación es autoritativa:
    # eliminamos también los ingletes que emitió Claude para no duplicar.
    if hay_anotacion_inglete_global:
        trabajo.cantos = [c for c in trabajo.cantos if c.tipo != 'ingletado']

    geometrias_procesadas = []  # tuplas hashables de vértices ya procesados
    # Copetes de Claude ya usados como cobertura de alguna arista pared
    # (consumo GLOBAL entre encimeras: la afinidad textual de zona es frágil
    # — "encimera pared" vs "encimera de pared" — y sin este segundo nivel
    # se duplicaban copetes de cabeza)
    copetes_consumidos_global = set()
    geos_dudosas_avisadas = set()
    registros_procesados = {}  # id(_anot_reg) → encimera ya procesada

    for encimera in encimeras:
        # Escuadrar: las cocinas son ortogonales — el pulso del trazo o la
        # lectura de Claude meten derivas de 20-50mm por arista
        rect = _escuadrar_poligono(encimera.vertices_mm)
        if rect is not None:
            encimera.vertices_mm = rect
            xs_r = [v[0] for v in rect]
            ys_r = [v[1] for v in rect]
            encimera.largo_mm = float(max(xs_r))
            encimera.ancho_mm = float(max(ys_r))

        # Snap a cotas conocidas
        cotas, cotas_propias = _recolectar_cotas(trabajo, encimera)

        # Guard global de mutación: si la MAYORÍA de las aristas relevantes
        # YA coinciden con cotas propias (±5), la geometría es
        # cota-consistente (viene del solver o de un plano bien construido)
        # — reescalar/snapear la ROMPE (J0026: la línea x=2050, que no es
        # cota, saltaba a la cota 2200, que es una arista y no el ancho).
        geometria_consistente = False
        if cotas_propias and encimera.vertices_mm:
            import math as _math_gc
            vs_gc = encimera.vertices_mm
            n_gc = len(vs_gc)
            lens_gc = [_math_gc.hypot(vs_gc[(i + 1) % n_gc][0] - vs_gc[i][0],
                                      vs_gc[(i + 1) % n_gc][1] - vs_gc[i][1])
                       for i in range(n_gc)]
            relevantes_gc = [L for L in lens_gc if L >= 150]
            casadas_gc = [L for L in relevantes_gc
                          if any(abs(L - c) <= 5 for c in cotas_propias)]
            geometria_consistente = (len(relevantes_gc) >= 3
                                     and len(casadas_gc)
                                     >= 0.5 * len(relevantes_gc))

        # Reescalado a cotas propias: el croquis puede dibujar una pieza a
        # OTRA escala que el resto (J0026: isla de polilínea 900×525 con
        # cotas 1000×620 — fuera de la tolerancia del snap). Si ambos lados
        # del bbox casan con cotas propias distintas con factores de escala
        # similares, se reescala el polígono entero.
        if cotas_propias and encimera.vertices_mm and not geometria_consistente:
            xs_b = [v[0] for v in encimera.vertices_mm]
            ys_b = [v[1] for v in encimera.vertices_mm]
            W_b = max(xs_b) - min(xs_b)
            H_b = max(ys_b) - min(ys_b)
            if W_b > 100 and H_b > 100:
                cl = min(cotas_propias, key=lambda c: abs(c - W_b))
                ca = min((c for c in cotas_propias if c != cl),
                         key=lambda c: abs(c - H_b), default=None)
                if ca is not None:
                    fx = cl / W_b
                    fy = ca / H_b
                    if (0.8 <= fx <= 1.25 and 0.8 <= fy <= 1.25
                            and abs(fx - fy) <= 0.1
                            and (abs(fx - 1) > 0.02 or abs(fy - 1) > 0.02)):
                        x0_b, y0_b = min(xs_b), min(ys_b)
                        encimera.vertices_mm = [
                            [(v[0] - x0_b) * fx, (v[1] - y0_b) * fy]
                            for v in encimera.vertices_mm]
                        encimera.largo_mm = float(cl)
                        encimera.ancho_mm = float(ca)
                        trabajo.advertencias.append(
                            f"Postproc [{(encimera.zona or '?')[:35]}]: "
                            f"reescalado a cotas propias {cl:.0f}×{ca:.0f} "
                            f"(croquis a otra escala: ×{fx:.2f}/{fy:.2f})")
        if cotas and not geometria_consistente:
            verts_snap, snap_log = _snap_vertices_a_cotas(
                encimera.vertices_mm, cotas, cotas_propias)
            if snap_log['x'] or snap_log['y']:
                # Validación: el snap no puede degenerar el polígono.
                # Eliminar vértices duplicados consecutivos y comprobar que
                # el área se conserva razonablemente; si no, revertir.
                limpios = []
                for v in verts_snap:
                    if not limpios or (v[0] != limpios[-1][0] or v[1] != limpios[-1][1]):
                        limpios.append(v)
                if (len(limpios) > 1 and limpios[0][0] == limpios[-1][0]
                        and limpios[0][1] == limpios[-1][1]):
                    limpios.pop()
                area_orig = abs(_signed_area_2d(encimera.vertices_mm))
                area_snap = abs(_signed_area_2d(limpios)) if len(limpios) >= 3 else 0.0
                if len(limpios) < 3 or (area_orig > 0 and area_snap < 0.5 * area_orig):
                    trabajo.advertencias.append(
                        f"Postproc: snap revertido en {encimera.zona or '?'} — "
                        f"polígono degenerado ({len(limpios)} vértices, "
                        f"área {area_snap / 1e6:.2f}m² vs {area_orig / 1e6:.2f}m²)")
                else:
                    encimera.vertices_mm = limpios
                    xs = [v[0] for v in limpios]
                    ys = [v[1] for v in limpios]
                    encimera.largo_mm = float(max(xs) - min(xs))
                    encimera.ancho_mm = float(max(ys) - min(ys))

        zona_corta = (encimera.zona or '?').split('(')[0].strip()[:35]

        # Snap de FONDOS: las distancias entre líneas paralelas también
        # deben caer en cota (el snap absoluto no las ve)
        if cotas and not geometria_consistente:
            ajustado = _ajustar_fondos_a_cotas(encimera.vertices_mm,
                                               cotas, cotas_propias)
            if ajustado is not None:
                encimera.vertices_mm = ajustado
                xs_a = [v[0] for v in ajustado]
                ys_a = [v[1] for v in ajustado]
                encimera.largo_mm = float(max(xs_a) - min(xs_a))
                encimera.ancho_mm = float(max(ys_a) - min(ys_a))
                trabajo.advertencias.append(
                    f"Postproc [{zona_corta}]: fondo(s) ajustado(s) a cota "
                    f"(snap de diferencias)")

        # Validar la FORMA contra la polilínea del operador y, si no encaja,
        # RECONSTRUIR el polígono desde ella — el operador es la verdad.
        # Caza escalones con cotas intercambiadas (J0020 600/300), que las
        # comprobaciones de cotas no detectan.
        regs = getattr(encimera, '_anot_reg', None)
        # Procesar UNA sola vez por registro de anotaciones: las gemelas de
        # opción comparten registro, pero el snap con cotas_propias
        # asimétricas (las notas de la opción 2 no repiten las cotas) hacía
        # divergir sus geometrías → doble procesado y cantos duplicados
        # (J0024). La gemela adopta la geometría final de la primera.
        if regs is not None:
            ya = registros_procesados.get(id(regs))
            if ya is not None:
                encimera.vertices_mm = [list(v) for v in ya.vertices_mm]
                encimera.largo_mm = ya.largo_mm
                encimera.ancho_mm = ya.ancho_mm
                encimera.aristas_contacto = ya.aristas_contacto
                continue
        mapa_aristas = None
        if regs:
            d_forma, mapa_aristas = _alinear_poligono_con_polilinea(
                encimera.vertices_mm, regs.get('polilinea_pix'))
            if d_forma is not None and d_forma > 0.15:
                if _reconstruir_desde_polilinea(encimera, regs, cotas,
                                                cotas_propias, trabajo,
                                                zona_corta):
                    mapa_aristas = list(range(len(encimera.vertices_mm)))
                else:
                    geo_aviso = tuple(tuple(v) for v in encimera.vertices_mm)
                    if geo_aviso not in geos_dudosas_avisadas:
                        geos_dudosas_avisadas.add(geo_aviso)
                        trabajo.advertencias.append(
                            f"⚠ GEOMETRÍA DUDOSA [{zona_corta}]: el polígono "
                            f"no encaja con la polilínea del operador "
                            f"(desviación {d_forma * 100:.0f}% del bbox) y no "
                            f"se pudo reconstruir — revisar vértices contra "
                            f"el plano")
                    mapa_aristas = None
                    if d_forma > 0.45:
                        # Desajuste catastrófico: proyectar los trazos sobre
                        # esta geometría produce basura (pulidos de 80mm,
                        # huecos recolocados a ciegas — J0025). Los trazos
                        # NO se aplican; se usa aristas_contacto o heurística
                        # y se avisa para revisión manual.
                        encimera._anot_no_fiable = True  # type: ignore[attr-defined]
                        regs = None
                        trabajo.advertencias.append(
                            f"⚠ Postproc [{zona_corta}]: trazos del operador "
                            f"NO aplicados (geometría no casa) — revisar "
                            f"manualmente o re-anotar el contorno")
            else:
                # Forma OK: derivar el mapa con la MISMA transformación que
                # los demás trazos (la alineación normalizada es ambigua en
                # rectángulos y podría rotar el mapeo respecto a los huecos)
                mapa_cercania = _mapa_aristas_por_cercania(
                    regs, encimera.vertices_mm)
                if mapa_cercania is not None:
                    mapa_aristas = mapa_cercania

        # Dedup por geometría (opcion1/opcion2 misma forma → solo procesar una)
        geo_key = tuple(tuple(v) for v in encimera.vertices_mm)
        if geo_key in geometrias_procesadas:
            continue
        geometrias_procesadas.append(geo_key)
        if regs is not None:
            registros_procesados[id(regs)] = encimera

        aristas = _clasificar_aristas_encimera(encimera.vertices_mm)
        if not aristas:
            continue

        # Convertir trazos pixel → mm-local de ESTA encimera con los vertices
        # actualizados (post-snap/reconstrucción). El cargador almacenó en
        # pixel-space para que los vértices puedan moverse sin invalidar nada.
        if regs:
            cand_pix = regs['polilinea_pix']
            paredes_mm_local = _trazos_a_mm_local(regs['paredes_pix'], cand_pix, encimera.vertices_mm)
            ma_mm_local = _trazos_a_mm_local(regs['ma_pix'], cand_pix, encimera.vertices_mm)
            copetes_mm_local = _trazos_a_mm_local(regs['copetes_pix'], cand_pix, encimera.vertices_mm)
            pulidos_op_local = _trazos_a_mm_local(regs['pulidos_pix'], cand_pix, encimera.vertices_mm)
            ingletes_op_local = _trazos_a_mm_local(regs['ingletes_pix'], cand_pix, encimera.vertices_mm)
            frontales_mm_local = _trazos_a_mm_local(regs.get('frontales_pix', []), cand_pix, encimera.vertices_mm)
            zocalos_mm_local = _trazos_a_mm_local(regs.get('zocalos_pix', []), cand_pix, encimera.vertices_mm)
        else:
            paredes_mm_local = ma_mm_local = copetes_mm_local = []
            pulidos_op_local = ingletes_op_local = []
            frontales_mm_local = zocalos_mm_local = []
        # Una arista NO se pule si está cubierta por: pared, mueble alto,
        # copete, frontal/chapeado o zócalo. El pulido es lo COMPLEMENTARIO.
        # Precedencia de clasificación: trazos del operador (verdad absoluta)
        # > aristas_contacto emitida por Claude > heurística cóncava (último
        # recurso — invierte penínsulas y no sabe nada de rectángulos).
        trazos_no_pulir = (paredes_mm_local + ma_mm_local + copetes_mm_local
                           + frontales_mm_local + zocalos_mm_local)
        fuente_clasificacion = 'heuristica'
        tipos_px = (_clasificar_aristas_pixel(regs, encimera, mapa_aristas)
                    if regs else None)
        if tipos_px is not None:
            # Vía preferente: clasificación EN PÍXEL sobre el canvas real
            # (inmune a trazos de otras piezas y a errores de geometría)
            fuente_clasificacion = 'operador'
            n_override = sum(1 for t in tipos_px if t == 'pared')
            for a, t in zip(aristas, tipos_px):
                a['tipo'] = t
            trabajo.advertencias.append(
                f"Postproc [{zona_corta}]: aristas por trazos del operador "
                f"en píxel (pared:{n_override} "
                f"frente:{len(aristas) - n_override})")
        elif trazos_no_pulir:
            fuente_clasificacion = 'operador'
            n_override = 0
            n_libre = 0
            for a in aristas:
                p1 = encimera.vertices_mm[a['v1']]
                p2 = encimera.vertices_mm[a['v2']]
                d = _dist_minima_arista_a_trazos(p1, p2, trazos_no_pulir)
                if d <= TOL_PARED_MM:
                    a['tipo'] = 'pared'
                    n_override += 1
                else:
                    a['tipo'] = 'frontal'
                    n_libre += 1
            trabajo.advertencias.append(
                f"Postproc [{zona_corta}]: aristas (pared:{n_override} frente:{n_libre})"
                f"{' [+copetes]' if copetes_mm_local else ''}")
        elif (encimera.aristas_contacto
              and len(encimera.aristas_contacto) == len(aristas)):
            fuente_clasificacion = 'claude'
            n_pared = 0
            n_vista = 0
            for a, contacto in zip(aristas, encimera.aristas_contacto):
                if contacto in ('pared', 'mueble'):
                    a['tipo'] = 'pared'
                    n_pared += 1
                else:  # vista / ventana → arista pulible
                    a['tipo'] = 'frontal'
                    n_vista += 1
            trabajo.advertencias.append(
                f"Postproc [{zona_corta}]: aristas_contacto de Claude "
                f"(pared:{n_pared} vista:{n_vista})")
        elif not any(a['tipo'] in ('frontal', 'cabeza') for a in aristas):
            # Convexo sin información de paredes → no actuar
            continue
        else:
            trabajo.advertencias.append(
                f"⚠ Postproc [{zona_corta}]: clasificación de aristas por "
                f"heurística geométrica SIN confirmar (sin trazos del operador "
                f"ni aristas_contacto) — revisar pared/vista")

        # Guardar la clasificación final en la pieza para pasos posteriores
        # (p.ej. recolocar el grifo hacia la arista pared real — la
        # convención Y de Claude puede no coincidir con la del canvas)
        encimera._aristas_tipos = [  # type: ignore[attr-defined]
            {'v1': a['v1'], 'v2': a['v2'], 'tipo': a['tipo'], 'len': a['len']}
            for a in aristas]

        # Emitir pulidos. Si el operador anotó trazos de pulido cian para
        # ESTA encimera, son autoritativos: emitimos un canto por cada arista
        # cubierta por un trazo. Si no, fallback geométrico: aristas no-pared.
        nuevos_pulidos = 0
        # Track aristas cubiertas (operador pulido O inglete) — excluyen el
        # pulido automático. OJO: el inglete NO excluye copete (es la unión
        # en esquina de los copetes/chapeados, no un sustituto); solo el
        # pulido del operador hace la arista incompatible con copete.
        aristas_cubiertas = set()       # pulido O inglete → sin pulido auto
        aristas_pulido_op = set()       # SOLO pulido operador → sin copete
        TOL_PERTENENCIA_MM = 400

        if pulidos_op_local:
            # Cada trazo cian del operador → 1 arista (la más cercana al
            # CENTROIDE del trazo). El operador debe hacer un trazo separado
            # por cada arista que quiera pulida.
            for stroke in pulidos_op_local:
                if not stroke:
                    continue
                cx = sum(p[0] for p in stroke) / len(stroke)
                cy = sum(p[1] for p in stroke) / len(stroke)
                best_a = None
                best_d = float('inf')
                best_pared_d = float('inf')
                for a in aristas:
                    p1 = encimera.vertices_mm[a['v1']]
                    p2 = encimera.vertices_mm[a['v2']]
                    d = _dist_punto_a_segmento(cx, cy, p1[0], p1[1], p2[0], p2[1])
                    # Una arista PARED nunca se pule (J0026: el trazo cian
                    # caía sobre la pared del chapeado 795) — el pulido solo
                    # puede vivir en aristas vistas
                    if a['tipo'] == 'pared':
                        best_pared_d = min(best_pared_d, d)
                        continue
                    if d < best_d:
                        best_d = d
                        best_a = a
                if best_a is None or best_d > TOL_PERTENENCIA_MM:
                    if best_pared_d <= TOL_PERTENENCIA_MM:
                        trabajo.advertencias.append(
                            f"Postproc [{zona_corta}]: trazo de pulido del "
                            f"operador sobre arista PARED ignorado (las "
                            f"paredes con chapeado no se pulen)")
                    continue
                if best_a['idx'] in aristas_cubiertas:
                    continue
                aristas_cubiertas.add(best_a['idx'])
                aristas_pulido_op.add(best_a['idx'])
                if best_a['len'] >= 50:
                    trabajo.cantos.append(Canto(
                        tipo='recto_pulido',
                        longitud_ml=round(best_a['len'] / 1000.0, 3),
                        notas=f"arista idx={best_a['idx']} (operador cian) en {zona_corta}",
                    ))
                    nuevos_pulidos += 1

        # Emitir ingletes desde anotación del operador. Cada trazo magenta →
        # arista más cercana al centroide (pertenecía a esta encimera si
        # está dentro de tolerancia). Marca la arista como cubierta para que
        # NO emita un pulido auto sobre la misma esquina.
        nuevos_ingletes_op = 0
        ingletes_op_por_arista = {}  # idx → Canto emitido (reinterpretable)
        for stroke in ingletes_op_local:
            if not stroke or len(stroke) < 2:
                continue
            # Sin umbral de longitud: un tick de 2-3px ya es una marca
            # deliberada de inglete (el operador no pinta magenta por azar)
            L_total = _path_length_mm(stroke)
            cx = sum(p[0] for p in stroke) / len(stroke)
            cy = sum(p[1] for p in stroke) / len(stroke)
            best_a = None
            best_d = float('inf')
            for a in aristas:
                p1 = encimera.vertices_mm[a['v1']]
                p2 = encimera.vertices_mm[a['v2']]
                d = _dist_punto_a_segmento(cx, cy, p1[0], p1[1], p2[0], p2[1])
                if d < best_d:
                    best_d = d
                    best_a = a
            if best_a is None or best_d > TOL_PERTENENCIA_MM:
                continue
            aristas_cubiertas.add(best_a['idx'])
            # El trazo magenta SEÑALA la arista de unión; la longitud del
            # inglete es la de la arista (regla usuario: "va ingletado 1m de
            # la pata y 1m de la isla" = la cabeza), no la del trazo a mano.
            canto_ing = Canto(
                tipo='ingletado',
                longitud_ml=round(best_a['len'] / 1000.0, 3),
                notas=f"trazo magenta operador (arista idx={best_a['idx']}) "
                      f"en {zona_corta}",
            )
            trabajo.cantos.append(canto_ing)
            ingletes_op_por_arista[best_a['idx']] = canto_ing
            nuevos_ingletes_op += 1

        # ── Pulido por exclusión (regla universal) ──────────────────────
        # Una arista que NO ha sido marcada por trazo del operador (cyan
        # pulido, magenta inglete, ni clasificada como 'pared' por trazos
        # de pared/MA/copete/frontal/zocalo) → va vista → se pule. Es la
        # regla complementaria que el usuario describe: pulido = lo que
        # queda cuando se han excluido todos los demás tratamientos.
        for a in aristas:
            if a['idx'] in aristas_cubiertas:
                continue
            if a['tipo'] == 'pared':
                # Cubierta por algún trazo de no-pulir → no se pule
                continue
            if a['len'] < 50:
                continue
            aristas_cubiertas.add(a['idx'])
            trabajo.cantos.append(Canto(
                tipo='recto_pulido',
                longitud_ml=round(a['len'] / 1000.0, 3),
                notas=f"arista idx={a['idx']} (auto exclusión) en {zona_corta}",
            ))
            nuevos_pulidos += 1

        if nuevos_pulidos or nuevos_ingletes_op:
            trabajo.advertencias.append(
                f"Postproc [{zona_corta}]: {nuevos_pulidos} pulidos + "
                f"{nuevos_ingletes_op} ingletes operador")

        # ── Copete por exclusión (regla simétrica al pulido) ─────────────
        # Arista PARED/mueble sin chapeado ni copete que la cubra → copete.
        # Solo con clasificación FIABLE (operador o aristas_contacto de
        # Claude): la heurística geométrica inventa paredes (J0020) y
        # emitiría copetes falsos. Las opciones de material comparten
        # geometría → se emite el copete que falte en CADA opción (pool de
        # coberturas independiente por sufijo _opcionN).
        if fuente_clasificacion in ('operador', 'claude'):
            # Una arista con PULIDO del operador nunca lleva copete (vista);
            # el inglete sí es compatible (unión en esquina de copetes)
            aristas_pared = [a for a in aristas
                             if a['tipo'] == 'pared' and a['len'] >= 100
                             and a['idx'] not in aristas_pulido_op]
            # Gemelas por REGISTRO compartido (no por geometría: cuando se
            # procesa la primera opción, la gemela aún puede tener vértices
            # divergentes sin adoptar — J0024 dejaba su copete sin ajustar)
            encs_geo = [e for e in encimeras
                        if tuple(tuple(v) for v in e.vertices_mm) == geo_key
                        or (regs is not None
                            and getattr(e, '_anot_reg', None) is regs)]
            sufijos = []
            for e in encs_geo:
                suf = ''
                for s in ('_opcion1', '_opcion2', '_opcion3',
                          '_opcion_a', '_opcion_b'):
                    if s in (e.material_rol or ''):
                        suf = s
                        break
                if suf not in sufijos:
                    sufijos.append(suf)
            # Afinidad de zona: los copetes solo cubren aristas de SU
            # encimera (las dos primeras palabras de la zona de la encimera
            # deben aparecer en la zona del copete) — sin esto, un copete de
            # la península "cubriría" la cabeza de otra encimera del mismo
            # largo. Los frontales cubren por longitud (su zona no suele
            # nombrar a la encimera). Si el trabajo solo tiene UNA geometría
            # de encimera, todos los copetes son suyos (la afinidad textual
            # es frágil: "encimera principal" vs "…superior encimera").
            zona_tokens = ' '.join((zona_corta or '').lower().split()[:2])
            geos_unicas = {tuple(tuple(v) for v in p.vertices_mm)
                           for p in encimeras}
            unica_encimera = len(geos_unicas) == 1

            def _es_de_esta_encimera(p):
                if unica_encimera:
                    return True
                return zona_tokens and zona_tokens in (p.zona or '').lower()

            # Aristas pared cubiertas por CHAPEADO (frontal): alimenta el
            # inglete automático de esquinas reflejas (J0026: 795/225)
            frontales_en_arista: dict = {}

            for suf in sufijos:
                def _de_opcion(p):
                    rol = p.material_rol or ''
                    if suf:
                        return suf in rol
                    return not any(s in rol for s in
                                   ('_opcion1', '_opcion2', '_opcion3',
                                    '_opcion_a', '_opcion_b'))

                def _longitud(p):
                    return ((p.longitud_ml or 0) * 1000) or (p.largo_mm or 0)

                def _es_cobertura(p):
                    # Las piezas generadas por el PROPIO postproc no cubren
                    # aristas de otras encimeras (el copete auto del tramo
                    # superior se tragaba la cabeza del tramo inferior)
                    return 'postproc' not in (p.notas or '').lower()

                pool_copetes = [(_longitud(p), p) for p in trabajo.piezas
                                if p.tipo == 'copete' and _de_opcion(p)
                                and _es_de_esta_encimera(p) and _longitud(p)
                                and _es_cobertura(p)
                                and id(p) not in copetes_consumidos_global]
                # 2º nivel: copetes de la opción NO afines y aún sin consumir
                # en ninguna encimera (red de seguridad de la afinidad textual)
                pool_copetes_global = [
                    (_longitud(p), p) for p in trabajo.piezas
                    if p.tipo == 'copete' and _de_opcion(p)
                    and not _es_de_esta_encimera(p) and _longitud(p)
                    and _es_cobertura(p)
                    and id(p) not in copetes_consumidos_global]
                pool_frontales = [(_longitud(p), p) for p in trabajo.piezas
                                  if p.tipo == 'frontal' and _de_opcion(p)
                                  and _longitud(p) and _es_cobertura(p)]
                ref_copete = next((p for p in trabajo.piezas
                                   if p.tipo == 'copete' and _de_opcion(p)),
                                  None)
                # FASE 1 — matches exactos globales (≤10mm) ANTES de ningún
                # subset-sum: la pared grande no puede robarle al escalón sus
                # chapeados exactos (J0026: 795+225+1255 sumaban ≈2200 y el
                # subset de la arista 2200 se los tragaba)
                # (solo chapeados: los copetes conservan el camino original
                # — su orden de consumo entre gemelas ya está validado)
                cubiertas_exactas = set()
                for a in sorted(aristas_pared, key=lambda x: -x['len']):
                    m = next(((L, p) for L, p in pool_frontales
                              if abs(L - a['len']) <= 5), None)
                    if m is None:
                        continue
                    pool_frontales.remove(m)
                    frontales_en_arista.setdefault(a['idx'], []).append(m[1])
                    cubiertas_exactas.add(a['idx'])
                for a in sorted(aristas_pared, key=lambda x: -x['len']):
                    if a['idx'] in cubiertas_exactas:
                        continue
                    tol = max(150, a['len'] * 0.2)
                    cubierta = False
                    for pool in (pool_copetes, pool_copetes_global,
                                 pool_frontales):
                        match = next(((L, p) for L, p in pool
                                      if abs(L - a['len']) <= tol), None)
                        if match is not None:
                            # Solo el match individual EXACTO (≤5mm) gana
                            # directamente; los aproximados se deciden abajo
                            # con subset-sum (J0025: la trasera 2760 casaba
                            # con el frontal 2680 d=80 cuando 2312+448 era
                            # exacta)
                            if abs(match[0] - a['len']) > 5:
                                match = None
                            if match is not None:
                                pool.remove(match)
                                if match[1].tipo == 'copete':
                                    copetes_consumidos_global.add(id(match[1]))
                                elif match[1].tipo == 'frontal':
                                    frontales_en_arista.setdefault(
                                        a['idx'], []).append(match[1])
                                cubierta = True
                                break
                    if cubierta:
                        continue
                    # Cobertura por TRAMOS (subset-sum): el MEJOR subconjunto
                    # de piezas cuya suma aproxima la arista (no greedy: el
                    # primer casi-acierto puede robarle la pieza a otra
                    # arista — J0025)
                    candidatas_suma = [
                        (L, p, pool) for pool in (pool_copetes,
                                                  pool_copetes_global,
                                                  pool_frontales)
                        for (L, p) in pool if L <= a['len'] * 1.1][:12]
                    mejor_sub = None  # (|d|, n_piezas, subset)
                    nc = len(candidatas_suma)
                    for mask in range(1, 1 << nc):
                        suma = 0.0
                        items = []
                        for b in range(nc):
                            if mask & (1 << b):
                                suma += candidatas_suma[b][0]
                                items.append(candidatas_suma[b])
                        if suma > a['len'] * 1.1:
                            continue
                        d = abs(suma - a['len'])
                        clave = (d, len(items))
                        if mejor_sub is None or clave < mejor_sub[0]:
                            mejor_sub = (clave, items)
                    if mejor_sub and mejor_sub[0][0] <= max(150, a['len'] * 0.1):
                        for L, p, pool in mejor_sub[1]:
                            pool.remove((L, p))
                            if p.tipo == 'frontal':
                                frontales_en_arista.setdefault(
                                    a['idx'], []).append(p)
                            if p.tipo == 'copete':
                                copetes_consumidos_global.add(id(p))
                                if len(mejor_sub[1]) == 1:
                                    L_arista = round(a['len'] / 1000.0, 3)
                                    if abs((p.longitud_ml or 0) - L_arista) > 0.005:
                                        p.longitud_ml = L_arista
                                        p.notas = ((p.notas or '')
                                                   + f' [ajustado a arista '
                                                     f'{L_arista}ml]').strip()
                        continue
                    # La cobertura PARCIAL solo con FRONTALES: un chapeado
                    # por tramos es habitual (pilar), pero un copete de
                    # cabeza nunca es "tramo" de la trasera (J0021)
                    usadas_suma = [(L, p, pool) for (L, p, pool)
                                   in (mejor_sub[1] if mejor_sub else [])
                                   if p.tipo == 'frontal']
                    suma = sum(L for L, _, _ in usadas_suma)
                    if usadas_suma and suma >= 300:
                        # Cobertura PARCIAL por tramos (J0024: trasera 4725
                        # con frontal de 2100 a la derecha): se consume lo
                        # cubierto y el copete se emite solo por el RESTO
                        for L, p, pool in usadas_suma:
                            pool.remove((L, p))
                            if p.tipo == 'copete':
                                copetes_consumidos_global.add(id(p))
                        resto = a['len'] - suma
                        if resto < 100:
                            continue
                        L_ml = round(resto / 1000.0, 3)
                        trabajo.piezas.append(Pieza(
                            tipo='copete',
                            material_rol=(ref_copete.material_rol if ref_copete
                                          else f'copete{suf}'),
                            longitud_ml=L_ml,
                            altura_mm=(ref_copete.altura_mm if ref_copete
                                       and ref_copete.altura_mm else 50.0),
                            zona=f'copete en {zona_corta} (arista idx={a["idx"]}, '
                                 f'tramo sin chapeado)',
                            notas=f'Auto exclusión pared (postproc, fuente '
                                  f'{fuente_clasificacion}) — resto tras '
                                  f'{round(suma)}mm cubiertos por tramos',
                        ))
                        trabajo.advertencias.append(
                            f"Postproc [{zona_corta}]: añadido copete {L_ml}ml "
                            f"en arista pared idx={a['idx']} (resto sin "
                            f"chapeado){suf or ''}")
                        continue
                    L_ml = round(a['len'] / 1000.0, 3)
                    trabajo.piezas.append(Pieza(
                        tipo='copete',
                        material_rol=(ref_copete.material_rol if ref_copete
                                      else f'copete{suf}'),
                        longitud_ml=L_ml,
                        altura_mm=(ref_copete.altura_mm if ref_copete
                                   and ref_copete.altura_mm else 50.0),
                        zona=f'copete en {zona_corta} (arista idx={a["idx"]})',
                        notas=f'Auto exclusión pared (postproc, fuente '
                              f'{fuente_clasificacion})',
                    ))
                    trabajo.advertencias.append(
                        f"Postproc [{zona_corta}]: añadido copete {L_ml}ml "
                        f"en arista pared idx={a['idx']} sin chapeado"
                        f"{suf or ''}")
                # Copetes fantasma: los copetes de Claude de ESTA encimera que
                # quedaron sin consumir y no casan con NINGUNA arista pared
                # reclaman una arista vista → eliminarlos (solo con
                # clasificación del operador, la más fiable).
                if fuente_clasificacion == 'operador':
                    for L_p, pieza_cop in pool_copetes:
                        if any(abs(L_p - a['len']) <= max(150, a['len'] * 0.2)
                               for a in aristas_pared):
                            continue
                        if pieza_cop in trabajo.piezas:
                            trabajo.piezas.remove(pieza_cop)
                            trabajo.advertencias.append(
                                f"Postproc [{zona_corta}]: eliminado copete "
                                f"fantasma {round(L_p / 1000.0, 3)}ml{suf or ''} "
                                f"— reclama una arista vista/pulida")

            # Inglete automático entre chapeados que doblan una esquina
            # REFLEJA (escalón/columna — J0026: 795/225). En una esquina
            # normal de cocina (90° interior) los chapeados se juntan a
            # testa, sin inglete: solo la esquina que ENVUELVE material
            # (ángulo interior 270°) deja canto visto que se ingleta.
            if frontales_en_arista and len(encimera.vertices_mm) >= 3:
                verts_e = encimera.vertices_mm
                sentido = 1 if _signed_area_2d(verts_e) > 0 else -1
                por_idx = {a['idx']: a for a in aristas_pared}
                op_inglete = {k for k in aristas_cubiertas
                              if k not in aristas_pulido_op}
                hechas = set()
                for i in sorted(frontales_en_arista):
                    for j in sorted(frontales_en_arista):
                        if j <= i or (i, j) in hechas:
                            continue
                        a1, a2 = por_idx.get(i), por_idx.get(j)
                        if not a1 or not a2:
                            continue
                        if a1['v2'] == a2['v1']:
                            vp, vc, vn = (verts_e[a1['v1']],
                                          verts_e[a1['v2']],
                                          verts_e[a2['v2']])
                        elif a2['v2'] == a1['v1']:
                            vp, vc, vn = (verts_e[a2['v1']],
                                          verts_e[a2['v2']],
                                          verts_e[a1['v2']])
                        else:
                            continue  # no comparten vértice
                        cross = ((vc[0] - vp[0]) * (vn[1] - vc[1])
                                 - (vc[1] - vp[1]) * (vn[0] - vc[0]))
                        if cross * sentido >= 0:
                            continue  # esquina normal → a testa
                        # Si el operador marcó inglete en una de las dos
                        # aristas, su marca señala ESTA unión — pero el
                        # inglete entre chapeados corre por la ALTURA de
                        # los dos chapeados (2 cantos verticales), no por
                        # el largo de la arista (J0026: 0.795 → 2×0.6)
                        canto_op = (ingletes_op_por_arista.get(i)
                                    or ingletes_op_por_arista.get(j))
                        if canto_op is not None and canto_op in trabajo.cantos:
                            trabajo.cantos.remove(canto_op)
                            trabajo.advertencias.append(
                                f"Postproc [{zona_corta}]: inglete del "
                                f"operador reinterpretado — la unión de "
                                f"chapeados en esquina va por su altura")
                        elif i in op_inglete or j in op_inglete:
                            continue  # inglete op de otra encimera/unión
                        hechas.add((i, j))
                        alt = max((p.altura_mm or 600.0)
                                  for p in (frontales_en_arista[i]
                                            + frontales_en_arista[j]))
                        L_ing = round(2 * alt / 1000.0, 3)
                        trabajo.cantos.append(Canto(
                            tipo='ingletado', longitud_ml=L_ing,
                            notas=f'unión chapeados en esquina refleja '
                                  f'aristas idx={i}/{j} de {zona_corta}: '
                                  f'2 cantos × {alt:.0f}mm (postproc)'))
                        trabajo.advertencias.append(
                            f"Postproc [{zona_corta}]: inglete {L_ing}ml "
                            f"entre chapeados de aristas idx={i}/{j} "
                            f"(esquina refleja)")

            # Cabeza VISTA de chapeado se pule (Regla A, T5551 — J0028: el
            # frontis de 2463 termina en el extremo abierto de la cocina):
            # si una arista con chapeado tiene una arista VECINA vista, la
            # cabeza del chapeado queda expuesta en esa esquina → canto
            # pulido de su altura.
            if frontales_en_arista:
                n_ar = len(encimera.vertices_mm)
                tipo_por_idx = {a['idx']: a['tipo'] for a in aristas}
                for i, chapas in frontales_en_arista.items():
                    for vecina in ((i - 1) % n_ar, (i + 1) % n_ar):
                        if tipo_por_idx.get(vecina, 'pared') == 'pared':
                            continue
                        alt_ch = max((p.altura_mm or 600.0) for p in chapas)
                        L_cab = round(alt_ch / 1000.0, 3)
                        marca = (f'cabeza vista de chapeado arista idx={i} '
                                 f'lado idx={vecina}')
                        if any(marca in (c.notas or '') for c in trabajo.cantos):
                            continue
                        trabajo.cantos.append(Canto(
                            tipo='recto_pulido', longitud_ml=L_cab,
                            notas=f'{marca} en {zona_corta}: extremo '
                                  f'expuesto del frontis (postproc)'))
                        trabajo.advertencias.append(
                            f"Postproc [{zona_corta}]: pulido {L_cab}ml en "
                            f"cabeza vista del chapeado (arista idx={i})")

            # Patrón "frontal alto parte la pared" (J0025: 600−243; J0026:
            # 2410−300): si existe un frontal ALTO y un chapeado estándar
            # mide EXACTAMENTE una arista pared completa, el reparto es
            # sospechoso — el chapeado real suele ser pared_total − alto.
            altos = [p for p in trabajo.piezas
                     if p.tipo == 'frontal' and (p.altura_mm or 0) >= 800
                     and 'postproc' not in (p.notas or '').lower()]
            # Si algún chapeado estándar YA es "cota − alto" (el diseño hizo
            # la resta: J0025 2312=2760−448, J0026 corregido 2110=2410−300),
            # el reparto está resuelto → sin aviso.
            reparto_hecho = any(
                abs((p.largo_mm or 0) - (c - (alto.largo_mm or 0))) <= 25
                for p in trabajo.piezas
                if p.tipo == 'frontal' and (p.altura_mm or 600) < 800
                for alto in altos
                for c in (list(cotas_propias or ())
                          + [a['len'] for a in aristas_pared]))
            if altos and not reparto_hecho:
                for a in aristas_pared:
                    chap = next(
                        (p for p in trabajo.piezas if p.tipo == 'frontal'
                         and (p.altura_mm or 600) < 800
                         and abs((p.largo_mm or 0) - a['len']) <= 5), None)
                    if chap:
                        alto = altos[0]
                        trabajo.advertencias.append(
                            f"⚠ [{zona_corta}]: chapeado {chap.largo_mm:.0f} "
                            f"ocupa la arista pared idx={a['idx']} completa "
                            f"pero existe frontal alto {alto.largo_mm or 0:.0f}"
                            f"×{alto.altura_mm or 0:.0f} — si comparten pared, "
                            f"el chapeado suele ser pared_total − "
                            f"{alto.largo_mm or 0:.0f} (patrón J0025/J0026) — "
                            f"VERIFICAR reparto")
                        break

    # NOTA: la heurística de "auto-frontal contra la pared más larga" se
    # eliminó (2026-06-11): inventó chapeados en J0014 y J0020 porque la
    # clasificación geométrica de paredes no es fiable. Un frontal solo
    # existe si lo emite Claude (plano/plantilla) o si el operador lo traza
    # (lo crea _completar_piezas_desde_trazos).


def _completar_ingletes_implicitos(trabajo: TrabajoExtraido) -> None:
    """Añade ingletes implícitos en post-procesado (geometría, no LLM).

    Regla: por cada pieza horizontal (encimera/isla/costado) que tenga `cabeza
    vista` y canto pulido tanto en el frontal como en la cabeza, la unión
    frontal-cabeza se ejecuta con inglete. Idem para frontal/copete/zócalo
    cuando envuelven la cabeza del mueble.

    Detección de "cabeza vista": cantos `recto_pulido` cuya zona o notas
    mencionen "cabeza" o "lateral". Cada uno → 1 inglete con longitud = fondo
    de la encimera (típico 0.6m, leído del bbox).
    """
    if not trabajo.cantos:
        return

    # Fondo (ancho) de referencia: el más común entre las encimeras
    fondos = [p.ancho_mm for p in trabajo.piezas
              if p.tipo == 'encimera' and p.ancho_mm]
    fondo_ref_m = (max(set(fondos), key=fondos.count) / 1000.0) if fondos else 0.6

    nuevos: list[Canto] = []
    for c in trabajo.cantos:
        if c.tipo != 'recto_pulido':
            continue
        notas_lower = (c.notas or '').lower()
        if 'cabeza' not in notas_lower and 'lateral' not in notas_lower:
            continue
        # Los pulidos por exclusión cubren TODAS las aristas vistas; generar
        # un inglete por cada uno fabricaba ingletes fantasma (J0017/J0018).
        # Solo los pulidos del operador o con cabeza explícita de Claude
        # implican unión ingletada frontal-cabeza.
        if 'auto exclusión' in notas_lower or 'auto exclusion' in notas_lower:
            continue
        # Dedup estructural: el inglete embebe las notas completas del pulido
        # que lo originó — buscar la nota ÍNTEGRA, no un prefijo de 25 chars
        # (dos cabezas distintas pueden compartir prefijo).
        ya_existe = any(
            (c2.tipo == 'ingletado'
             and notas_lower and notas_lower in (c2.notas or '').lower())
            for c2 in trabajo.cantos + nuevos
        )
        if ya_existe:
            continue
        nuevos.append(Canto(
            tipo='ingletado',
            longitud_ml=round(fondo_ref_m, 3),
            notas=f'Inglete implícito frontal-cabeza · {c.notas or ""}'.strip(),
        ))

    if nuevos:
        trabajo.cantos.extend(nuevos)


def _punto_en_poligono(px, py, verts) -> bool:
    """Ray casting estándar."""
    n = len(verts)
    dentro = False
    j = n - 1
    for i in range(n):
        xi, yi = verts[i]
        xj, yj = verts[j]
        if ((yi > py) != (yj > py)) and \
                (px < (xj - xi) * (py - yi) / ((yj - yi) or 1e-9) + xi):
            dentro = not dentro
        j = i
    return dentro


def _alinear_poligono_con_polilinea(verts, poly):
    """Busca la mejor correspondencia entre los vértices del polígono (mm) y
    los puntos de la polilínea del operador (px): toda rotación cíclica y
    sentido de recorrido, SIN espejo — intercambiar las cotas de un escalón
    equivale a un espejo vertical y es justo el error a detectar (convención
    confirmada con J0020: el mapeo Y px↔mm es directo).

    Devuelve (d_min, mapa_aristas): d_min es la desviación máxima entre
    vértices emparejados (fracción del bbox unitario) y mapa_aristas[i] = el
    índice de la arista de la polilínea que corresponde a la arista i del
    polígono. (None, None) si no hay correspondencia 1:1 de vértices."""
    import math as _math
    if not poly or not verts or len(poly) != len(verts) or len(verts) < 4:
        return None, None

    def _norm(pts):
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        w = (max(xs) - min(xs)) or 1
        h = (max(ys) - min(ys)) or 1
        return [((p[0] - min(xs)) / w, (p[1] - min(ys)) / h) for p in pts]

    a = _norm(verts)
    n = len(a)
    b0 = _norm(poly)
    mejor = (float('inf'), 0, False)
    for rev in (False, True):
        b = list(reversed(b0)) if rev else b0
        for off in range(n):
            d = max(_math.hypot(a[i][0] - b[(i + off) % n][0],
                                a[i][1] - b[(i + off) % n][1])
                    for i in range(n))
            if d < mejor[0]:
                mejor = (d, off, rev)
    d_min, off, rev = mejor
    mapa = []
    for i in range(n):
        j = (i + off) % n
        # sin reversa: vértice i ↔ poly[j] → arista i ↔ arista j de poly.
        # con reversa: vértice i ↔ poly[n-1-j] → la arista i une los puntos
        # poly[n-1-j] y poly[n-2-j] → arista (n-2-j) % n de poly.
        mapa.append(j if not rev else (n - 2 - j) % n)
    return d_min, mapa


def _resolver_poligono_por_cierre(poly, encimera, cotas, cotas_propias):
    """Resuelve el polígono de un croquis TORCIDO o NO A ESCALA:
    la topología (secuencia de direcciones de arista) viene del contorno del
    operador rectificado (≤20°); las medidas vienen de las COTAS. Por cada
    eje se asignan cotas a las aristas (candidatas por cercanía al tamaño
    dibujado) exigiendo el CIERRE del polígono (Σ positivas = Σ negativas),
    con ≤1 arista libre por eje cuya longitud se deriva del cierre. Las
    `aristas_cota` declaradas por Claude (agrupadas por eje) desempatan los
    casos en que el dibujo engaña (un fondo de 300 dibujado como 850).
    Devuelve los vértices o None si no hay solución con confianza."""
    import itertools
    import math as _math

    poly_rect = _escuadrar_poligono(poly, tol_ang=20.0)
    if poly_rect is None:
        return None
    n = len(poly_rect)
    edges = []
    for i in range(n):
        dx = poly_rect[(i + 1) % n][0] - poly_rect[i][0]
        dy = poly_rect[(i + 1) % n][1] - poly_rect[i][1]
        if abs(dx) >= abs(dy):
            edges.append(('x', 1 if dx > 0 else -1, abs(dx)))
        else:
            edges.append(('y', 1 if dy > 0 else -1, abs(dy)))

    reales = {float(c) for c in (set(cotas or ()) | set(cotas_propias or ()))
              if c >= 150}
    if encimera.aristas_cota:
        reales |= {float(c) for c in encimera.aristas_cota
                   if c and c >= 150}
    if len(reales) < 2:
        return None

    # Cotas declaradas por Claude agrupadas por el EJE de sus aristas (si su
    # polígono es clasificable) — desempate semántico
    declaradas_eje = {'x': set(), 'y': set()}
    if encimera.aristas_cota and encimera.vertices_mm \
            and len(encimera.aristas_cota) == len(encimera.vertices_mm):
        vc = encimera.vertices_mm
        for i, c in enumerate(encimera.aristas_cota):
            if not c or c < 150:
                continue
            dx = vc[(i + 1) % len(vc)][0] - vc[i][0]
            dy = vc[(i + 1) % len(vc)][1] - vc[i][1]
            declaradas_eje['x' if abs(dx) >= abs(dy) else 'y'].add(float(c))

    # Escala inicial para el ranking (votación de ratios cota/arista_px)
    ratios = []
    for _, _, L in sorted(edges, key=lambda e: -e[2])[:4]:
        for c in reales:
            r = c / L
            if 1.0 <= r <= 15.0:
                ratios.append(r)
    if not ratios:
        return None
    ratios.sort()
    mejor_r, mejor_n = ratios[0], 1
    for r in ratios:
        grupo = [x for x in ratios if abs(x - r) <= r * 0.06]
        if len(grupo) > mejor_n:
            mejor_n, mejor_r = len(grupo), sum(grupo) / len(grupo)
    s0 = mejor_r

    asignacion = [None] * n
    for eje in ('x', 'y'):
        idxs = [i for i in range(n) if edges[i][0] == eje]
        if not idxs or len(idxs) > 6:
            return None
        cand = {}
        for i in idxs:
            L_est = edges[i][2] * s0
            cercanas = sorted(reales, key=lambda c: abs(c - L_est))[:3]
            cand[i] = [('c', c) for c in cercanas] + [('libre', None)]
        mejor = None  # (score, {idx: longitud})
        for combo in itertools.product(*(cand[i] for i in idxs)):
            libres = [i for i, (t, _) in zip(idxs, combo) if t == 'libre']
            if len(libres) > 1:
                continue
            suma = 0.0
            for i, (t, c) in zip(idxs, combo):
                if t == 'c':
                    suma += edges[i][1] * c
            largos = {i: c for i, (t, c) in zip(idxs, combo) if t == 'c'}
            if libres:
                i_l = libres[0]
                valor = -suma / edges[i_l][1]
                if valor < 150:
                    continue
                largos[i_l] = valor
            elif abs(suma) > 5:
                continue
            score = 0.0
            for i, (t, c) in zip(idxs, combo):
                if t == 'c':
                    L_est = edges[i][2] * s0
                    score += 1.0 / (1.0 + abs(c - L_est) / 300.0)
                    # La cota declarada por Claude EN ESTE EJE pesa más que
                    # el parecido al dibujo: los croquis no están a escala
                    # (un fondo de 300 puede venir dibujado como 680)
                    if c in declaradas_eje[eje]:
                        score += 0.8
                else:
                    score += 0.3
            if mejor is None or score > mejor[0]:
                mejor = (score, largos)
        if mejor is None or mejor[0] / len(idxs) < 0.5:
            return None
        for i, L in mejor[1].items():
            asignacion[i] = L

    # Reconstrucción tipo tortuga desde la topología
    x = y = 0.0
    pts = []
    for i in range(n):
        pts.append([x, y])
        eje_i, sg, _ = edges[i]
        if eje_i == 'x':
            x += sg * asignacion[i]
        else:
            y += sg * asignacion[i]
    mx = min(p[0] for p in pts)
    my = min(p[1] for p in pts)
    pts = [[round(p[0] - mx, 1), round(p[1] - my, 1)] for p in pts]
    if abs(_signed_area_2d(pts)) < 1e5:  # < 0.1 m²
        return None
    return pts


def _reconstruir_desde_polilinea(encimera, regs, cotas, cotas_propias,
                                 trabajo, zona_corta,
                                 forzar_cierre=False) -> bool:
    """Reemplaza vertices_mm por la geometría REAL que dibujó el operador.
    Los puntos de la polilínea se agrupan por eje (clustering — el trazo es
    a mano alzada), se escalan al bbox en mm de la pieza (el bbox de Claude
    es fiable: cuadra con las cotas) con mapeo Y DIRECTO px↔mm y se snapean
    a cotas. Se usa cuando el polígono de Claude no encaja con la polilínea
    (escalón con cotas intercambiadas, J0020). Tras reconstruir, el vértice
    i corresponde al punto i de la polilínea (identidad)."""
    poly = regs.get('polilinea_pix')
    verts = encimera.vertices_mm
    if not poly or len(poly) != len(verts):
        return False
    xs_m = [v[0] for v in verts]
    ys_m = [v[1] for v in verts]
    xs_p = [p[0] for p in poly]
    ys_p = [p[1] for p in poly]
    W_mm = max(xs_m) - min(xs_m)
    H_mm = max(ys_m) - min(ys_m)
    W_px = (max(xs_p) - min(xs_p)) or 1
    H_px = (max(ys_p) - min(ys_p)) or 1
    if W_mm < 50 or H_mm < 50:
        return False
    ar_px = W_px / H_px
    ar_mm = W_mm / H_mm
    rotado = abs(ar_px - ar_mm) > abs(ar_px - 1.0 / ar_mm)

    def _cluster(vals, span):
        """Agrupa coordenadas casi iguales (misma línea del plano dibujada a
        mano — el pulso da desvíos de 5-8px). Devuelve {valor: media}."""
        tol = max(8.0, span * 0.03)
        grupos = []
        for v in sorted(set(vals)):
            if grupos and abs(v - grupos[-1][0]) <= tol:
                grupos[-1].append(v)
            else:
                grupos.append([v])
        mapeo = {}
        for g in grupos:
            avg = sum(g) / len(g)
            for v in g:
                mapeo[v] = avg
        return mapeo

    nuevos_f = None
    if not rotado and not forzar_cierre:
        # Vía A — clustering por eje: contornos limpios con el bbox mm de
        # Claude validado por cotas
        mx = _cluster(xs_p, W_px)
        my = _cluster(ys_p, H_px)
        x0 = min(mx.values())
        y0 = min(my.values())
        sx = W_mm / ((max(mx.values()) - x0) or 1)
        sy = H_mm / ((max(my.values()) - y0) or 1)
        crudos = [[(mx[p[0]] - x0) * sx, (my[p[1]] - y0) * sy] for p in poly]
        nuevos, _ = _snap_vertices_a_cotas(crudos, cotas or set(), cotas_propias)
        # El resultado debe ser (casi) ortogonal — un contorno torcido de
        # forma no rígida produce aquí un polígono deforme (J0025)
        if _escuadrar_poligono(nuevos) is not None:
            area_orig = abs(_signed_area_2d(verts))
            area_new = abs(_signed_area_2d(nuevos)) if len(nuevos) >= 3 else 0.0
            if not (area_orig > 0 and area_new < 0.5 * area_orig):
                nuevos_f = [[float(x), float(y)] for x, y in nuevos]
    via_solver = False
    if nuevos_f is None:
        # Vía B — solver de CIERRE: croquis torcidos o no a escala. La
        # topología viene del contorno del operador; las medidas, de las
        # cotas (con las libres derivadas por cierre del polígono).
        nuevos_f = _resolver_poligono_por_cierre(poly, encimera, cotas,
                                                 cotas_propias)
        if nuevos_f is not None:
            via_solver = True
            trabajo.advertencias.append(
                f"🔧 Postproc [{zona_corta}]: polígono RESUELTO POR COTAS "
                f"con cierre (croquis torcido/no a escala)")
    if nuevos_f is None:
        return False
    verts_orig = [list(v) for v in verts]

    def _aplicar(p):
        p.vertices_mm = [list(v) for v in nuevos_f]
        xs = [v[0] for v in p.vertices_mm]
        ys = [v[1] for v in p.vertices_mm]
        p.largo_mm = float(max(xs) - min(xs))
        p.ancho_mm = float(max(ys) - min(ys))
        p.aristas_contacto = None  # desalineada del nuevo orden de vértices
        p.aristas_cota = None      # ídem (re-runs la malinterpretarían)
        p.notas = ((p.notas or '')
                   + ' [geometría reconstruida desde polilínea del operador]').strip()
        if via_solver:
            # Croquis muy torcido: la proyección bbox de trazos es imprecisa
            # — no crear PIEZAS desde trazos sobre esta geometría (los
            # pulidos/clasificación van por cobertura píxel y sí valen)
            p._sin_piezas_desde_trazos = True  # type: ignore[attr-defined]

    _aplicar(encimera)
    # Propagar a las gemelas de otras opciones de material — comparten el
    # MISMO registro de anotaciones (objeto idéntico asignado en _cargar):
    # si no, su geo_key divergente las dejaría fuera del dedup y sin los
    # copetes/pulidos de exclusión de su opción.
    for otra in trabajo.piezas:
        if otra is encimera or otra.tipo not in ('encimera', 'isla'):
            continue
        if getattr(otra, '_anot_reg', None) is regs:
            _aplicar(otra)
    trabajo.advertencias.append(
        f"🔧 Postproc [{zona_corta}]: polígono RECONSTRUIDO desde la "
        f"polilínea del operador (la emisión de Claude no encajaba): "
        f"{[[round(v[0]), round(v[1])] for v in encimera.vertices_mm]}")
    return True


def _mapa_aristas_por_cercania(regs, verts):
    """Mapa arista_polígono → arista_polilínea derivado de la MISMA
    transformación px→mm que usan trazos y huecos (_trazos_a_mm_local):
    cada vértice de la polilínea convertido a mm-local se empareja con el
    vértice más cercano del polígono. Para rectángulos esto es lo único
    fiable — la alineación por forma normalizada es ambigua (cualquier
    offset de un rectángulo encaja) y podía rotar el mapeo 90° respecto a
    la transformación de los demás trazos. Devuelve None si el emparejado
    no es una biyección consecutiva (polilínea deformada)."""
    import math as _math
    poly = regs.get('polilinea_pix')
    if not poly or not verts or len(poly) != len(verts):
        return None
    conv = _trazos_a_mm_local([poly], poly, verts)
    if not conv or not conv[0] or len(conv[0]) != len(verts):
        return None
    pts = conv[0]
    n = len(verts)
    pareja = []
    for (px, py) in pts:
        i_min = min(range(n), key=lambda i: _math.hypot(verts[i][0] - px,
                                                        verts[i][1] - py))
        pareja.append(i_min)
    if len(set(pareja)) != n:
        return None  # dos puntos cayeron en el mismo vértice
    # ¿Recorrido consecutivo (directo o inverso)?
    directo = all(pareja[(k + 1) % n] == (pareja[k] + 1) % n for k in range(n))
    inverso = all(pareja[(k + 1) % n] == (pareja[k] - 1) % n for k in range(n))
    if not (directo or inverso):
        return None
    mapa = [None] * n
    for k in range(n):
        if directo:
            # poly arista k (puntos k→k+1) ↔ polígono arista pareja[k]
            mapa[pareja[k]] = k
        else:
            # inverso: poly k→k+1 recorre polígono pareja[k]→pareja[k]-1,
            # que es la arista pareja[k]-1 del polígono
            mapa[(pareja[k] - 1) % n] = k
    return mapa


def _clasificar_aristas_pixel(regs, encimera, mapa, tol_mm=250):
    """Clasifica cada arista del polígono (pared/frontal) midiendo EN
    PÍXELES — sobre el canvas real, sin proyectar entre marcos — la
    cobertura de los trazos de no-pulir (pared/MA/copete/frontal/zócalo) a
    lo largo de la arista correspondiente de la polilínea. Inmune a los
    trazos de OTRAS piezas del mismo canvas (que en el marco mm-local de
    esta pieza caen donde no deben) y a errores de geometría de Claude.

    Una arista es pared si los trazos cubren ≥30% de su longitud (o ≥300mm
    equivalentes) a distancia ≤ tol_mm. Devuelve lista de tipos alineada con
    las aristas del polígono, o None si no aplica."""
    import math as _math
    poly = regs.get('polilinea_pix')
    verts = encimera.vertices_mm
    if not poly or mapa is None or len(poly) != len(verts):
        return None
    trazos = []
    for k in ('paredes_pix', 'ma_pix', 'copetes_pix', 'frontales_pix',
              'zocalos_pix'):
        trazos.extend(regs.get(k) or [])
    if not trazos:
        return None
    # Los trazos de PULIDO del operador mandan: una arista cubierta por cian
    # es vista por definición, aunque un rectángulo de muebles altos (que en
    # el plano cuelga SOBRE la encimera) también la roce.
    trazos_pulido = list(regs.get('pulidos_pix') or [])
    xs_m = [v[0] for v in verts]
    ys_m = [v[1] for v in verts]
    xs_p = [p[0] for p in poly]
    ys_p = [p[1] for p in poly]
    escala = (((max(xs_m) - min(xs_m)) / ((max(xs_p) - min(xs_p)) or 1))
              + ((max(ys_m) - min(ys_m)) / ((max(ys_p) - min(ys_p)) or 1))) / 2
    escala = escala or 1.0
    tol_px = tol_mm / escala
    n = len(poly)

    def _cobertura(lista_trazos, x1, y1, dx, dy, L2, L):
        # Longitud CUBIERTA real: unión de intervalos entre puntos
        # CONSECUTIVOS del trazo que están cerca de la arista — el trazo
        # solo cubre el tramo que RECORRE. El span max−min de proyecciones
        # daba cobertura total a un trazo que RODEA la pieza y toca las dos
        # esquinas sin recorrer la arista (J0026: el cian perimetral de la
        # península forzaba 'vista' en la cabeza que va contra pared). Un
        # trazo recto de solo 2 puntos a lo largo de la arista sigue
        # cubriendo [0,1] (J0021/J0022: trazos de pared en línea).
        intervalos = []
        for tr in lista_trazos:
            prev_t = None
            for (px, py) in tr:
                t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy)
                                 / (L2 or 1)))
                cxp, cyp = x1 + t * dx, y1 + t * dy
                cerca = _math.hypot(px - cxp, py - cyp) <= tol_px
                if cerca and prev_t is not None:
                    intervalos.append((min(prev_t, t), max(prev_t, t)))
                prev_t = t if cerca else None
        if not intervalos:
            return 0.0
        intervalos.sort()
        total = 0.0
        cur_a, cur_b = intervalos[0]
        for a, b in intervalos[1:]:
            if a <= cur_b:
                cur_b = max(cur_b, b)
            else:
                total += cur_b - cur_a
                cur_a, cur_b = a, b
        total += cur_b - cur_a
        return total * L

    tipos = []
    cian_cubre = []
    for i in range(n):
        k = mapa[i]
        x1, y1 = poly[k]
        x2, y2 = poly[(k + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        L = _math.sqrt(L2) or 1.0
        objetivo = min(300 / escala, 0.30 * L)
        if _cobertura(trazos_pulido, x1, y1, dx, dy, L2, L) >= objetivo:
            tipos.append('frontal')  # cian del operador = vista, sin discusión
            cian_cubre.append(True)
            continue
        cian_cubre.append(False)
        cobertura = _cobertura(trazos, x1, y1, dx, dy, L2, L)
        tipos.append('pared' if cobertura >= objetivo else 'frontal')

    # Ticks de COPETE: una marca corta no alcanza el 30% de cobertura pero
    # es una declaración deliberada de "esta arista va contra pared"
    # (J0026: la cabeza de la península con tick naranja se pulía). Cada
    # trazo de copete fuerza 'pared' en su arista más cercana, salvo que
    # el cian del operador la reclame como vista.
    for tr in (regs.get('copetes_pix') or []):
        if not tr:
            continue
        cx = sum(p[0] for p in tr) / len(tr)
        cy = sum(p[1] for p in tr) / len(tr)
        best_i = None
        best_d = float('inf')
        for i in range(n):
            k = mapa[i]
            x1, y1 = poly[k]
            x2, y2 = poly[(k + 1) % n]
            d = _dist_punto_a_segmento(cx, cy, x1, y1, x2, y2)
            if d < best_d:
                best_d = d
                best_i = i
        if (best_i is not None and best_d <= tol_px
                and tipos[best_i] == 'frontal' and not cian_cubre[best_i]):
            tipos[best_i] = 'pared'
    return tipos


def _reposicionar_huecos_desde_trazos(trabajo: TrabajoExtraido) -> None:
    """Las marcas rojas del operador (cruces/contornos de hueco) son la
    posición AUTORITATIVA: cada trazo rojo se convierte a mm-local de su
    encimera y reposiciona el hueco correspondiente (Claude tiende a
    recolocarlos o centrarlos pese a la marca)."""
    geometrias = set()
    for enc in trabajo.piezas:
        if enc.tipo not in ('encimera', 'isla') or not enc.vertices_mm \
                or len(enc.vertices_mm) < 3:
            continue
        geo = tuple(tuple(v) for v in enc.vertices_mm)
        if geo in geometrias:
            continue
        geometrias.add(geo)
        regs = getattr(enc, '_anot_reg', None)
        if (not regs or not regs.get('huecos_pix')
                or getattr(enc, '_anot_no_fiable', False)):
            continue
        zona_tokens = ' '.join((enc.zona or '').lower().split()[:2])
        huecos_enc = [h for h in trabajo.huecos
                      if zona_tokens and zona_tokens in (h.pieza_zona or '').lower()]
        if not huecos_enc:
            continue
        poly_pix = regs['polilinea_pix']
        usados = set()
        n_p = len(poly_pix)
        for stroke_pix in regs['huecos_pix']:
            if not stroke_pix:
                continue
            # Pertenencia EN PÍXEL contra la polilínea de ESTA encimera (los
            # trazos se comparten entre encimeras y la proyección bbox a
            # mm-local puede plantar dentro un trazo que es de otra pieza).
            cx_p = sum(p[0] for p in stroke_pix) / len(stroke_pix)
            cy_p = sum(p[1] for p in stroke_pix) / len(stroke_pix)
            if not _punto_en_poligono(cx_p, cy_p, poly_pix):
                d_borde_px = min(
                    _dist_punto_a_segmento(cx_p, cy_p,
                                           poly_pix[i][0], poly_pix[i][1],
                                           poly_pix[(i + 1) % n_p][0],
                                           poly_pix[(i + 1) % n_p][1])
                    for i in range(n_p))
                if d_borde_px > 40:  # ~200mm a escala típica de 5mm/px
                    continue
            mm = _trazos_a_mm_local([stroke_pix], poly_pix, enc.vertices_mm)
            if not mm or not mm[0]:
                continue
            trazo = mm[0]
            cx = sum(p[0] for p in trazo) / len(trazo)
            cy = sum(p[1] for p in trazo) / len(trazo)

            def _dist(h):
                if h.centro_x_mm is None or h.centro_y_mm is None:
                    return 1e9  # sin posición previa: se asigna el último
                import math as _math
                return _math.hypot(h.centro_x_mm - cx, h.centro_y_mm - cy)

            libres = [h for h in huecos_enc if id(h) not in usados]
            if not libres:
                break
            # Matching por TAMAÑO del trazo: un contorno grande es una
            # placa/fregadero; una marca pequeña es grifo/enchufe/dosificador
            # (la cercanía sola confundía el rectángulo del fregadero con el
            # grifo que Claude había colocado al lado).
            xs_t = [p[0] for p in trazo]
            ys_t = [p[1] for p in trazo]
            bw = max(xs_t) - min(xs_t)
            bh = max(ys_t) - min(ys_t)
            es_grande = bw >= 150 and bh >= 150
            candidatos = [h for h in libres
                          if (h.tipo in ('placa', 'fregadero')) == es_grande]
            if es_grande and not candidatos:
                continue  # un contorno grande JAMÁS reposiciona un grifo

            def _puntuacion(h):
                # Preferir el hueco cuyas DIMENSIONES casan con el bbox del
                # trazo (la distancia a la posición previa engaña si Claude
                # la estimó mal o espejada); distancia solo como desempate.
                if h.largo_mm and h.ancho_mm:
                    return (abs(h.largo_mm - bw) + abs(h.ancho_mm - bh),
                            _dist(h))
                return (1e9, _dist(h))

            h = min(candidatos or libres, key=_puntuacion)
            usados.add(id(h))
            old = (h.centro_x_mm, h.centro_y_mm)
            h.centro_x_mm = round(cx, 0)
            h.centro_y_mm = round(cy, 0)
            xs = [p[0] for p in trazo]
            ys = [p[1] for p in trazo]
            if not h.largo_mm and (max(xs) - min(xs)) >= 50:
                h.largo_mm = round(max(xs) - min(xs), 0)
            if not h.ancho_mm and (max(ys) - min(ys)) >= 50:
                h.ancho_mm = round(max(ys) - min(ys), 0)
            h.notas = ((h.notas or '')
                       + ' [posición desde trazo rojo del operador]').strip()
            if old[0] is not None and abs(old[0] - cx) > 100:
                trabajo.advertencias.append(
                    f"Postproc: hueco {h.tipo} reposicionado por trazo del "
                    f"operador ({old[0]:.0f},{old[1]:.0f})→({cx:.0f},{cy:.0f}) "
                    f"en {enc.zona or '?'}")
            # Orientación: si la marca del operador es claramente apaisada o
            # vertical y contradice el largo×ancho del hueco, se gira 90°
            # (J0024: fregadero en el brazo vertical dibujado girado)
            if (h.largo_mm and h.ancho_mm and bw >= 50 and bh >= 50
                    and abs(bw - bh) > 0.15 * max(bw, bh)
                    and abs(h.largo_mm - h.ancho_mm) > 10
                    and (bw > bh) != (h.largo_mm > h.ancho_mm)):
                h.largo_mm, h.ancho_mm = h.ancho_mm, h.largo_mm
                h.notas = ((h.notas or '')
                           + ' [girado 90° según marca del operador]').strip()
                trabajo.advertencias.append(
                    f"Postproc: hueco {h.tipo} girado 90° según la marca del "
                    f"operador ({h.largo_mm:.0f}×{h.ancho_mm:.0f})")
            # El grifo va DETRÁS del fregadero, hacia la pared REAL (la
            # arista clasificada pared por los trazos del operador) — la
            # convención Y de Claude puede estar invertida respecto al canvas
            if h.tipo == 'fregadero':
                grifo = next((g for g in huecos_enc
                              if g.tipo == 'grifo' and id(g) not in usados),
                             None)
                tipos_ar = getattr(enc, '_aristas_tipos', None)
                if grifo is not None and tipos_ar:
                    paredes_largas = [
                        t for t in tipos_ar
                        if t['tipo'] == 'pared'
                        and t['len'] >= 0.5 * max(x['len'] for x in tipos_ar)]
                    if paredes_largas:
                        import math as _math
                        def _d_arista(t):
                            p1 = enc.vertices_mm[t['v1']]
                            p2 = enc.vertices_mm[t['v2']]
                            return _dist_punto_a_segmento(
                                cx, cy, p1[0], p1[1], p2[0], p2[1])
                        cercana = min(paredes_largas, key=_d_arista)
                        p1 = enc.vertices_mm[cercana['v1']]
                        p2 = enc.vertices_mm[cercana['v2']]
                        # Proyección del centro del fregadero sobre la pared
                        dx, dy = p2[0] - p1[0], p2[1] - p1[1]
                        L2 = dx * dx + dy * dy or 1
                        tt = max(0.0, min(1.0, ((cx - p1[0]) * dx
                                                + (cy - p1[1]) * dy) / L2))
                        px_, py_ = p1[0] + tt * dx, p1[1] + tt * dy
                        # Grifo a ~60mm de la pared, alineado con el fregadero
                        nx, ny = cx - px_, cy - py_
                        nn = _math.hypot(nx, ny) or 1
                        usados.add(id(grifo))
                        grifo.centro_x_mm = round(px_ + nx / nn * 60, 0)
                        grifo.centro_y_mm = round(py_ + ny / nn * 60, 0)
                        grifo.notas = ((grifo.notas or '')
                                       + ' [recolocado detrás del fregadero '
                                         'hacia la pared clasificada]').strip()


def _ajustar_profundidad_huecos(trabajo: TrabajoExtraido) -> None:
    """Regla de taller (operador, 2026-06-11): si no hay posición EXACTA
    (marca roja del operador o cota), el borde delantero de placa/fregadero
    queda a 80mm del FRENTE de la encimera (la arista vista). Solo se ajusta
    la coordenada perpendicular al frente; la lateral se conserva."""
    import math as _math
    MARGEN = 80.0
    for enc in trabajo.piezas:
        if enc.tipo not in ('encimera', 'isla') or not enc.vertices_mm:
            continue
        tipos_ar = getattr(enc, '_aristas_tipos', None)
        if not tipos_ar:
            continue
        max_len = max(a['len'] for a in tipos_ar)
        frentes = [a for a in tipos_ar
                   if a['tipo'] != 'pared' and a['len'] >= 0.5 * max_len]
        if not frentes:
            continue
        zona_tokens = ' '.join((enc.zona or '').lower().split()[:2])
        for h in trabajo.huecos:
            if h.tipo not in ('placa', 'fregadero'):
                continue
            if 'trazo rojo' in (h.notas or ''):
                continue  # la marca del operador es exacta — no tocar
            if not (zona_tokens and zona_tokens in (h.pieza_zona or '').lower()):
                continue
            if h.centro_x_mm is None or h.centro_y_mm is None:
                continue
            cx, cy = h.centro_x_mm, h.centro_y_mm
            mejor = None
            for a in frentes:
                p1 = enc.vertices_mm[a['v1']]
                p2 = enc.vertices_mm[a['v2']]
                d = _dist_punto_a_segmento(cx, cy, p1[0], p1[1], p2[0], p2[1])
                if mejor is None or d < mejor[0]:
                    mejor = (d, p1, p2)
            if mejor is None:
                continue
            _, p1, p2 = mejor
            dx, dy = p2[0] - p1[0], p2[1] - p1[1]
            L2 = dx * dx + dy * dy or 1
            t = max(0.0, min(1.0, ((cx - p1[0]) * dx + (cy - p1[1]) * dy) / L2))
            px_, py_ = p1[0] + t * dx, p1[1] + t * dy
            nx, ny = cx - px_, cy - py_
            nn = _math.hypot(nx, ny) or 1.0
            # Dimensión del hueco a lo largo de la normal al frente
            profundidad = (h.ancho_mm if abs(ny) >= abs(nx) else h.largo_mm) or 400.0
            objetivo = MARGEN + profundidad / 2.0
            nuevo_x = round(px_ + nx / nn * objetivo, 0)
            nuevo_y = round(py_ + ny / nn * objetivo, 0)
            if abs(nuevo_x - cx) < 5 and abs(nuevo_y - cy) < 5:
                continue
            h.centro_x_mm, h.centro_y_mm = nuevo_x, nuevo_y
            h.notas = ((h.notas or '')
                       + ' [profundidad default: 80mm del frente]').strip()
            trabajo.advertencias.append(
                f"Postproc: hueco {h.tipo} llevado a 80mm del frente "
                f"({cx:.0f},{cy:.0f})→({nuevo_x:.0f},{nuevo_y:.0f})")


def _validar_huecos_dentro(trabajo: TrabajoExtraido) -> None:
    """Invariante (J0026): un hueco placa/fregadero debe caer ÍNTEGRO dentro
    del polígono de su pieza. Si no cabe: 1) se prueba girado 90° (el
    fregadero va A LO LARGO del brazo), 2) se desliza a lo largo del frente
    conservando la distancia perpendicular (regla 80mm); si nada lo mete →
    advertencia. Los huecos con marca roja del operador no se mueven (la
    marca es autoritativa): solo se avisa de la inconsistencia."""
    import math as _math
    MARGEN_BORDE = 2.0
    procesados = set()
    for enc in trabajo.piezas:
        if enc.tipo not in ('encimera', 'isla') or not enc.vertices_mm \
                or len(enc.vertices_mm) < 3:
            continue
        verts = enc.vertices_mm
        zona_tokens = ' '.join((enc.zona or '').lower().split()[:2])

        def _dentro(cx, cy, l, a):
            for sx in (-1.0, 0.0, 1.0):
                for sy in (-1.0, 0.0, 1.0):
                    px = cx + sx * (l / 2.0 - MARGEN_BORDE)
                    py = cy + sy * (a / 2.0 - MARGEN_BORDE)
                    if not _punto_en_poligono(px, py, verts):
                        return False
            return True

        for h in trabajo.huecos:
            if h.tipo not in ('placa', 'fregadero') or id(h) in procesados:
                continue
            if not (zona_tokens and zona_tokens in (h.pieza_zona or '').lower()):
                continue
            if h.centro_x_mm is None or h.centro_y_mm is None \
                    or not h.largo_mm or not h.ancho_mm:
                continue
            procesados.add(id(h))
            cx, cy = h.centro_x_mm, h.centro_y_mm
            l, a = h.largo_mm, h.ancho_mm
            if _dentro(cx, cy, l, a):
                continue
            if 'trazo rojo' in (h.notas or ''):
                trabajo.advertencias.append(
                    f"⚠ Hueco {h.tipo} con marca del operador NO cabe en "
                    f"{(enc.zona or '?')[:35]} — revisar geometría o marca")
                continue
            arreglado = None
            if _dentro(cx, cy, a, l):
                arreglado = (cx, cy, a, l, 'girado 90°')
            if arreglado is None:
                # Deslizar por la arista vista (frente) más cercana,
                # conservando la componente perpendicular (regla 80mm)
                tipos_ar = getattr(enc, '_aristas_tipos', None) or []
                mejor = None
                for ar in tipos_ar:
                    if ar['tipo'] == 'pared':
                        continue
                    p1, p2 = verts[ar['v1']], verts[ar['v2']]
                    d = _dist_punto_a_segmento(cx, cy, p1[0], p1[1],
                                               p2[0], p2[1])
                    if mejor is None or d < mejor[0]:
                        mejor = (d, p1, p2)
                if mejor:
                    _, p1, p2 = mejor
                    ux, uy = p2[0] - p1[0], p2[1] - p1[1]
                    L_ar = _math.hypot(ux, uy) or 1.0
                    ux, uy = ux / L_ar, uy / L_ar
                    for paso in range(1, int(L_ar / 25.0) + 1):
                        for signo in (1, -1):
                            s = signo * paso * 25.0
                            for (ll, aa, et) in ((l, a, ''),
                                                 (a, l, 'girado 90° y ')):
                                if _dentro(cx + s * ux, cy + s * uy, ll, aa):
                                    arreglado = (cx + s * ux, cy + s * uy,
                                                 ll, aa,
                                                 f'{et}deslizado '
                                                 f'{abs(s):.0f}mm')
                                    break
                            if arreglado:
                                break
                        if arreglado:
                            break
            if arreglado is None:
                trabajo.advertencias.append(
                    f"⚠ Hueco {h.tipo} ({l:.0f}×{a:.0f}) NO cabe en "
                    f"{(enc.zona or '?')[:35]} en ninguna posición — VERIFICAR")
                continue
            ncx, ncy, nl, na, como = arreglado
            dx_, dy_ = ncx - cx, ncy - cy
            h.centro_x_mm, h.centro_y_mm = round(ncx, 0), round(ncy, 0)
            h.largo_mm, h.ancho_mm = nl, na
            h.notas = ((h.notas or '')
                       + f' [{como} para caber en la pieza]').strip()
            trabajo.advertencias.append(
                f"Postproc: hueco {h.tipo} {como} para caber en "
                f"{(enc.zona or '?')[:35]}"
                + (" — VERIFICAR posición lateral"
                   if _math.hypot(dx_, dy_) > 300 else ""))
            # El grifo acompaña a su fregadero (mantiene la alineación)
            if h.tipo == 'fregadero' and (abs(dx_) > 1 or abs(dy_) > 1):
                for g in trabajo.huecos:
                    if (g.tipo == 'grifo' and g.centro_x_mm is not None
                            and g.centro_y_mm is not None
                            and _math.hypot(g.centro_x_mm - cx,
                                            g.centro_y_mm - cy) <= 700):
                        g.centro_x_mm = round(g.centro_x_mm + dx_, 0)
                        g.centro_y_mm = round(g.centro_y_mm + dy_, 0)


def _espejar_opciones(trabajo: TrabajoExtraido) -> None:
    """Cuando hay varias opciones de material (_opcion1/_opcion2…), las
    piezas físicas son LAS MISMAS en todas (misma cocina) — solo cambia el
    material. Si Claude emitió una pieza en una opción y la olvidó en otra
    (p.ej. frontal solo en opcion1), se clona en las que falte. Sin esto los
    presupuestos por opción salen descompensados (J0020: opcion2 sin
    frontal → 1 tabla menos)."""
    TIPOS_FISICOS = ('encimera', 'isla', 'frontal', 'copete', 'zocalo',
                     'costado')
    SUFIJOS = ('_opcion1', '_opcion2', '_opcion3', '_opcion_a', '_opcion_b')

    def _sufijo(p):
        rol = p.material_rol or ''
        return next((s for s in SUFIJOS if s in rol), None)

    sufijos_presentes = sorted({_sufijo(p) for p in trabajo.piezas
                                if _sufijo(p) and p.tipo in TIPOS_FISICOS})
    if len(sufijos_presentes) < 2:
        return

    def _misma_pieza(a, b):
        if a.tipo != b.tipo:
            return False
        if a.vertices_mm and b.vertices_mm:
            return a.vertices_mm == b.vertices_mm
        for va, vb in ((a.largo_mm, b.largo_mm), (a.ancho_mm, b.ancho_mm),
                       (a.altura_mm, b.altura_mm)):
            if va and vb and abs(va - vb) > 5:
                return False
        la = a.longitud_ml or 0
        lb = b.longitud_ml or 0
        if la and lb and abs(la - lb) > 0.01:
            return False
        return bool((a.largo_mm and b.largo_mm) or (la and lb))

    import copy as _copy
    nuevas = []
    for suf_origen in sufijos_presentes:
        piezas_origen = [p for p in trabajo.piezas
                         if p.tipo in TIPOS_FISICOS and _sufijo(p) == suf_origen]
        for suf_destino in sufijos_presentes:
            if suf_destino == suf_origen:
                continue
            piezas_destino = [p for p in trabajo.piezas
                              if p.tipo in TIPOS_FISICOS
                              and _sufijo(p) == suf_destino]
            for p in piezas_origen:
                ya_clonadas = [q for q in nuevas if _sufijo(q) == suf_destino]
                if any(_misma_pieza(p, q) for q in piezas_destino + ya_clonadas):
                    continue
                clon = _copy.copy(p)
                clon.material_rol = (p.material_rol or '').replace(
                    suf_origen, suf_destino)
                clon.notas = ((p.notas or '')
                              + f' [espejado de {suf_origen.lstrip("_")}]').strip()
                nuevas.append(clon)
                trabajo.advertencias.append(
                    f"Postproc: pieza {p.tipo} ({p.largo_mm or p.longitud_ml}"
                    f") espejada de {suf_origen} a {suf_destino} — Claude la "
                    f"emitió solo en una opción")
    if nuevas:
        trabajo.piezas.extend(nuevas)


def _completar_piezas_desde_trazos(trabajo: TrabajoExtraido) -> None:
    """Emite piezas copete/zócalo/frontal a partir de los trazos del operador
    cuando Claude no las emitió («si el operador lo marcó, existe»).

    Cada trazo se asigna a la arista más cercana de su encimera (mismo
    criterio de pertenencia que los pulidos cian: centroide → arista,
    tolerancia 400mm — los trazos de otra encimera caen lejos al convertir
    a mm-local y no matchean). La pieza se crea con longitud = longitud de
    la arista, salvo que ya exista una del mismo tipo con longitud ±20%.
    """
    import math as _math
    TOL_PERTENENCIA_MM = 400
    TIPOS = (('copetes_pix', 'copete'), ('zocalos_pix', 'zocalo'),
             ('frontales_pix', 'frontal'))

    encimeras = [p for p in trabajo.piezas
                 if p.tipo in ('encimera', 'isla') and p.vertices_mm
                 and len(p.vertices_mm) >= 3]
    if not encimeras:
        return

    def _altura_default(tipo, enc):
        if tipo == 'frontal':
            # La altura del material puede ser la de una zona especial
            # (campana a 1500 — J0026); los chapeados de pared van a ~600.
            # Prioridad: frontal de Claude en la MISMA encimera > 600.
            zona_tokens_enc = ' '.join((enc.zona or '').lower().split()[:2])
            for p in trabajo.piezas:
                if (p.tipo == 'frontal' and p.altura_mm and zona_tokens_enc
                        and zona_tokens_enc in (p.zona or '').lower()):
                    return p.altura_mm
            return 600.0
        for m in trabajo.materiales:
            if m.rol and tipo in m.rol.lower() and m.altura_cm:
                return float(m.altura_cm) * 10.0
        return {'copete': 50.0, 'zocalo': 60.0}[tipo]

    def _material_rol(tipo, enc):
        for m in trabajo.materiales:
            if m.rol and tipo in m.rol.lower():
                return m.rol
        return enc.material_rol

    # Pool de longitudes de piezas YA emitidas por Claude, por tipo. Cada
    # trazo del operador consume como mucho una pieza del pool (±20%); si el
    # pool se agota, la pieza falta → se crea. Así dos copetes de cabeza
    # IGUALES (p.ej. 0.6m + 0.6m) generan dos piezas, no una («si van
    # separadas se dibujan separadas»).
    GENERICOS = {'encimera', 'tramo', 'superior', 'inferior', 'principal',
                 'central', 'zona', 'con', 'de', 'la', 'el', 'en'}

    def _tokens_distintivos(zona):
        return {w for w in (zona or '').lower().replace('(', ' ').split()
                if len(w) >= 3 and w not in GENERICOS}

    pool = {}
    for _, tipo in TIPOS:
        pool[tipo] = [{'L': p.longitud_ml or ((p.largo_mm or 0) / 1000.0),
                       'zona': p.zona or ''}
                      for p in trabajo.piezas if p.tipo == tipo]

    usados = set()       # id() de trazos píxel ya consumidos (listas compartidas)
    aristas_creadas = set()  # (geo, tipo, idx_arista, L) con pieza ya creada
    # Tramos de arista ya cubiertos por trazos anteriores: (geo, tipo, idx)
    # → [(t0, t1)]. Un garabato redundante ENCIMA de la misma marca (J0024:
    # frontal repasado 3 veces) no debe crear piezas nuevas.
    intervalos_cubiertos = {}
    nuevas: list = []
    frontal_trazos_totales = 0
    frontales_consumidos_L: list = []
    geometrias = set()
    for enc in encimeras:
        geo = tuple(tuple(v) for v in enc.vertices_mm)
        if geo in geometrias:
            continue
        geometrias.add(geo)
        regs = getattr(enc, '_anot_reg', None)
        if not regs or getattr(enc, '_anot_no_fiable', False):
            continue
        if getattr(enc, '_sin_piezas_desde_trazos', False):
            trabajo.advertencias.append(
                f"Postproc [{(enc.zona or '?')[:35]}]: piezas desde trazos NO "
                f"aplicadas (croquis torcido, geometría del solver) — "
                f"verificar chapeados/copetes manualmente")
            continue
        zona_corta = (enc.zona or '?').split('(')[0].strip()[:35]
        verts = enc.vertices_mm
        n = len(verts)
        for key_pix, tipo in TIPOS:
            # Trazos GRANDES primero: la marca principal registra su tramo y
            # los repasos pequeños encima quedan como redundantes
            trazos_orden = sorted(regs.get(key_pix) or [],
                                  key=_path_length_mm, reverse=True)
            for stroke_pix in trazos_orden:
                if id(stroke_pix) in usados:
                    continue
                mm = _trazos_a_mm_local([stroke_pix], regs['polilinea_pix'], verts)
                if not mm or not mm[0]:
                    continue
                stroke = mm[0]
                L_trazo = _path_length_mm(stroke)
                # Aristas CUBIERTAS por el trazo — no solo la más cercana al
                # centroide: un trazo en L cubre DOS aristas, y el centroide
                # de un trazo largo puede caer sobre una arista interior
                # ajena (J0024: copete fantasma sobre una arista pulida).
                # Además varios tramos cortos pueden caer en la misma arista
                # larga (frontal por tramos alrededor de un pilar).
                objetivos = []  # (idx, len_arista, cobertura_mm)
                for i in range(n):
                    p1, p2 = verts[i], verts[(i + 1) % n]
                    dxa = p2[0] - p1[0]
                    dya = p2[1] - p1[1]
                    L2a = dxa * dxa + dya * dya
                    L_ar = _math.sqrt(L2a) or 1.0
                    if L_ar < 100:
                        continue
                    ts = []
                    for (px, py) in stroke:
                        tt = max(0.0, min(1.0, ((px - p1[0]) * dxa
                                                + (py - p1[1]) * dya)
                                          / (L2a or 1)))
                        cxp, cyp = p1[0] + tt * dxa, p1[1] + tt * dya
                        # 450mm: los trazos de frontal/zócalo se dibujan
                        # sobre la PARED del plano, no sobre la encimera
                        if _math.hypot(px - cxp, py - cyp) <= 450:
                            ts.append(tt)
                    if len(ts) < 2:
                        continue
                    cov = (max(ts) - min(ts)) * L_ar
                    frac_pts = len(ts) / len(stroke)
                    if ((cov >= 0.2 * L_ar)
                            or (frac_pts >= 0.5 and cov >= 100)
                            or (frac_pts >= 0.25 and cov >= 300)):
                        objetivos.append((i, L_ar, cov, min(ts), max(ts)))
                if not objetivos and L_trazo <= 400:
                    # Marca corta (tick): arista más cercana al centroide
                    cx = sum(p[0] for p in stroke) / len(stroke)
                    cy = sum(p[1] for p in stroke) / len(stroke)
                    best = None
                    for i in range(n):
                        p1, p2 = verts[i], verts[(i + 1) % n]
                        d = _dist_punto_a_segmento(cx, cy, p1[0], p1[1],
                                                   p2[0], p2[1])
                        L_ar = _math.hypot(p2[0] - p1[0], p2[1] - p1[1])
                        if L_ar >= 100 and (best is None or d < best[0]):
                            best = (d, i, L_ar)
                    if best and best[0] <= TOL_PERTENENCIA_MM:
                        objetivos = [(best[1], best[2],
                                      min(L_trazo, best[2]), 0.0, 1.0)]
                if not objetivos:
                    continue
                usados.add(id(stroke_pix))
                if tipo == 'frontal':
                    frontal_trazos_totales += 1
                objetivos_pendientes = list(objetivos)
                # 1) ¿Queda una pieza de Claude en el pool que cubra este
                # trazo? (match exacto ±20%, prefiriendo afinidad de zona)
                toks_enc = _tokens_distintivos(enc.zona)

                def _afin(entrada):
                    return bool(toks_enc
                                & _tokens_distintivos(entrada['zona']))

                def _compatible(e):
                    # SOLO para zócalos: zonas con tokens distintivos
                    # DISJUNTOS = piezas de encimeras distintas (el residuo
                    # del zócalo de la pared no puede cubrir el trazo de la
                    # isla). En copetes/frontales la zona describe la cabeza
                    # y no suele nombrar a la encimera — no filtrar.
                    if tipo != 'zocalo':
                        return True
                    toks_e = _tokens_distintivos(e['zona'])
                    if toks_enc and toks_e and not (toks_enc & toks_e):
                        return False
                    return True

                for best_i, best_len, cov, t0, t1 in objetivos_pendientes:
                    # ¿Garabato redundante? Si el tramo ya está cubierto en
                    # ≥70% por trazos anteriores de este tipo en esta arista
                    # (la marca se repasa varias veces), no genera nada.
                    clave_int = (geo, tipo, best_i)
                    previos = intervalos_cubiertos.get(clave_int, [])
                    solape = 0.0
                    for (a0, a1) in previos:
                        solape += max(0.0, min(t1, a1) - max(t0, a0))
                    if (t1 - t0) > 0 and solape / (t1 - t0) >= 0.7:
                        continue
                    intervalos_cubiertos.setdefault(clave_int, []).append((t0, t1))
                    # Longitud objetivo: la arista si el trazo la recorre en
                    # su mayor parte (≥60% — el pulso a mano se queda corto);
                    # si no, lo realmente cubierto (un zócalo o un tramo de
                    # chapeado puede cubrir solo parte de la arista)
                    if cov >= 0.6 * best_len:
                        L_ml = round(best_len / 1000.0, 3)
                    else:
                        L_ml = round(cov / 1000.0, 3)
                        # Cobertura corta y fraccional = cola del trazo
                        # doblando la esquina hacia la arista vecina, no
                        # una pieza (J0028: copete de 0.107 fantasma desde
                        # el trazo del copete de la columna)
                        if L_ml < 0.2:
                            continue
                    if L_ml < 0.1:
                        continue
                    candidatas = sorted(
                        (e for e in pool[tipo]
                         if e['L'] and abs(e['L'] - L_ml) <= max(0.2 * L_ml, 0.1)
                         and _compatible(e)),
                        key=lambda e: not _afin(e))
                    if candidatas:
                        pool[tipo].remove(candidatas[0])
                        if tipo == 'frontal':
                            frontales_consumidos_L.append(candidatas[0]['L'])
                        continue
                    # Zócalos: una pieza larga de Claude (cota del plano,
                    # p.ej. 4830 para toda la pared) cubre VARIOS tramos
                    # marcados — consumo residual SOLO de piezas afines
                    if tipo == 'zocalo':
                        afines = [e for e in pool[tipo]
                                  if _afin(e) and e['L'] >= L_ml * 0.8]
                        if afines:
                            mayor = max(afines, key=lambda e: e['L'])
                            pool[tipo].remove(mayor)
                            resto = mayor['L'] - L_ml
                            if resto > 0.1:
                                pool[tipo].append({'L': round(resto, 3),
                                                   'zona': mayor['zona']})
                            continue
                    # ¿Ya creamos pieza equivalente en esta arista? (tramos
                    # DISTINTOS en la misma arista sí se permiten)
                    clave_arista = (geo, tipo, best_i, round(L_ml, 1))
                    if clave_arista in aristas_creadas:
                        continue
                    aristas_creadas.add(clave_arista)
                    nuevas.append(Pieza(
                        tipo=tipo,
                        material_rol=_material_rol(tipo, enc),
                        largo_mm=round(L_ml * 1000.0, 0),
                        altura_mm=_altura_default(tipo, enc),
                        longitud_ml=L_ml,
                        zona=f'{tipo} en {zona_corta} (arista idx={best_i})',
                        notas='Auto desde trazo operador (postproc)',
                    ))
                    trabajo.advertencias.append(
                        f"Postproc: añadido {tipo} {L_ml}ml desde trazo del "
                        f"operador en {zona_corta} (arista idx={best_i})")
                    if tipo == 'frontal':
                        # Un chapeado en esta arista anula el copete que el
                        # copete-por-exclusión emitió ANTES de conocer este
                        # trazo (copete y frontal son excluyentes — J0026)
                        marca_idx = f'(arista idx={best_i})'
                        for p in list(trabajo.piezas):
                            if (p.tipo == 'copete'
                                    and marca_idx in (p.zona or '')
                                    and zona_corta in (p.zona or '')
                                    and 'Auto exclusión' in (p.notas or '')):
                                trabajo.piezas.remove(p)
                                trabajo.advertencias.append(
                                    f"Postproc [{zona_corta}]: copete auto de "
                                    f"la arista idx={best_i} retirado — el "
                                    f"operador marcó chapeado ahí")
    if nuevas:
        trabajo.piezas.extend(nuevas)

    # Red de seguridad anti-invención (J0026: chapeado 1000 de la península
    # que Claude se inventó): si el operador SÍ marcó chapeados, un frontal
    # de Claude cuya longitud no casa con NINGÚN trazo es sospechoso.
    if frontal_trazos_totales:
        avisadas = set()
        for p in trabajo.piezas:
            if p.tipo != 'frontal':
                continue
            notas_p = (p.notas or '').lower()
            if 'postproc' in notas_p or 'espejad' in notas_p:
                continue
            L = p.longitud_ml or ((p.largo_mm or 0) / 1000.0)
            if not L:
                continue
            clave = round(L, 1)
            if clave in avisadas:
                continue
            if any(abs(L - Lc) <= max(0.2 * L, 0.1)
                   for Lc in frontales_consumidos_L):
                continue
            avisadas.add(clave)
            trabajo.advertencias.append(
                f"⚠ Frontal {L:.2f}ml (alto {p.altura_mm or '?'}mm, "
                f"{(p.zona or '?')[:35]}) SIN trazo del operador que lo "
                f"respalde — verificar que existe en el plano")


def _verificar_muesca_pilar(trabajo: TrabajoExtraido) -> None:
    """Red de seguridad de pilares: si el operador marcó un pilar que cae
    dentro (o pegado al borde) del polígono de una encimera y el polígono NO
    tiene vértices cóncavos cerca del pilar, falta la muesca → advertencia.
    """
    import math as _math

    def _concavos(verts):
        n = len(verts)
        area = _signed_area_2d(verts)
        sentido = 1 if area > 0 else -1
        out = []
        for i in range(n):
            xp, yp = verts[(i - 1) % n]
            xc, yc = verts[i]
            xn, yn = verts[(i + 1) % n]
            cross = (xc - xp) * (yn - yc) - (yc - yp) * (xn - xc)
            if cross * sentido < 0:
                out.append((xc, yc))
        return out

    geometrias = set()
    for enc in trabajo.piezas:
        if enc.tipo not in ('encimera', 'isla') or not enc.vertices_mm \
                or len(enc.vertices_mm) < 3:
            continue
        geo = tuple(tuple(v) for v in enc.vertices_mm)
        if geo in geometrias:
            continue
        geometrias.add(geo)
        regs = getattr(enc, '_anot_reg', None)
        if (not regs or not regs.get('pilares_pix')
                or getattr(enc, '_anot_no_fiable', False)):
            continue
        pilares_mm = _trazos_a_mm_local(
            regs['pilares_pix'], regs['polilinea_pix'], enc.vertices_mm)
        concavos = _concavos(enc.vertices_mm)
        for trazo in pilares_mm:
            if not trazo:
                continue
            cx = sum(p[0] for p in trazo) / len(trazo)
            cy = sum(p[1] for p in trazo) / len(trazo)
            n_v = len(enc.vertices_mm)
            d_borde = min(
                _dist_punto_a_segmento(cx, cy,
                                       enc.vertices_mm[i][0], enc.vertices_mm[i][1],
                                       enc.vertices_mm[(i + 1) % n_v][0],
                                       enc.vertices_mm[(i + 1) % n_v][1])
                for i in range(n_v))
            if not (_punto_en_poligono(cx, cy, enc.vertices_mm) or d_borde <= 200):
                continue  # el pilar no afecta a esta encimera
            tiene_muesca = any(
                _math.hypot(vx - px, vy - py) <= 300
                for (vx, vy) in concavos for (px, py) in trazo)
            if not tiene_muesca:
                trabajo.advertencias.append(
                    f"⚠ PILAR sin muesca: el operador marcó un pilar sobre "
                    f"'{enc.zona or '?'}' pero el polígono no tiene entrante "
                    f"(vértices cóncavos) cerca — revisar geometría")


def _volcar_divergencia(trabajo: TrabajoExtraido, folder,
                        snapshot_claude: dict) -> None:
    """Escribe <carpeta>/divergencia.json comparando lo que emitió Claude con
    el resultado tras el postproc (anotaciones del operador como verdad).
    Corpus para el plan de aprendizaje: métrica de % acierto por proyecto y,
    a futuro, selección de ejemplos few-shot."""
    from datetime import datetime

    def _key_canto(t):
        tipo, lml, _ = t
        return (tipo, round(lml or 0, 2))

    cantos_claude = snapshot_claude.get('cantos', [])
    cantos_final = [(c.tipo, c.longitud_ml, c.notas) for c in trabajo.cantos]
    claves_claude = [_key_canto(t) for t in cantos_claude]
    claves_final = [_key_canto(t) for t in cantos_final]

    conservados = []
    restantes = list(claves_final)
    for k in claves_claude:
        if k in restantes:
            restantes.remove(k)
            conservados.append(k)
    eliminados = [t for t in cantos_claude
                  if _key_canto(t) not in conservados
                  or claves_claude.count(_key_canto(t)) > conservados.count(_key_canto(t))]

    piezas_claude = snapshot_claude.get('piezas', [])
    piezas_final = [(p.tipo, p.largo_mm, p.ancho_mm, p.zona) for p in trabajo.piezas]
    añadidas = [p for p in piezas_final if p not in piezas_claude]
    quitadas = [p for p in piezas_claude if p not in piezas_final]

    n_claude = len(cantos_claude)
    div = {
        'job_id': trabajo.job_id,
        'fecha': datetime.now().isoformat(timespec='seconds'),
        'metricas': {
            'cantos_claude': n_claude,
            'cantos_final': len(cantos_final),
            'cantos_conservados': len(conservados),
            'pct_cantos_acertados': round(100.0 * len(conservados) / n_claude, 1)
                                    if n_claude else None,
            'piezas_añadidas_postproc': len(añadidas),
            'piezas_eliminadas_postproc': len(quitadas),
        },
        'cantos_eliminados': [
            {'tipo': t[0], 'longitud_ml': t[1], 'notas': t[2]} for t in eliminados],
        'cantos_añadidos': [
            {'tipo': c.tipo, 'longitud_ml': c.longitud_ml, 'notas': c.notas}
            for c in trabajo.cantos
            if _key_canto((c.tipo, c.longitud_ml, None)) not in claves_claude],
        'piezas_añadidas': [
            {'tipo': p[0], 'largo_mm': p[1], 'ancho_mm': p[2], 'zona': p[3]}
            for p in añadidas],
        'piezas_eliminadas': [
            {'tipo': p[0], 'largo_mm': p[1], 'ancho_mm': p[2], 'zona': p[3]}
            for p in quitadas],
    }
    out = Path(folder) / 'divergencia.json'
    out.write_text(json.dumps(div, ensure_ascii=False, indent=2),
                   encoding='utf-8')


def extract_trabajo(
    folder: Path,
    api_key: Optional[str] = None,
    model: str = "claude-sonnet-4-6",
    verbose: bool = True,
    auto_clasificar: bool = True,
) -> TrabajoExtraido:
    """
    Función principal: dado un Path de carpeta, extrae todos los datos.

    Si `auto_clasificar=True` y no existe `clasificacion.json`, lanza el
    clasificador previamente para que el extractor reciba etiquetas
    de tipo de contenido por archivo/página (mejora extracción de forma).
    """
    if verbose:
        print(f"\n[Procesando] {folder.name}")

    # Info básica del nombre de la carpeta
    folder_info = parse_folder_name(folder.name)

    # Cargar / generar clasificación de fuentes
    clasif_path = folder / "clasificacion.json"
    clasificacion = None
    if auto_clasificar and not clasif_path.exists():
        if verbose:
            print(f"  [pre-step] Clasificando fuentes (no existe clasificacion.json)...")
        try:
            from clasificador import clasificar_proyecto
            clave = api_key or os.environ.get("ANTHROPIC_API_KEY")
            if clave:
                clasificacion = clasificar_proyecto(folder, clave, verbose=verbose)
                clasif_path.write_text(
                    json.dumps(clasificacion, ensure_ascii=False, indent=2),
                    encoding="utf-8")
        except Exception as e:
            if verbose:
                print(f"  [!] Clasificación falló (continuando sin ella): {e}")
            clasificacion = None
    elif clasif_path.exists():
        try:
            clasificacion = json.loads(clasif_path.read_text(encoding="utf-8"))
            if verbose:
                print(f"  [pre-step] clasificacion.json cargado")
        except Exception:
            clasificacion = None

    # Construir contenido para Claude
    content, archivos = build_claude_content(folder, verbose=verbose,
                                              clasificacion=clasificacion)

    if len(content) <= 1:
        # Solo hay el texto de contexto, no hay archivos útiles
        trabajo = TrabajoExtraido(
            job_id=folder_info.get('job_id', ''),
            cliente=folder_info.get('cliente', ''),
            tienda=folder_info.get('tienda'),
            vendedor=folder_info.get('vendedor'),
            ciudad=folder_info.get('ciudad'),
            confianza='baja',
            advertencias=['No se encontraron archivos procesables en la carpeta'],
        )
        trabajo.archivos_fuente = archivos
        return trabajo

    # Llamar a Claude
    key = api_key or os.environ.get('ANTHROPIC_API_KEY')
    if not key:
        raise ValueError("Se necesita ANTHROPIC_API_KEY. Pásala como argumento o variable de entorno.")

    client = anthropic.Anthropic(api_key=key)

    if verbose:
        print(f"  Enviando {len(content)} bloques a Claude ({model})...")

    import time
    max_retries = 3
    for attempt in range(max_retries):
        try:
            message = _mensaje_streaming(
                client,
                model=model,
                max_tokens=32000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}]
            )
            response_text = _texto_respuesta(message)
            break
        except anthropic.RateLimitError as e:
            wait = 60 * (attempt + 1)
            if attempt < max_retries - 1:
                if verbose:
                    print(f"  Rate limit, esperando {wait}s (intento {attempt+1}/{max_retries})...")
                time.sleep(wait)
            else:
                trabajo = TrabajoExtraido(
                    job_id=folder_info.get('job_id', ''),
                    cliente=folder_info.get('cliente', ''),
                    confianza='baja',
                    advertencias=[f'Rate limit tras {max_retries} intentos: {e}'],
                )
                trabajo.archivos_fuente = archivos
                return trabajo
        except Exception as e:
            trabajo = TrabajoExtraido(
                job_id=folder_info.get('job_id', ''),
                cliente=folder_info.get('cliente', ''),
                confianza='baja',
                advertencias=[f'Error llamando a Claude: {e}'],
            )
            trabajo.archivos_fuente = archivos
            return trabajo

    if verbose:
        print(f"  Respuesta recibida ({len(response_text)} chars)")

    # Parsear JSON
    data = extract_json_from_response(response_text)

    # Si no hay JSON válido, pedir a Claude que convierta su análisis a JSON
    if not data:
        if verbose:
            print(f"  No se encontró JSON válido, pidiendo conversión...")
        try:
            message2 = _mensaje_streaming(
                client,
                model=model,
                max_tokens=32000,
                messages=[
                    {"role": "user", "content": content},
                    {"role": "assistant", "content": response_text},
                    {"role": "user", "content": (
                        "Tu respuesta anterior contenía el análisis correcto pero no en formato JSON. "
                        "Ahora devuelve ÚNICAMENTE el objeto JSON, comenzando con { y terminando con }. "
                        "Sin ningún texto adicional antes ni después."
                    )}
                ],
                system=SYSTEM_PROMPT,
            )
            response_text = _texto_respuesta(message2)
            data = extract_json_from_response(response_text)
        except Exception as e:
            if verbose:
                print(f"  Error en segundo intento: {e}")

    if not data:
        trabajo = TrabajoExtraido(
            job_id=folder_info.get('job_id', ''),
            cliente=folder_info.get('cliente', ''),
            confianza='baja',
            advertencias=['No se pudo extraer JSON válido tras 2 intentos', response_text[:300]],
        )
        trabajo.archivos_fuente = archivos
        return trabajo

    trabajo = json_to_trabajo(data, folder_info, folder=folder)
    trabajo = _limpiar_trabajo(trabajo)

    # Si materiales vacíos con muchos archivos, reintentar con menos PDFs
    if not trabajo.materiales and len(archivos) > 4:
        if verbose:
            print(f"  Resultado vacío, reintentando con PDFs prioritarios...")
        content2, archivos2 = build_claude_content(folder, verbose=False, max_pdfs=3)
        try:
            msg2 = _mensaje_streaming(
                client,
                model=model, max_tokens=32000, system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content2}]
            )
            data2 = extract_json_from_response(_texto_respuesta(msg2))
            if data2 and data2.get('materiales'):
                trabajo2 = json_to_trabajo(data2, folder_info, folder=folder)
                trabajo2 = _limpiar_trabajo(trabajo2)
                trabajo2.archivos_fuente = archivos2
                trabajo2.advertencias.append(f'Reintento con {len(archivos2)} archivos prioritarios (original tenía {len(archivos)})')
                return trabajo2
        except Exception as e:
            trabajo.advertencias.append(f'Reintento fallido: {e}')

    trabajo.archivos_fuente = archivos
    return trabajo


def _limpiar_trabajo(trabajo: TrabajoExtraido) -> TrabajoExtraido:
    """Limpieza y validación post-extracción."""
    # 1. Eliminar materiales vacíos (frontal/copete = NO)
    trabajo.materiales = [
        m for m in trabajo.materiales
        if m.marca or m.color or m.es_igual_a or m.grosor_cm
    ]

    # 2. Limpiar material_rol con "/" (tomar solo la primera parte)
    for p in trabajo.piezas:
        if '/' in (p.material_rol or ''):
            p.material_rol = p.material_rol.split('/')[0].strip()
            if p.notas:
                p.notas += ' [material_rol simplificado de opción múltiple]'
            else:
                p.notas = '[material_rol simplificado de opción múltiple]'

    # 3. Eliminar piezas sin tipo, sin material_rol, o tipo 'pilar'/'pilastra'
    # (el pilar es un obstáculo del edificio, no una pieza a fabricar — la
    # encimera lleva una muesca alrededor; no se emite pieza separada).
    descartados_pilar = []
    piezas_filtradas = []
    for p in trabajo.piezas:
        if not p.tipo or p.tipo == 'desconocido' or not p.material_rol:
            continue
        if p.tipo.lower().strip() in ('pilar', 'pilastra'):
            descartados_pilar.append(p)
            continue
        piezas_filtradas.append(p)
    trabajo.piezas = piezas_filtradas
    if descartados_pilar:
        trabajo.advertencias.append(
            f"Postproc: descartadas {len(descartados_pilar)} pieza(s) pilar/pilastra "
            "(obstáculos, no se fabrican; la encimera debe llevar muesca)")

    # 4. Normalizar tipo de pieza (chapeado → frontal)
    tipo_map = {
        'chapeado': 'frontal', 'chapeado_pared': 'frontal',
        'chapeado pared': 'frontal', 'revestimiento': 'frontal',
        'cascada': 'costado', 'waterfall': 'costado',
        'rodapie': 'zocalo', 'rodapié': 'zocalo',
        'zócalo': 'zocalo',
    }
    for p in trabajo.piezas:
        p.tipo = tipo_map.get(p.tipo.lower().strip(), p.tipo.lower().strip())

    # 5. Normalizar tipo de canto y eliminar no-cantos
    TIPOS_CANTO_VALIDOS = {
        'recto_pulido', 'recto_pulido_agua', 'ingletado',
        'bisel', 'boleado', 'canto_pilastra',
    }
    canto_map = {
        'recto_pulido': 'recto_pulido', 'recto pulido': 'recto_pulido',
        'recto_pulido_agua': 'recto_pulido_agua',
        'canto recto pulido agua': 'recto_pulido_agua',
        'ml canto recto pulido agua': 'recto_pulido_agua',
        'ingletado': 'ingletado', 'ml ingletado': 'ingletado',
        'bisel': 'bisel', 'boleado': 'boleado',
        'canto_pilastra': 'canto_pilastra', 'ml canto pilastra': 'canto_pilastra',
    }
    cantos_validos = []
    for c in trabajo.cantos:
        tipo_norm = canto_map.get(c.tipo.lower().strip(), c.tipo.lower().strip())
        if tipo_norm in TIPOS_CANTO_VALIDOS:
            c.tipo = tipo_norm
            cantos_validos.append(c)
        else:
            # Elaboraciones mal puestas en cantos → mover a observaciones
            if trabajo.observaciones:
                trabajo.observaciones += f'; {c.tipo}'
            else:
                trabajo.observaciones = c.tipo
    trabajo.cantos = cantos_validos

    return trabajo
