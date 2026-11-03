"""EXPEDIENTES VIRTUALES: grupitos de documentos de UN expediente, instancias de un tipo de expediente virtual (migración `0004`).

## Qué es (decisión de producto, Moibe, 2026-10-29)

Un expediente es solo un RECEPTÁCULO: la carpeta de un cliente o de un caso, donde cae todo lo suyo (`servicios/expedientes.py`). No tiene tipo.
Los que tienen esquema son los expedientes virtuales: «de todo lo de Juan Pérez, estos tres documentos son su solicitud de crédito». Un
expediente virtual:

- es SUBCONJUNTO de los documentos de un solo expediente (su «expediente»): solo puede tener documentos que están en él, y si un documento sale
  del expediente, sale también de sus virtuales. Eso lo cuida la base (`FK_virtualCaseDocuments_caseDocument`, ON DELETE CASCADE);
- es INSTANCIA de un tipo de expediente virtual de su organización (`servicios/tipos_expediente.py`: nombre + esquema de tipos documentales).
  Un expediente puede tener varios virtuales, del mismo tipo o de tipos distintos;
- tiene un nombre (dos pueden llamarse igual), y sus documentos se eligen A MANO o los mete SOLO el clasificador (ver abajo). El esquema describe,
  no restringe (como siempre): una persona puede meter el documento que quiera.

Para qué sirven: para tomar esos grupitos de documentos y usarlos en el motor de reglas (un proceso compara datos de ciertos tipos documentales;
un expediente virtual junta justo esos).

## Lo que mete el clasificador (Moibe, 2026-11-02)

Un documento que el clasificador reconoce como de un tipo documental entra SOLO a los expedientes virtuales de su expediente cuyo tipo lleva ese
tipo documental en su esquema (`colocar_clasificados`). Basta con ser de ese tipo: no hay confianza mínima (Moibe, 2026-11-02, primero pidió 75% y
horas después lo quitó: «pasa simplemente por ser de ese tipo»). Si el expediente tiene dos virtuales del mismo tipo, entra a los DOS («los dos
clasifican»). Lo que entró así queda marcado (`addedBy = AGREGADO_POR_CLASIFICADOR`) y se puede sacar como cualquier otro: quien lo saca no lo
vuelve a ver entrar, porque solo se revisa lo que cambió (un documento recién clasificado o metido a un expediente, un virtual recién creado),
nunca todo. Se mira la ÚLTIMA clasificación del documento: si la última opinión es «no es ninguno», no se presume una vieja.

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

#: Lo que queda en `virtualCaseDocuments.addedBy` cuando el documento lo metió el clasificador y no una persona (que deja su GUID).
AGREGADO_POR_CLASIFICADOR = "clasificador"

RE_ID = re.compile(r"^expediente-virtual-[0-9a-f]{32}$")
_RE_EXPEDIENTE = re.compile(r"^expediente-[0-9a-f]{32}$")
_RE_TIPO = re.compile(r"^tipo-expediente-[0-9a-f]{32}$")
_RE_DOCUMENTO = re.compile(r"^documento-[0-9a-f]{32}$")


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


def duplicados(documentos: list[dict]) -> list[dict]:
    """Los tipos documentales que tienen DOS O MÁS documentos entre `documentos` (cada uno con su `id`, su `nombreOriginal` y su `tipoDocumental`,
    el de su última extracción o, si no se ha extraído, el de su última clasificación; uno sin tipo no cuenta). Moibe, 2026-11-03: un expediente
    virtual con dos INE no sabe cuál es «el INE», así que un proceso que lo usa NO corre sobre él y los documentos quedan marcados.

    Devuelve `[{"tipoId", "tipoNombre", "documentos": [{"id", "nombre"}]}]` en el orden en que aparece el primer documento de cada tipo, con los
    documentos en el orden en que vienen. No se guarda nada: se calcula al leer, así que sacar uno de los dos quita la marca sola."""
    por_tipo: dict[str, dict] = {}
    for d in documentos:
        tipo = d.get("tipoDocumental") or {}
        if not tipo.get("id"):
            continue
        grupo = por_tipo.setdefault(tipo["id"], {"tipoId": tipo["id"], "tipoNombre": tipo.get("nombre"), "documentos": []})
        grupo["documentos"].append({"id": d["id"], "nombre": d.get("nombreOriginal")})
    return [g for g in por_tipo.values() if len(g["documentos"]) >= 2]


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
            f"SELECT {expedientes_sql.columnas_doc('d')}, vd.addedBy AS agregadoPor FROM nexus.virtualCaseDocuments vd "
            "JOIN nexus.documents d ON d.documentId = vd.documentId WHERE vd.virtualCaseId = ? ORDER BY vd.virtualCaseDocumentId",
            v["virtualCaseId"],
        )
        # `agregadoPorClasificador`: lo metió el clasificador y no una persona. La pantalla lo señala; se saca como cualquier otro.
        docs = [{**expedientes_sql.a_documento(f), "agregadoPorClasificador": f["agregadoPor"] == AGREGADO_POR_CLASIFICADOR} for f in tablas.filas(cur)]
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


# Lo que el clasificador mete SOLO, en una sola sentencia: cada documento a cada virtual de SU expediente cuyo tipo espera el tipo documental con que
# se clasificó (última clasificación, de cualquier confianza) y donde todavía no está. El `NOT EXISTS` con candado serializa dos llamadas
# simultáneas: la segunda ya ve lo que metió la primera y no repite (y la restricción única de la tabla es la red de abajo).
_COLOCAR = (
    "INSERT INTO nexus.virtualCaseDocuments (virtualCaseId, caseId, documentId, addedBy) "
    "OUTPUT INSERTED.virtualCaseId, INSERTED.documentId "
    "SELECT v.virtualCaseId, v.caseId, cd.documentId, ? "
    "FROM nexus.virtualCases v "
    "JOIN nexus.cases c ON c.caseId = v.caseId "
    "JOIN nexus.caseDocuments cd ON cd.caseId = v.caseId "
    "JOIN nexus.documents d ON d.documentId = cd.documentId "
    "CROSS APPLY (SELECT TOP (1) k.documentTypeId, k.confidence FROM nexus.documentClassifications k "
    "WHERE k.documentId = d.documentId ORDER BY k.classificationId DESC) u "
    "JOIN nexus.documentTypes t ON t.documentTypeId = u.documentTypeId "
    "JOIN nexus.virtualCaseTypeDocumentTypes s ON s.virtualCaseTypeId = v.virtualCaseTypeId AND s.documentTypePublicId = t.publicId "
    "WHERE v.tenant = ? AND d.tenant = ? {filtros} "
    "AND NOT EXISTS (SELECT 1 FROM nexus.virtualCaseDocuments x WITH (UPDLOCK, HOLDLOCK) WHERE x.virtualCaseId = v.virtualCaseId AND x.documentId = cd.documentId) "
    "ORDER BY v.virtualCaseId, cd.caseDocumentId"
)


def colocar_clasificados(tenant: str, documento_id: object = None, expediente_id: object = None, virtual_id: object = None) -> list[dict]:
    """Mete SOLOS a los expedientes virtuales los documentos que el clasificador reconoció como de un tipo (sin confianza mínima).

    La regla: un documento entra a un expediente virtual cuando (1) está en el expediente de ese virtual, (2) su ÚLTIMA clasificación dice un tipo
    documental que está en el esquema del tipo del virtual y (3) todavía no está en él. Entra a TODOS los virtuales
    que lo cumplen, también cuando un expediente tiene dos del mismo tipo.

    Solo mira lo que se le dice, nunca todo: `documento_id` (un documento recién clasificado o recién metido a un expediente; con `expediente_id`,
    solo en ese expediente) o `virtual_id` (un virtual recién creado, con lo que ya tiene su expediente). Por eso a quien saca un documento de un
    virtual nadie se lo vuelve a meter. Sin base no hay virtuales: devuelve `[]`; un id que no tiene la forma de lo que dice ser, también.

    Devuelve lo que metió, uno por documento y virtual: `{documentoId, nombreOriginal, tipoDocumental: {id, nombre}, confianza,
    expedienteVirtual: {id, nombre}, expediente: {id, nombre}}`. El llamador anota el rastro (quién lo provocó) y decide qué hacer si esto
    falla: no debe tumbar lo que lo provocó."""
    if not SQLSERVER_HOST:
        return []
    if documento_id is None and virtual_id is None:
        raise ValueError("Hay que decir de qué documento o de qué expediente virtual se trata: no se revisa todo.")
    filtros: list[str] = []
    valores: list[object] = []
    for valor, regex, condicion in (
        (documento_id, _RE_DOCUMENTO, "d.publicId = ?"),
        (expediente_id, _RE_EXPEDIENTE, "c.publicId = ?"),
        (virtual_id, RE_ID, "v.publicId = ?"),
    ):
        if valor is None:
            continue
        if not isinstance(valor, str) or not regex.match(valor):
            return []
        filtros.append(f"AND {condicion}")
        valores.append(valor)
    tenant = almacen._validar_tenant(tenant)
    tablas = _tablas()
    with tablas.transaccion() as cur:
        cur.execute(_COLOCAR.format(filtros=" ".join(filtros)), AGREGADO_POR_CLASIFICADOR, tenant, tenant, *valores)
        metidos = [(f[0], f[1]) for f in cur.fetchall()]
        if not metidos:
            return []
        virtuales_ids = sorted({m[0] for m in metidos})
        documentos_ids = sorted({m[1] for m in metidos})
        cur.execute(
            "SELECT v.virtualCaseId, v.publicId, v.name, c.publicId AS expedienteId, c.name AS expedienteNombre FROM nexus.virtualCases v "
            f"JOIN nexus.cases c ON c.caseId = v.caseId WHERE v.virtualCaseId IN ({', '.join('?' * len(virtuales_ids))})",
            *virtuales_ids,
        )
        virtuales = {f["virtualCaseId"]: f for f in tablas.filas(cur)}
        documentos: dict[int, dict] = {}
        for i in range(0, len(documentos_ids), 500):
            lote = documentos_ids[i : i + 500]
            # OUTER y LEFT: si entre meterlo y leerlo llegó otra clasificación (de otra petición), el documento se reporta igual, solo sin tipo.
            cur.execute(
                "SELECT d.documentId, d.publicId, d.originalName, t.publicId AS tipoId, t.name AS tipoNombre, u.confidence FROM nexus.documents d "
                "OUTER APPLY (SELECT TOP (1) k.documentTypeId, k.confidence FROM nexus.documentClassifications k "
                "WHERE k.documentId = d.documentId ORDER BY k.classificationId DESC) u "
                f"LEFT JOIN nexus.documentTypes t ON t.documentTypeId = u.documentTypeId WHERE d.documentId IN ({', '.join('?' * len(lote))})",
                *lote,
            )
            documentos.update({f["documentId"]: f for f in tablas.filas(cur)})
    return [
        {
            "documentoId": documentos[d]["publicId"],
            "nombreOriginal": documentos[d]["originalName"],
            "tipoDocumental": {"id": documentos[d]["tipoId"], "nombre": documentos[d]["tipoNombre"]} if documentos[d]["tipoId"] else None,
            "confianza": float(documentos[d]["confidence"]) if documentos[d]["confidence"] is not None else None,
            "expedienteVirtual": {"id": virtuales[v]["publicId"], "nombre": virtuales[v]["name"]},
            "expediente": {"id": virtuales[v]["expedienteId"], "nombre": virtuales[v]["expedienteNombre"]},
        }
        for v, d in sorted(metidos)
    ]


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
