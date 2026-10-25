"""Los roles que un usuario puede tener dentro de una organización (HU07).

El catálogo sale del DISEÑO (Sprint 1, sección HU07): seis roles con la
descripción exacta de sus permisos, que es lo que la pantalla pinta bajo cada
opción.

⚠️ **No coincide con `security.roles` de la base**, que hoy tiene otros cuatro
(ADMIN, CONFIGURATOR, REVIEWER, QUERY) heredados de antes del diseño. Se eligió
seguir al diseño porque es la decisión de producto y la tabla todavía no se usa
para esto. Hay que pedirle al DBA que siembre estos seis; está anotado en
`docs/solicitudes-dba.md`.

`ADMIN` sí existe y vive aparte: es el administrador de la organización, el que
nace con ella (HU02). No se ofrece en el selector —no se "asigna" desde aquí—,
pero sí se muestra en el listado.
"""

ADMIN = "ADMIN"

# El orden es el del diseño, de arriba abajo.
CATALOGO: list[dict[str, str]] = [
    {
        "codigo": "SUPERVISOR",
        "nombre": "Supervisor",
        "descripcion": (
            "Monitorea cola HITL, métricas del equipo, reasigna casos, panel de calidad del "
            "pipeline, ve log de actividad de otros usuarios, marca alertas como revisadas."
        ),
    },
    {
        "codigo": "ANALISTA",
        "nombre": "Analista",
        "descripcion": (
            "Revisa cola HITL, corrige campos, resuelve casos estándar, consulta expediente, "
            "búsqueda semántica, exporta resultados de búsqueda."
        ),
    },
    {
        "codigo": "OPERADOR",
        "nombre": "Operador",
        "descripcion": (
            "Carga documentos manualmente, edita metadata básica, descarga archivo original, "
            "consulta bandeja de documentos."
        ),
    },
    {
        "codigo": "COMPLIANCE_OFFICER",
        "nombre": "Compliance Officer",
        "descripcion": (
            "Resuelve casos de alerta PLD, atiende solicitudes ARCO, ve campos sin enmascarar, "
            "exporta reportes de auditoría, ve eventos PLD del audit trail."
        ),
    },
    {
        "codigo": "AUDITOR",
        "nombre": "Auditor",
        "descripcion": (
            "Revisa trazabilidad de decisiones, línea de tiempo de auditoría (eventos públicos), "
            "ve OT-01 y OT-02 sin poder descargar, exporta reportes de auditoría."
        ),
    },
    {
        "codigo": "VIEWER",
        "nombre": "Viewer",
        "descripcion": (
            "Consulta OT-01 y OT-02 en modo lectura sin descarga, ve historial de firmas y línea "
            "de tiempo de auditoría (eventos públicos únicamente)."
        ),
    },
]

#: Los roles que pueden DESCARTAR un documento de la bandeja de preparación (HU «Descartar archivos»): los que trabajan la
#: bandeja. Compliance, auditor y viewer son de consulta y no pueden destruir el trabajo de otro. Es una decisión de producto
#: pendiente de confirmar con César; el administrador de plataforma siempre puede (en su espacio).
PUEDEN_DESCARTAR = frozenset({ADMIN, "SUPERVISOR", "ANALISTA", "OPERADOR"})

#: Los roles que pueden ADMINISTRAR lo que se configura para que un tercero use NexusDoc y lo que define cómo se procesa: API Keys,
#: webhooks, procesos (motor de reglas) y la biblioteca de tipos documentales (crear, cambiar, ordenar, borrar). Antes del 2026-10-22
#: bastaba con pertenecer a la organización: un Auditor podía crear una API Key o un webhook (hallazgo NEX-472 de la auditoría de QA).
#: LEER la biblioteca de tipos documentales sigue abierto a todo el que opera en la organización: el pipeline la necesita para
#: clasificar. Es una propuesta de producto (Administrador y Supervisor), pendiente de confirmar con César; el administrador de
#: plataforma siempre puede (en su espacio).
PUEDEN_ADMINISTRAR = frozenset({ADMIN, "SUPERVISOR"})

#: Los roles que pueden ORGANIZAR documentos en expedientes: crear un expediente y meter o sacar documentos de él. Son los que trabajan
#: la bandeja (como `PUEDEN_DESCARTAR`): compliance, auditor y viewer son de consulta y no mueven el trabajo de otro. LEER expedientes
#: queda abierto a todo el que opera en la organización. Propuesta de producto, pendiente de confirmar con César; el administrador de
#: plataforma siempre puede (en su espacio).
PUEDEN_ORGANIZAR = frozenset({ADMIN, "SUPERVISOR", "ANALISTA", "OPERADOR"})

ASIGNABLES = frozenset(r["codigo"] for r in CATALOGO)
#: Todos los que puede llevar una membresía, incluido el administrador.
VALIDOS = ASIGNABLES | {ADMIN}

_NOMBRES = {r["codigo"]: r["nombre"] for r in CATALOGO} | {ADMIN: "Administrador"}


def nombre_de(codigo: str) -> str:
    """El nombre visible de un rol; el propio código si no se conoce (un rol
    viejo guardado antes de un cambio de catálogo no debe romper el listado)."""
    return _NOMBRES.get(codigo, codigo)
