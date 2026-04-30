# Guía de Identificación de Elementos en Planos de Diseño de Cocinas

> Documento de referencia para que un sistema LLM (Claude) **identifique y localice
> correctamente** los elementos, símbolos y figuras presentes en los planos de cocina
> generados por programas de diseño de mobiliario.
>
> **Alcance**: solo identificación y ubicación. Este documento NO trata sobre cómo
> extraer ni interpretar medidas; eso queda para el modelo decidir caso por caso
> según la tarea que se le pida.

---

## 1. Tipo de vista

Los planos de cocina suelen mezclar dos representaciones en una misma lámina:

- **Vista en alzado (elevación frontal)**: los muebles vistos de frente, como
  si estuvieras delante de la pared.
- **Vista en planta (cenital) embebida**: encimera, fregadero y placa de cocina
  suelen dibujarse vistos desde arriba, superpuestos sobre la elevación.

Esta mezcla es **habitual y deliberada**, no un error del dibujo.

---

## 2. Cómo identificar cada tipo de mueble

### 2.1 Muebles altos (colgados / armarios de pared)

- **Cómo se ven**: rectángulo con una **X** (aspas) dibujada en su interior.
- **Posición típica**: zona superior del plano, alineados horizontalmente.
- **Variantes**:
  - Una X simple dentro de un rectángulo = un módulo alto cerrado.
  - Varios rectángulos con X apilados verticalmente = columna alta o conjunto de
    altos colocados unos sobre otros.
  - X con líneas adicionales dentro = módulo con cristal, vitrina o frente especial.

### 2.2 Muebles bajos

- **Cómo se ven**: rectángulo **sin X**, normalmente con una línea horizontal por
  encima que representa la encimera.
- **Posición típica**: zona inferior del plano, apoyados sobre la línea del suelo.
- **Pista visual**: si encima del rectángulo hay una placa, fregadero o lavavajillas
  dibujado en planta, casi seguro es un mueble bajo.

### 2.3 Columnas (torres altas)

- **Cómo se ven**: rectángulo o serie de rectángulos apilados que ocupan toda la
  altura del plano, normalmente desde el suelo (o rodapié) hasta cerca del techo.
- **Pista visual**: a diferencia de los altos, **arrancan desde el suelo** y suelen
  llevar X en cada compartimento.

### 2.4 Cajoneras

- **Cómo se ven**: rectángulo bajo dividido en varias franjas horizontales por
  líneas finas. Cada franja = un cajón.
- **Pista visual**: contar las divisiones horizontales para saber el número de
  cajones.

### 2.5 Estanterías o entrepaños vistos

- **Cómo se ven**: módulo abierto, sin X, con líneas horizontales internas que
  representan baldas. A veces línea discontinua si la balda no es estructural.

---

## 3. Líneas y rellenos: cómo distinguir cada uno

| Elemento gráfico | Qué representa | Dónde aparece |
|---|---|---|
| Línea continua negra fina | Contorno de los muebles | Por todo el dibujo |
| Línea continua negra gruesa | Estructura del muro / pared | Perímetro |
| Línea discontinua (a trazos) | Elemento oculto, balda interna o swing de puerta | Dentro de muebles o en plantas |
| Línea discontinua rosa/magenta | **Anotación manual del muro** marcado a mano | Perímetro, sobre el dibujo original |
| Resaltado verde fluorescente | Zona de encimera marcada a mano | Sobre el frente del mueble bajo |
| Resaltado naranja/rosa fluorescente | Zona destacada por el técnico | Variable |
| Flechas a mano con texto | **Anotaciones manuales posteriores** | Variable |

### 3.1 Diferenciar dibujo CAD original vs. anotaciones manuales

Es **crítico** que el modelo distinga entre:

- **Dibujo CAD original**: líneas finas, colores planos negro/gris, cotas mecanografiadas, trazos limpios.
- **Anotaciones a mano**: rotulador fluorescente (verde, naranja, rosa), bolígrafo,
  trazos irregulares, números escritos a mano, flechas dibujadas.

Las anotaciones manuales son **correcciones, validaciones o aclaraciones** añadidas
por el técnico tras imprimir el plano. **Prevalecen sobre el plano original cuando
hay conflicto**, porque suelen reflejar la realidad de la obra o decisiones tomadas
después del diseño inicial.

---

## 4. Cómo identificar electrodomésticos y elementos de cocina

### 4.1 Placa de cocina (encimera de cocción)

- **Cómo se ve**: rectángulo con varios círculos pequeños en su interior (los fuegos).
- **Configuraciones típicas**:
  - 4 círculos en disposición 2×2 → placa de 4 fuegos.
  - 5 círculos (4 + 1 central) → placa de 5 fuegos o con quemador wok.
  - 2 círculos en línea → encimera modular (dominó).
- **Pista visual**: aparece dibujada en **planta cenital**, embebida en el alzado.

### 4.2 Horno

- **Cómo se ve**: cuadrado o rectángulo con uno o dos rectángulos internos que
  representan la puerta y los mandos.
- **Posición habitual**:
  - Bajo la placa, encastrado en mueble bajo.
  - En columna, a media altura.
- **Pista visual**: si ves una placa con un cuadrado justo debajo, casi seguro es
  el horno.

### 4.3 Fregadero

- **Cómo se ve**: rectángulo grande con una o dos cubetas dibujadas (rectángulos
  interiores con esquinas redondeadas), y a veces el grifo representado como un
  círculo o pequeña línea perpendicular fuera de la cubeta.
- **Variantes**:
  - 1 cubeta → fregadero simple.
  - 2 cubetas iguales → fregadero doble.
  - 1 cubeta + escurridor (zona plana sin cubeta) → fregadero con escurridor.
- **Vista**: cenital, embebida en el alzado.

### 4.4 Campana extractora

- **Cómo se ve**: rectángulo en la parte superior, sobre la placa, frecuentemente
  con líneas internas representando el frente o filtros.
- **Variantes**: decorativa (visible), integrada (oculta dentro de mueble alto),
  isla (colgada del techo).

### 4.5 Frigorífico, lavavajillas, lavadora, microondas

- **Cómo se ven**: módulo (alto o bajo) **con etiqueta textual identificativa**:
  - `FRI` / `FRIG` → frigorífico
  - `CONG` → congelador
  - `LV` / `LVJ` → lavavajillas
  - `LAV` / `LV` → lavadora
  - `MO` / `MW` → microondas
  - `HRN` / `H` → horno
- A veces se marca con una cruz diagonal simple (no la X de mueble alto, sino una
  cruz de extremo a extremo del rectángulo) para indicar "aparato encastrado".

### 4.6 Vinotecas, cafeteras encastradas, calientaplatos

- Aparecen como módulos pequeños, normalmente en columna, con etiqueta textual
  o icono específico del programa.

---

## 5. Cómo identificar elementos constructivos

### 5.1 Muros / paredes

- **Cómo se ven**: línea continua gruesa (a veces doble línea) que delimita el
  perímetro de la cocina.
- **Pista visual**: las paredes son lo que **encierra** todo el dibujo. Suelen
  aparecer en los extremos izquierdo, derecho y a veces detrás del mobiliario.

### 5.2 Ventanas

- **Cómo se ven en alzado**: rectángulo más fino que la pared, con líneas internas
  horizontales (representando los cristales).
- **Cómo se ven en planta**: tres líneas paralelas dentro del grosor del muro.
- **Posición típica**: sobre la encimera (entre muebles bajos y altos) o en zonas
  sin mobiliario.

### 5.3 Puertas

- **Cómo se ven en planta**: hueco en la pared con un cuarto de círculo (arco)
  que indica el sentido de apertura.
- **Cómo se ven en alzado**: rectángulo vertical, a veces con línea diagonal o
  manilla representada.

### 5.4 Rodapié / zócalo

- **Cómo se ve**: franja horizontal estrecha en la base de los muebles bajos,
  separada de estos por una línea.
- **Pista visual**: es lo más cercano al suelo, debajo del cuerpo del mueble bajo.

### 5.5 Encimera

- **Cómo se ve**: línea horizontal con cierto grosor que recorre la parte superior
  de los muebles bajos.
- **Pista visual**: separa visualmente los muebles bajos de la zona libre superior
  o de los muebles altos. En este tipo de planos, se suele resaltar a mano con
  rotulador verde.

### 5.6 Costados vistos

- **Cómo se ven**: extremos laterales del conjunto de muebles que dan a una zona
  abierta (paso, pared sin mueble continuo).
- **Pista visual**: el primer y último mueble de cada tramo, si no tienen otro
  mueble pegado en ese lado.

### 5.7 Copete / remate superior

- **Cómo se ve**: franja horizontal sobre los muebles altos, hasta el techo o
  hasta una determinada altura, a veces con tramado o sombreado.
- **Pista visual**: aparece **encima** de los muebles altos.

### 5.8 Faldón / falsa cajonera

- **Cómo se ve**: panel vertical entre la encimera y el suelo en zonas donde no
  hay mueble bajo (por ejemplo bajo una placa con horno separado, o en frente de
  un lavavajillas para uniformar).

---

## 6. Numeración y referencias en el plano

### 6.1 Números dentro de círculos (① ② ③ ④ …)

- **Qué representan**: paredes o tramos numerados de la cocina.
- **Función**: vincular cada vista de alzado con la planta general. La pared ①
  del alzado corresponde a la pared ① marcada en la planta.
- **Posición típica**: esquinas o extremos de cada tramo dibujado.

### 6.2 Códigos textuales que pueden aparecer

| Código frecuente | Probable significado |
|---|---|
| `BAJOS` | Conjunto de muebles bajos |
| `ALTOS` | Conjunto de muebles altos |
| `COL` / `COLUMNA` | Mueble columna |
| `ENC` / `ENCIMERA` | Plano de trabajo |
| `RP` / `ROD` | Rodapié |
| `PERSPECTIVA` / `PERSP` | Referencia a una vista 3D |
| `H` | Habitualmente "Altura" |
| `A`, `Anc` | Habitualmente "Ancho" |
| `P`, `Prof` | Habitualmente "Profundidad" |
| `FRI`, `LV`, `LAV`, `MO`, `HRN` | Electrodomésticos (ver §4.5) |

> **Nota**: la nomenclatura concreta varía mucho entre programas (TopSolid, Imos,
> Cabinet Vision, KD Max, Compusoft Winner, 2020 Design, SketchUp, etc.) y entre
> empresas. El modelo debe **interpretar siempre en contexto** y, ante duda,
> indicar el código tal cual aparece sin asumir significado.

---

## 7. Glosario rápido (ES → concepto)

| Término | Concepto |
|---|---|
| **Encimera** | Plano de trabajo (countertop) sobre los muebles bajos |
| **Chapeado / canto** | Acabado del borde visible de la encimera o tablero |
| **Rodapié / zócalo** | Tira inferior bajo los muebles bajos que oculta las patas |
| **Muebles bajos** | Módulos apoyados en el suelo |
| **Muebles altos / colgados** | Módulos fijados a la pared, por encima de la encimera |
| **Columna / torre** | Mueble alto de altura completa |
| **Costado vista** | Lateral de mueble que queda expuesto y va chapeado |
| **Frente** | Cara delantera del mueble (puerta o cajón) |
| **Cuerpo / carcasa** | Estructura del mueble sin frentes ni encimera |
| **Faldón** | Panel decorativo entre encimera y suelo cuando no hay mueble |
| **Copete** | Remate superior de muebles altos hasta el techo |
| **Tirador** | Manilla del frente |
| **Bisagra** | Articulación de la puerta |
| **Junquillo** | Listón decorativo o de remate |

---

## 8. Procedimiento de identificación recomendado

Cuando el modelo recibe un plano, debe seguir este orden de **localización**:

1. **Identificar tipo de vista** (alzado / planta / mixta).
2. **Localizar el contorno** (muros: línea gruesa o anotación manual rosa).
3. **Identificar cada pared o tramo** y su número en círculo (① ② ③…).
4. **Para cada tramo**:
   - Localizar muebles altos (con X) en la zona superior.
   - Localizar muebles bajos (sin X, con encimera encima) en la zona inferior.
   - Localizar columnas (de suelo a techo, con X interna).
   - Localizar electrodomésticos por su silueta o etiqueta.
5. **Localizar elementos constructivos**: encimera, rodapié, copete, costados vistos, faldones, ventanas, puertas.
6. **Marcar las anotaciones manuales** (resaltadores, flechas, texto a mano) como zona de atención prioritaria.
7. **Reportar lo identificado** dejando que el sistema/usuario decida qué hacer
   con esa información (extraer cotas, generar un listado, contar piezas, etc.).

> **Principio rector**: este documento entrena la **identificación**, no la
> interpretación. Una vez los elementos están localizados, la decisión sobre qué
> medir, contar o procesar se toma en la conversación con el usuario.

---

## 9. Recursos visuales de referencia

Para complementar este documento con bibliotecas de símbolos y ejemplos visuales:

**En español:**
- Edraw – Símbolos de cocina: https://www.edrawsoft.com/es/symbols/kitchen.html
- Edraw – Símbolos de planos y significados: https://edraw.wondershare.es/floor-plan-tips/floor-plan-symbols-and-meanings.html
- Como Organizar la Casa – Mobiliario en planos: https://comoorganizarlacasa.com/representacion-de-mobiliario-en-planos-arquitectonicos/

**En inglés (más técnicas):**
- NKBA Cap. 4 – Universal Presentation Standards: https://elearning.nkba.org/wp-content/uploads/2023/10/Chapter-4-Universal-Presentation-Standards.pdf
- NKBA Cap. 12 – Interior Elevations for Kitchens & Baths: https://elearning.nkba.org/wp-content/uploads/2023/12/Chapter-12-Interior-Elevations-for-Kitchens-and-Baths-1.pdf
- Life of an Architect – Graphic Standards for Cabinetry: https://www.lifeofanarchitect.com/graphic-standards-for-architectural-cabinetry/
- Foyr – Floor Plan Symbols: https://foyr.com/learn/floor-plan-symbols/
- BigRentz – Floor Plan Symbols and Abbreviations: https://www.bigrentz.com/blog/floor-plan-symbols
