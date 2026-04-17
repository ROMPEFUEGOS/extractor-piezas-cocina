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
    Una pieza individual de piedra a fabricar (rectángulo a cortar de la tabla).

    Tipos posibles:
      encimera   — superficie horizontal sobre muebles bajos
      frontal    — panel vertical entre encimera y muebles altos (= chapeado = chapeado pared)
      copete     — franja estrecha pegada a la pared encima de la encimera (H típica 5cm)
      zocalo     — franja al pie de los muebles bajos (H típica 10cm), excluye nevera/lavadora/zona banquetas
      costado    — panel lateral vertical de isla/encimera que cae al suelo (= cascada/waterfall)
      pilastra   — revestimiento de arista/canto de pilar
      isla       — encimera de isla central independiente
      paso       — huella de escalón
      tabica     — tabica de escalón
      otro       — pieza especial indicada en observaciones
    """
    tipo: str
    material_rol: str                  # qué material usa (rol en MaterialSpec)
    # Dimensiones
    largo_mm: Optional[float] = None
    ancho_mm: Optional[float] = None
    altura_mm: Optional[float] = None  # para piezas verticales
    area_m2: Optional[float] = None    # calculado o leído directamente
    longitud_ml: Optional[float] = None  # para copetes, zocalos, ingletados
    # Contexto
    zona: Optional[str] = None         # "pared norte", "isla", "peninsula"...
    forma: Optional[str] = None        # rectangular, L, U, irregular, hueco_encimera
    notas: Optional[str] = None


@dataclass
class Hueco:
    """Un hueco o elaboración en la encimera/pieza."""
    tipo: str          # placa, fregadero, grifo, enchufe, bajo_encimera, enrasado_optico
    cantidad: int = 1
    posicion: Optional[str] = None   # izquierda, derecha, centro
    subtipo: Optional[str] = None    # bajo_encimera, sobre_encimera, enrasado para fregadero
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
                return {k: _conv(v) for k, v in obj.__dict__.items() if v is not None}
            elif isinstance(obj, list):
                return [_conv(i) for i in obj]
            return obj

        d = {}
        for k, v in self.__dict__.items():
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
