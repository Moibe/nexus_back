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

ASIGNABLES = frozenset(r["codigo"] for r in CATALOGO)
#: Todos los que puede llevar una membresía, incluido el administrador.
VALIDOS = ASIGNABLES | {ADMIN}

_NOMBRES = {r["codigo"]: r["nombre"] for r in CATALOGO} | {ADMIN: "Administrador"}


def nombre_de(codigo: str) -> str:
    """El nombre visible de un rol; el propio código si no se conoce (un rol
    viejo guardado antes de un cambio de catálogo no debe romper el listado)."""
    return _NOMBRES.get(codigo, codigo)
