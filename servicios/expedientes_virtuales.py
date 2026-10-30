"""EXPEDIENTES VIRTUALES: grupitos de documentos de UN expediente, instancias de un tipo de expediente virtual (migración `0004`).

## Qué es (decisión de producto, Moibe, 2026-10-29)

Un expediente es solo un RECEPTÁCULO: la carpeta de un cliente o de un caso, donde cae todo lo suyo (`servicios/expedientes.py`). No tiene tipo.
Los que tienen esquema son los expedientes virtuales: «de todo lo de Juan Pérez, estos tres documentos son su solicitud de crédito». Un
expediente virtual:

- es SUBCONJUNTO de los documentos de un solo expediente (su «expediente»): solo puede tener documentos que están en él, y si un documento sale
  del expediente, sale también de sus virtuales. Eso lo cuida la base (`FK_virtualCaseDocuments_caseDocument`, ON DELETE CASCADE);
- es INSTANCIA de un tipo de expediente virtual de su organización (`servicios/tipos_expediente.py`: nombre + esquema de tipos documentales).
  Un expediente puede tener varios virtuales, del mismo tipo o de tipos distintos;
- tiene un nombre (dos pueden llamarse igual), y sus documentos se eligen A MANO: los documentos todavía no guardan su tipo documental, así que
  no se pueden repartir solos según el esquema. El esquema describe, no restringe (como siempre).

Para qué sirven: para tomar esos grupitos de documentos y usarlos en el motor de reglas (un proceso compara datos de ciertos tipos documentales;
un expediente virtual junta justo esos).

## Solo en SQL

Por la decisión del 2026-10-28 («todo lo nuevo directo a SQL»), esta entidad NO tiene gemelo en JSON: donde no hay base (la laptop, las pruebas
offline sin `--sql`) cada operación levanta `SinBase` y el router contesta 503 diciéndolo. Leer los virtuales de un expediente sin base da una
lista vacía, para que el expediente se siga viendo.

## Errores

`NoEncontrado` (el expediente o el virtual no son de esa organización, o no existen: no se distingue), `TipoNoEncontrado`,
`DocumentoNoEnExpediente` (meter un documento que no está en el expediente), `DocumentoNoEncontrado` (sacar uno que no está en el virtual),
`VirtualInvalido` (nombre que no sirve).
"""

import re
import uuid

from config import SQLSERVER_HOST
from servicios import almacen

MAX_NOMBRE = 200

RE_ID = re.compile(r"^expediente-virtual-[0-9a-f]{32}$")
_RE_EXPEDIENTE = re.compile(r"^expediente-[0-9a-f]{32}$")
_RE_TIPO = re.compile(r"^tipo-expediente-[0-9a-f]{32}$")


class SinBase(RuntimeError):
    """Los expedientes virtuales solo viven en SQL Server, y esta instancia no tiene base."""


class VirtualInvalido(ValueError):
    """Lo que llegó no se puede guardar (nombre vacío o demasiado largo)."""


class NoEncontrado(LookupError):
    """No hay tal expediente (o tal expediente virtual) EN ESA organización."""


class TipoNoEncontrado(LookupError):
    """No hay tal tipo de expediente virtual EN ESA organización."""


class DocumentoNoEnExpediente(LookupError):
    """El documento no está en el expediente del que el virtual es subconjunto (o no existe)."""


class DocumentoNoEncontrado(LookupError):
    """El documento no está en ese expediente virtual."""


def _tablas():
    if not SQLSERVER_HOST:
        raise SinBase("Los expedientes virtuales necesitan la base de datos, y esta instancia no tiene.")
    from db import tablas

    return tablas


def limpiar_nombre(nombre: object) -> str:
    if not isinstance(nombre, str):
        raise VirtualInvalido("El nombre del expediente virtual no es válido.")
    limpio = " ".join(nombre.split())
    if not limpio or len(limpio) > MAX_NOMBRE or not limpio.isprintable():
        raise VirtualInvalido("El nombre del expediente virtual no es válido.")
    return limpio


def _ids_validos(expediente_id: object, virtual_id: object = None) -> None:
    if not isinstance(expediente_id, str) or not _RE_EXPEDIENTE.match(expediente_id):
        raise NoEncontrado(expediente_id)
    if virtual_id is not None and (not isinstance(virtual_id, str) or not RE_ID.match(virtual_id)):
        raise NoEncontrado(virtual_id)


# Cada virtual con su tipo y cuántos documentos tiene. `{filtro}` lo acota a un expediente o a un virtual.
_VIRTUALES = (
    "SELECT v.virtualCaseId, v.publicId, v.name, v.createdBy, v.createdAt, t.virtualCaseTypeId, t.publicId AS tipoId, t.name AS tipoNombre "
    "FROM nexus.virtualCases v JOIN nexus.virtualCaseTypes t ON t.virtualCaseTypeId = v.virtualCaseTypeId WHERE {filtro} ORDER BY v.virtualCaseId"
)


def resumenes_de(cur, caso_id: int) -> list[dict]:
    """Los virtuales de un expediente (por su llave interna), en el orden en que se crearon, cada uno con su tipo y los ids de sus documentos.
    Lo usa `expedientes_sql.expedientes_obtener`, dentro de su misma lectura."""
    from db import tablas

    from servicios.tipos_documentales_comun import momento

    cur.execute(_VIRTUALES.format(filtro="v.caseId = ?"), caso_id)
    virtuales = tablas.filas(cur)
    cur.execute(
        "SELECT vd.virtualCaseId, d.publicId FROM nexus.virtualCaseDocuments vd JOIN nexus.documents d ON d.documentId = vd.documentId "
        "WHERE vd.caseId = ? ORDER BY vd.virtualCaseDocumentId",
        caso_id,
    )
    docs: dict[int, list[str]] = {}
    for f in tablas.filas(cur):
        docs.setdefault(f["virtualCaseId"], []).append(f["publicId"])
    return [
        {
            "id": v["publicId"],
            "nombre": v["name"],
            "tipo": {"id": v["tipoId"], "nombre": v["tipoNombre"]},
            "creadoPor": v["createdBy"],
            "creadoEn": momento(v["createdAt"]),
            "documentos": docs.get(v["virtualCaseId"], []),
            "totalDocumentos": len(docs.get(v["virtualCaseId"], [])),
        }
        for v in virtuales
    ]


def crear(tenant: str, expediente_id: object, tipo_id: object, nombre: object, documentos_ids: object, por: str) -> dict:
    """Crea un expediente virtual de ese tipo dentro de ese expediente, con los documentos elegidos (pueden ser ninguno), y lo devuelve.

    Todo en una transacción: si un documento no está en el expediente (`DocumentoNoEnExpediente`), no se crea nada. El tipo queda bloqueado
    mientras tanto, igual que al borrarlo: un tipo no puede desaparecer a media creación."""
    from servicios import expedientes_sql

    limpio = limpiar_nombre(nombre)
    if not isinstance(documentos_ids, list) or any(not isinstance(i, str) for i in documentos_ids):
        raise VirtualInvalido("Los documentos deben ser una lista de ids.")
    pedidos = list(dict.fromkeys(documentos_ids))
    _ids_validos(expediente_id)
    tenant = almacen._validar_tenant(tenant)
    tablas = _tablas()
    virtual_id = f"expediente-virtual-{uuid.uuid4().hex}"
    with tablas.transaccion() as cur:
        caso = expedientes_sql.expediente_bloqueado(cur, tenant, expediente_id)
        if caso is None:
            raise NoEncontrado(expediente_id)
        tipo = expedientes_sql.tipo_bloqueado(cur, tenant, tipo_id) if isinstance(tipo_id, str) and _RE_TIPO.match(tipo_id) else None
        if tipo is None:
            raise TipoNoEncontrado(tipo_id)
        cur.execute(
            "INSERT INTO nexus.virtualCases (publicId, tenant, caseId, virtualCaseTypeId, name, createdBy) OUTPUT INSERTED.virtualCaseId VALUES (?, ?, ?, ?, ?, ?)",
            virtual_id, tenant, caso["caseId"], tipo["virtualCaseTypeId"], limpio, por,
        )
        nuevo = tablas.una(cur)["virtualCaseId"]
        for documento_id in pedidos:
            _meter(cur, caso["caseId"], nuevo, documento_id, por)
    return obtener(tenant, expediente_id, virtual_id)


def _meter(cur, caso_id: int, virtual: int, documento_id: str, por: str) -> bool:
    """Mete un documento del expediente al virtual. `False` si ya estaba; `DocumentoNoEnExpediente` si no es del expediente."""
    from db import tablas

    cur.execute(
        "SELECT cd.documentId, (SELECT count(*) FROM nexus.virtualCaseDocuments vd WHERE vd.virtualCaseId = ? AND vd.documentId = cd.documentId) AS ya "
        "FROM nexus.caseDocuments cd JOIN nexus.documents d ON d.documentId = cd.documentId WHERE cd.caseId = ? AND d.publicId = ?",
        virtual, caso_id, documento_id,
    )
    f = tablas.una(cur)
    if f is None:
        raise DocumentoNoEnExpediente(documento_id)
    if f["ya"]:
        return False
    cur.execute(
        "INSERT INTO nexus.virtualCaseDocuments (virtualCaseId, caseId, documentId, addedBy) VALUES (?, ?, ?, ?)", virtual, caso_id, f["documentId"], por
    )
    return True


def _virtual_bloqueado(cur, tenant: str, expediente_id: str, virtual_id: str) -> dict | None:
    from db import tablas

    cur.execute(
        "SELECT v.virtualCaseId, v.caseId, v.name FROM nexus.virtualCases v WITH (UPDLOCK, HOLDLOCK) JOIN nexus.cases c ON c.caseId = v.caseId "
        "WHERE v.publicId = ? AND v.tenant = ? AND c.publicId = ?",
        virtual_id, tenant, expediente_id,
    )
    return tablas.una(cur)


def obtener(tenant: str, expediente_id: object, virtual_id: object) -> dict | None:
    """El virtual con su tipo (y el esquema del tipo, con nombres), su expediente y sus documentos completos, en el orden en que se agregaron.
    `None` si no es de esa organización o no es de ese expediente."""
    from servicios import expedientes_sql
    from servicios.tipos_documentales_comun import momento

    try:
        _ids_validos(expediente_id, virtual_id)
    except NoEncontrado:
        return None
    tenant = almacen._validar_tenant(tenant)
    tablas = _tablas()
    with tablas.lectura() as cur:
        cur.execute(
            _VIRTUALES.format(filtro="v.publicId = ? AND v.tenant = ? AND v.caseId = (SELECT caseId FROM nexus.cases WHERE publicId = ? AND tenant = ?)"),
            virtual_id, tenant, expediente_id, tenant,
        )
        v = tablas.una(cur)
        if v is None:
            return None
        cur.execute("SELECT publicId, name FROM nexus.cases WHERE publicId = ? AND tenant = ?", expediente_id, tenant)
        caso = tablas.una(cur)
        cur.execute(
            "SELECT s.documentTypePublicId AS id, dt.name AS nombre FROM nexus.virtualCaseTypeDocumentTypes s "
            "JOIN nexus.documentTypes dt ON dt.publicId = s.documentTypePublicId WHERE s.virtualCaseTypeId = ? ORDER BY s.position",
            v["virtualCaseTypeId"],
        )
        esquema = [{"id": f["id"], "nombre": f["nombre"]} for f in tablas.filas(cur)]
        cur.execute(
            f"SELECT {expedientes_sql.columnas_doc('d')} FROM nexus.virtualCaseDocuments vd JOIN nexus.documents d ON d.documentId = vd.documentId "
            "WHERE vd.virtualCaseId = ? ORDER BY vd.virtualCaseDocumentId",
            v["virtualCaseId"],
        )
        docs = [expedientes_sql.a_documento(f) for f in tablas.filas(cur)]
    from servicios import procesos

    return {
        "id": v["publicId"],
        "nombre": v["name"],
        "tipo": {"id": v["tipoId"], "nombre": v["tipoNombre"], "tiposDocumentales": esquema},
        "expediente": {"id": caso["publicId"], "nombre": caso["name"]},
        "creadoPor": v["createdBy"],
        "creadoEn": momento(v["createdAt"]),
        "totalDocumentos": len(docs),
        "documentos": docs,
        # Los procesos del motor de reglas ligados a su tipo: los que le aplican. Se leen aunque quien mira no pueda editar procesos (solo
        # el nombre y si está activo); todavía no hay motor que los corra sobre el expediente virtual.
        "procesos": procesos.de_tipo(tenant, v["tipoId"]),
    }


def agregar_documento(tenant: str, expediente_id: object, virtual_id: object, documento_id: object, por: str) -> bool:
    """Mete un documento DEL EXPEDIENTE al virtual. `True` si lo agregó, `False` si ya estaba (no hace nada)."""
    _ids_validos(expediente_id, virtual_id)
    tenant = almacen._validar_tenant(tenant)
    if not isinstance(documento_id, str):
        raise DocumentoNoEnExpediente(documento_id)
    tablas = _tablas()
    with tablas.transaccion() as cur:
        v = _virtual_bloqueado(cur, tenant, expediente_id, virtual_id)
        if v is None:
            raise NoEncontrado(virtual_id)
        return _meter(cur, v["caseId"], v["virtualCaseId"], documento_id, por)


def quitar_documento(tenant: str, expediente_id: object, virtual_id: object, documento_id: object) -> None:
    """Saca un documento del virtual. El documento sigue en el expediente. `DocumentoNoEncontrado` si no estaba en el virtual."""
    _ids_validos(expediente_id, virtual_id)
    tenant = almacen._validar_tenant(tenant)
    tablas = _tablas()
    with tablas.transaccion() as cur:
        v = _virtual_bloqueado(cur, tenant, expediente_id, virtual_id)
        if v is None:
            raise NoEncontrado(virtual_id)
        cur.execute(
            "DELETE vd FROM nexus.virtualCaseDocuments vd JOIN nexus.documents d ON d.documentId = vd.documentId WHERE vd.virtualCaseId = ? AND d.publicId = ?",
            v["virtualCaseId"], documento_id,
        )
        if cur.rowcount == 0:
            raise DocumentoNoEncontrado(documento_id)

def borrar(tenant: str, expediente_id: object, virtual_id: object) -> dict:
    """Borra el expediente virtual (NO sus documentos: siguen en el expediente). Devuelve `{id, nombre}` de lo que se borró."""
    _ids_validos(expediente_id, virtual_id)
    tenant = almacen._validar_tenant(tenant)
    tablas = _tablas()
    with tablas.transaccion() as cur:
        v = _virtual_bloqueado(cur, tenant, expediente_id, virtual_id)
        if v is None:
            raise NoEncontrado(virtual_id)
        # Sus renglones se van con él (ON DELETE CASCADE).
        cur.execute("DELETE FROM nexus.virtualCases WHERE virtualCaseId = ?", v["virtualCaseId"])
    return {"id": virtual_id, "nombre": v["name"]}


def contar_por_tipo(tenant: str) -> dict[str, int]:
    """Cuántos expedientes virtuales tiene cada tipo de esa organización: `{tipo_id: n}`. Sin base, ninguno."""
    if not SQLSERVER_HOST:
        return {}
    tenant = almacen._validar_tenant(tenant)
    from db import tablas

    with tablas.lectura() as cur:
        cur.execute(
            "SELECT t.publicId, count(*) AS n FROM nexus.virtualCases v JOIN nexus.virtualCaseTypes t ON t.virtualCaseTypeId = v.virtualCaseTypeId "
            "WHERE v.tenant = ? GROUP BY t.publicId",
            tenant,
        )
        return {f["publicId"]: int(f["n"]) for f in tablas.filas(cur)}


def listar_de_tipo(tenant: str, tipo_id: object) -> list[dict]:
    """Los expedientes virtuales de UN tipo, de toda la organización (cada uno vive en algún expediente), los más recientes primero.

    Cada uno trae su expediente (nombre y cuántos documentos tiene) y cuántos de esos documentos lo forman: con eso la pantalla dice «2 de 3
    documentos» sin pedir cada expediente. Es lo que hay detrás de la cuenta `totalExpedientesVirtuales` de un tipo. Sin base, ninguno (como
    `contar_por_tipo`); un id que no tiene la forma de un tipo, tampoco."""
    if not SQLSERVER_HOST or not isinstance(tipo_id, str) or not _RE_TIPO.match(tipo_id):
        return []
    tenant = almacen._validar_tenant(tenant)
    from db import tablas

    from servicios.tipos_documentales_comun import momento

    with tablas.lectura() as cur:
        cur.execute(
            "SELECT v.publicId, v.name, v.createdBy, v.createdAt, c.publicId AS expedienteId, c.name AS expedienteNombre, "
            "(SELECT count(*) FROM nexus.virtualCaseDocuments vd WHERE vd.virtualCaseId = v.virtualCaseId) AS enVirtual, "
            "(SELECT count(*) FROM nexus.caseDocuments cd WHERE cd.caseId = c.caseId) AS enExpediente "
            "FROM nexus.virtualCases v JOIN nexus.cases c ON c.caseId = v.caseId "
            "JOIN nexus.virtualCaseTypes t ON t.virtualCaseTypeId = v.virtualCaseTypeId "
            "WHERE t.publicId = ? AND v.tenant = ? ORDER BY v.virtualCaseId DESC",
            tipo_id,
            tenant,
        )
        return [
            {
                "id": f["publicId"],
                "nombre": f["name"],
                "expediente": {"id": f["expedienteId"], "nombre": f["expedienteNombre"], "totalDocumentos": int(f["enExpediente"])},
                "totalDocumentos": int(f["enVirtual"]),
                "creadoPor": f["createdBy"],
                "creadoEn": momento(f["createdAt"]),
            }
            for f in tablas.filas(cur)
        ]
