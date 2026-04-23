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
Tu tarea es analizar todos los documentos de una carpeta de trabajo de una empresa de cocinas (Cocimoble) y extraer TODA la información relevante con máxima precisión.

## Tipos de documentos que puedes encontrar:
1. **Plantilla presupuesto marmolista**: Formulario con datos del cliente, material (marca, color, grosor, acabado), tipo de copete, frontal, zócalo, fregadero, elaboraciones y observaciones.
2. **Plano de planta 2D**: Vista desde arriba de la cocina con medidas en mm y etiquetas de tipo de pieza (ENCIMERA, FRONTAL H:XXcm, COPETE H:Xcm, ZOCALO H:Xcm...). Las X sobre muebles indican electrodomésticos sin piedra.
3. **Render/perspectiva 3D**: Imagen visual de la cocina montada. Ayuda a entender disposición, pilares, islas, cascadas, y confirmar dónde van los distintos materiales y piezas.
4. **Presupuesto del marmolista** (PDF con membrete "Mármoles y Granitos Redondela"): Lista de líneas con: material, dimensiones (Longo x Ancho), tipo de elaboración, precio.
5. **Excel de presupuesto**: Similar al PDF pero en tabla Excel con columnas Ref, Produto, Descrición, Longo, Ancho, Unid., m²/Cantid., Prezo, Total.
6. **TXT extra**: Notas adicionales del cliente o comentarios.

## TIPOS DE PIEZAS — DEFINICIONES PRECISAS:

### Encimera
Superficie horizontal sobre los muebles bajos. Profundidad estándar 600mm salvo indicación contraria.
Puede tener forma rectangular, en L, en U, con entrantes por electrodomésticos (marcados con X en plano).
También puede ser isla o península independiente.

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
   - **Posición**: placa típicamente cerca del centro (40-60% del largo); fregadero hacia un extremo (20-30% desde un lado); grifo justo detrás del fregadero.
   - **Tamaño (usar estos defaults)**:
     - placa inducción 4 zonas: **600×520 mm**
     - placa inducción 2 zonas / dominó: **300×520 mm**
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
    {"tipo": "encimera", "material_rol": "encimera", "largo_mm": 2180, "ancho_mm": 610, "zona": "pared superior izquierda"},
    {"tipo": "encimera", "material_rol": "encimera", "largo_mm": 990, "ancho_mm": 600, "zona": "pared superior derecha"},
    {"tipo": "frontal", "material_rol": "frontal", "largo_mm": 2180, "altura_mm": 580, "zona": "segmento 1"},
    {"tipo": "frontal", "material_rol": "frontal", "largo_mm": 195, "altura_mm": 580, "zona": "pilar cara izquierda"},
    {"tipo": "frontal", "material_rol": "frontal", "largo_mm": 360, "altura_mm": 580, "zona": "segmento 2"},
    {"tipo": "frontal", "material_rol": "frontal", "largo_mm": 150, "altura_mm": 580, "zona": "pilar cara derecha"},
    {"tipo": "frontal", "material_rol": "frontal", "largo_mm": 330, "altura_mm": 580, "zona": "segmento 3"},
    {"tipo": "frontal", "material_rol": "frontal", "largo_mm": 340, "altura_mm": 580, "zona": "pilar cara fondo"},
    {"tipo": "frontal", "material_rol": "frontal", "largo_mm": 990, "altura_mm": 580, "zona": "segmento 4"},
    {"tipo": "copete", "material_rol": "copete", "longitud_ml": 4.545, "altura_mm": 50, "notas": "rodea pilar igual que frontal"},
    {"tipo": "zocalo", "material_rol": "zocalo", "longitud_ml": 3.18, "altura_mm": 100, "zona": "pared superior muebles bajos"},
    {"tipo": "zocalo", "material_rol": "zocalo", "longitud_ml": 0.60, "altura_mm": 100, "zona": "lateral"}
  ],
  "huecos": [
    {"tipo": "placa", "cantidad": 1, "pieza_zona": "pared superior izquierda", "distancia_lado_mm": 2200, "largo_mm": 600, "ancho_mm": 520},
    {"tipo": "fregadero", "cantidad": 1, "subtipo": "sobre_encimera", "pieza_zona": "pared superior izquierda", "distancia_lado_mm": 900, "largo_mm": 780, "ancho_mm": 480},
    {"tipo": "grifo", "cantidad": 1, "pieza_zona": "pared superior izquierda", "distancia_lado_mm": 900},
    {"tipo": "enchufe", "cantidad": 1, "pieza_zona": "pared superior izquierda"}
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
        piezas.append(Pieza(
            tipo=p.get('tipo', 'desconocido'),
            material_rol=p.get('material_rol', 'encimera'),
            largo_mm=safe_float(p.get('largo_mm')),
            ancho_mm=safe_float(p.get('ancho_mm')),
            altura_mm=safe_float(p.get('altura_mm')),
            area_m2=safe_float(p.get('area_m2')),
            longitud_ml=safe_float(p.get('longitud_ml')),
            zona=p.get('zona'),
            forma=p.get('forma'),
            notas=p.get('notas'),
        ))

    # Huecos
    huecos = []
    for h in data.get('huecos', []):
        huecos.append(Hueco(
            tipo=h.get('tipo', 'desconocido'),
            cantidad=safe_int(h.get('cantidad')) or 1,
            pieza_zona=h.get('pieza_zona'),
            posicion=h.get('posicion'),
            distancia_lado_mm=h.get('distancia_lado_mm'),
            largo_mm=h.get('largo_mm'),
            ancho_mm=h.get('ancho_mm'),
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
    return trabajo


def extract_trabajo(
    folder: Path,
    api_key: Optional[str] = None,
    model: str = "claude-sonnet-4-6",
    verbose: bool = True,
) -> TrabajoExtraido:
    """
    Función principal: dado un Path de carpeta, extrae todos los datos.
    """
    if verbose:
        print(f"\n[Procesando] {folder.name}")

    # Info básica del nombre de la carpeta
    folder_info = parse_folder_name(folder.name)

    # Construir contenido para Claude
    content, archivos = build_claude_content(folder, verbose=verbose)

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
