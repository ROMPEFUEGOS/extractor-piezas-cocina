"""
Modelos de datos para el extractor de piezas de cocinas de piedra.
"""
from dataclasses import dataclass, field
from typing import Optional
import json


@dataclass
class MaterialSpec:
    """Especificación de un material (puede haber varios en un trabajo)."""
    rol: str                          # encimera, frontal, copete, zocalo, chapeado, pilastra...
    marca: Optional[str] = None
    color: Optional[str] = None
    grosor_cm: Optional[float] = None
    acabado: Optional[str] = None     # pulido, mate, natural, bocciardato, suede...
    altura_cm: Optional[float] = None # para frontales, copetes, zocalos
    canto: Optional[str] = None       # recto, bisel, boleado...
    es_igual_a: Optional[str] = None  # "encimera" si copia el material de otro rol
    notas: Optional[str] = None


@dataclass
class Pieza:
    """
    Una pieza individual de piedra a fabricar — diseñada como polígono 2D real.

    Tipos posibles:
      encimera   — superficie horizontal sobre muebles bajos
      frontal    — panel vertical entre encimera y muebles altos (= chapeado = chapeado pared)
      copete     — franja estrecha pegada a la pared encima de la encimera
      zocalo     — franja al pie de los muebles bajos
      costado    — panel lateral vertical de isla/encimera (= cascada/waterfall)
      pilastra   — revestimiento de arista/canto de pilar
      isla       — encimera de isla central independiente
      paso/tabica — escalón
      otro       — pieza especial

    REPRESENTACIÓN GEOMÉTRICA:
    Cada pieza se diseña en 2D como un POLÍGONO. Los vértices van en orden
    (CCW recomendado, aunque CW también vale). Para piezas rectangulares
    estándar, son 4 vértices. Para L: 6 vértices. Para U: 8 vértices. Para
    encimera con entrada de pilar: 6-8 vértices con la muesca.

    Ejemplo encimera en L (3870 largo + 545 saliente al final con fondo 280):
        vertices_mm = [[0,0], [3870,0], [3870,280], [3325,280], [3325,620], [0,620]]
    """
    tipo: str
    material_rol: str
    # Forma: 'rectangulo' | 'L' | 'U' | 'poligono' | 'pilar_entry' | 'isla' | etc.
    forma: Optional[str] = None
    # Vértices del polígono en mm, relativo a esquina inferior-izquierda (0,0)
    vertices_mm: Optional[list] = None  # ej: [[0,0],[2700,0],[2700,600],[0,600]]
    # Clasificación de contacto POR ARISTA, alineada con vertices_mm: la
    # entrada i describe la arista que va del vértice i al i+1.
    # Valores: 'pared' | 'vista' | 'mueble' | 'ventana'. La emite Claude
    # leyendo los muros del plano; el postproc la usa como verdad si no hay
    # trazos del operador (que siempre tienen prioridad).
    aristas_contacto: Optional[list] = None
    # Cota REAL leída del plano para cada arista (mm), alineada con
    # vertices_mm; null donde el plano no acota esa arista. NUNCA derivada
    # por aritmética. Permite al postproc verificar el CIERRE del polígono
    # (Σ por eje) y reconstruir croquis no-a-escala desde la topología del
    # contorno del operador + cotas.
    aristas_cota: Optional[list] = None
    # Bounding box (calculable de vertices, también se admite directo si no hay polígono)
    largo_mm: Optional[float] = None    # bounding box X
    ancho_mm: Optional[float] = None    # bounding box Y (fondo)
    altura_mm: Optional[float] = None   # solo para piezas verticales (frontal/copete/zócalo/costado)
    area_m2: Optional[float] = None
    longitud_ml: Optional[float] = None
    # Contexto
    zona: Optional[str] = None
    notas: Optional[str] = None


@dataclass
class Hueco:
    """Un hueco o elaboración en la encimera/pieza, con posición 2D real."""
    tipo: str          # placa, fregadero, grifo, enchufe, dosificador
    cantidad: int = 1
    pieza_zona: Optional[str] = None       # zona de la pieza a la que pertenece el hueco
    # Posición 2D del centro del hueco, relativa a esquina inferior-izquierda
    # del bounding box de la pieza a la que pertenece (mm).
    centro_x_mm: Optional[float] = None
    centro_y_mm: Optional[float] = None
    # Dimensiones del hueco
    largo_mm: Optional[float] = None
    ancho_mm: Optional[float] = None
    # Compatibilidad legacy (distancia desde borde izquierdo del frente al centro)
    posicion: Optional[str] = None
    distancia_lado_mm: Optional[float] = None
    # Atributos
    subtipo: Optional[str] = None    # bajo_encimera, sobre_encimera, enrasado
    notas: Optional[str] = None


@dataclass
class Canto:
    """Tratamiento de canto (arista) de una pieza."""
    tipo: str           # recto_pulido, ingletado, bisel, boleado, pilastra, canto_recto_agua
    longitud_ml: Optional[float] = None
    notas: Optional[str] = None


@dataclass
class TrabajoExtraido:
    """Datos completos extraídos de una carpeta de trabajo."""
    # Identificación
    job_id: str
    cliente: str
    tienda: Optional[str] = None
    vendedor: Optional[str] = None
    direccion: Optional[str] = None
    ciudad: Optional[str] = None
    fecha: Optional[str] = None
    piso: Optional[str] = None

    # Materiales (puede haber varios en "varios materiales")
    materiales: list = field(default_factory=list)  # lista de MaterialSpec

    # Piezas a fabricar (cada una es un rectángulo a cortar de la tabla)
    # tipos: encimera, frontal (=chapeado), copete, zocalo, pilastra, isla, cascada, paso...
    piezas: list = field(default_factory=list)      # lista de Pieza

    # Huecos / elaboraciones
    huecos: list = field(default_factory=list)       # lista de Hueco

    # Cantos y tratamientos
    cantos: list = field(default_factory=list)       # lista de Canto

    # Opciones adicionales
    tipo_cascada: Optional[str] = None   # recta, ingletada
    fregadero_tipo: Optional[str] = None # bajo_encimera, sobre_encimera, enrasado_optico
    tablas_reservadas: Optional[bool] = None
    proveedor_tablas: Optional[str] = None

    # Observaciones y contexto
    observaciones: Optional[str] = None
    notas_extra: Optional[str] = None    # de TXT extra
    confianza: str = "alta"              # alta, media, baja - cuánta confianza hay en la extracción
    advertencias: list = field(default_factory=list)  # lista de strings con avisos

    # Archivos fuente usados
    archivos_fuente: list = field(default_factory=list)

    def to_dict(self) -> dict:
        def _conv(obj):
            if hasattr(obj, '__dataclass_fields__'):
                # Saltar atributos privados también en dataclasses anidadas
                # (p.ej. Pieza._anot_reg con datos píxel del anotador)
                return {k: _conv(v) for k, v in obj.__dict__.items()
                        if v is not None and not k.startswith('_')}
            elif isinstance(obj, list):
                # Filtrar None y contenedores vacíos dentro de listas
                # (robustez del round-trip JSON)
                convertidos = [_conv(i) for i in obj]
                return [x for x in convertidos if x is not None and x != {}]
            return obj

        d = {}
        for k, v in self.__dict__.items():
            # Saltar atributos privados (cache de postproc, etc.) — no se serializan.
            if k.startswith('_'):
                continue
            converted = _conv(v)
            if converted is not None and converted != [] and converted != {}:
                d[k] = converted
        return d

    def to_json(self, indent=2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def resumen_texto(self) -> str:
        """Genera un resumen legible del trabajo."""
        lines = []
        lines.append(f"=== TRABAJO {self.job_id} - {self.cliente} ===")
        if self.fecha:
            lines.append(f"Fecha: {self.fecha}")
        if self.vendedor:
            lines.append(f"Vendedor: {self.vendedor} | Tienda: {self.tienda}")
        if self.ciudad:
            lines.append(f"Dirección: {self.ciudad}" + (f" {self.piso}" if self.piso else ""))

        lines.append("")
        lines.append("--- MATERIALES ---")
        for m in self.materiales:
            rol = m.rol.upper()
            desc = f"  [{rol}] "
            if m.es_igual_a:
                desc += f"= igual a {m.es_igual_a}"
            else:
                parts = []
                if m.marca: parts.append(m.marca)
                if m.color: parts.append(m.color)
                if m.grosor_cm: parts.append(f"{m.grosor_cm}cm")
                if m.acabado: parts.append(m.acabado)
                desc += " | ".join(parts)
            if m.altura_cm:
                desc += f" | H:{m.altura_cm}cm"
            if m.canto:
                desc += f" | Canto:{m.canto}"
            lines.append(desc)

        lines.append("")
        lines.append("--- PIEZAS ---")
        for p in self.piezas:
            desc = f"  [{p.tipo.upper()}] mat:{p.material_rol}"
            dims = []
            if p.largo_mm: dims.append(f"L:{p.largo_mm}mm")
            if p.ancho_mm: dims.append(f"A:{p.ancho_mm}mm")
            if p.altura_mm: dims.append(f"H:{p.altura_mm}mm")
            if p.area_m2: dims.append(f"{p.area_m2:.3f}m²")
            if p.longitud_ml: dims.append(f"{p.longitud_ml:.3f}ml")
            if dims: desc += " | " + ", ".join(dims)
            if p.zona: desc += f" | zona:{p.zona}"
            if p.notas: desc += f" | {p.notas}"
            lines.append(desc)

        if self.huecos:
            lines.append("")
            lines.append("--- HUECOS / ELABORACIONES ---")
            for h in self.huecos:
                desc = f"  {h.cantidad}x {h.tipo.upper()}"
                if h.subtipo: desc += f" ({h.subtipo})"
                if h.posicion: desc += f" [{h.posicion}]"
                lines.append(desc)

        if self.cantos:
            lines.append("")
            lines.append("--- CANTOS ---")
            for c in self.cantos:
                desc = f"  {c.tipo}"
                if c.longitud_ml: desc += f": {c.longitud_ml}ml"
                lines.append(desc)

        if self.observaciones:
            lines.append("")
            lines.append(f"--- OBSERVACIONES ---")
            lines.append(f"  {self.observaciones}")

        if self.advertencias:
            lines.append("")
            lines.append("--- ADVERTENCIAS ---")
            for w in self.advertencias:
                lines.append(f"  ⚠ {w}")

        lines.append(f"\nConfianza extracción: {self.confianza}")
        return "\n".join(lines)
