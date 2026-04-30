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
2. **Si la plantilla muestra la posición gráficamente pero sin cota** → estimar proporcionalmente a la medida total de la encimera.
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
      "largo_mm": 3000, "ancho_mm": 600,
      "zona": "pared norte principal con saliente pilar trasero derecho"
    },
    {
      "tipo": "isla", "material_rol": "encimera",
      "forma": "rectangulo",
      "vertices_mm": [[0,0],[2200,0],[2200,900],[0,900]],
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


def extract_json_from_response(text: str) -> Optional[dict]:
    """Extrae el JSON de la respuesta de Claude, con múltiples estrategias."""
    # 1. Bloque ```json ... ```
    match = re.search(r'```json\s*([\s\S]+?)\s*```', text, re.IGNORECASE)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

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


def json_to_trabajo(data: dict, folder_info: dict) -> TrabajoExtraido:
    """Convierte el dict extraído en un objeto TrabajoExtraido."""

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
        piezas.append(Pieza(
            tipo=p.get('tipo', 'desconocido'),
            material_rol=p.get('material_rol', 'encimera'),
            forma=p.get('forma'),
            vertices_mm=verts,
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
    _completar_copete_principal(trabajo)
    _completar_ingletes_implicitos(trabajo)
    return trabajo


def _completar_copete_principal(trabajo: TrabajoExtraido) -> None:
    """Si la encimera tiene copetes de cabeza vista (cortos, ≈ fondo encimera)
    pero NO existe el copete principal contra la pared (largo de la encimera),
    se añade. Es un caso muy frecuente de omisión por el LLM.

    Heurística: por cada encimera horizontal con largo > 1.5m, si hay 1 o más
    copetes pequeños (≤ ancho encimera + 100mm de tolerancia) y NO hay copete
    de longitud cercana al largo de la encimera, emitir el copete principal.
    """
    copetes = [p for p in trabajo.piezas if p.tipo == 'copete']
    if not copetes:
        return  # Plantilla decía copete=NO, no inventar
    encimeras = [p for p in trabajo.piezas
                 if p.tipo in ('encimera', 'isla') and p.largo_mm and p.ancho_mm]
    nuevos: list[Pieza] = []
    for enc in encimeras:
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
        # Si ya hay un inglete que mencione esta cabeza, no duplicar
        ya_existe = any(
            (c2.tipo == 'ingletado'
             and (c2.notas or '').lower().find(notas_lower[:25]) >= 0)
            for c2 in trabajo.cantos
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
            message = client.messages.create(
                model=model,
                max_tokens=8000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}]
            )
            response_text = message.content[0].text
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
            message2 = client.messages.create(
                model=model,
                max_tokens=8000,
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
            response_text = message2.content[0].text
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

    trabajo = json_to_trabajo(data, folder_info)
    trabajo = _limpiar_trabajo(trabajo)

    # Si materiales vacíos con muchos archivos, reintentar con menos PDFs
    if not trabajo.materiales and len(archivos) > 4:
        if verbose:
            print(f"  Resultado vacío, reintentando con PDFs prioritarios...")
        content2, archivos2 = build_claude_content(folder, verbose=False, max_pdfs=3)
        try:
            msg2 = client.messages.create(
                model=model, max_tokens=8000, system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content2}]
            )
            data2 = extract_json_from_response(msg2.content[0].text)
            if data2 and data2.get('materiales'):
                trabajo2 = json_to_trabajo(data2, folder_info)
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

    # 3. Eliminar piezas sin tipo o sin material_rol
    trabajo.piezas = [
        p for p in trabajo.piezas
        if p.tipo and p.tipo != 'desconocido' and p.material_rol
    ]

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
