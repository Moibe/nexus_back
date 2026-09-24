"""¿Funciona `POST /archivos/` de punta a punta?

    venv/bin/python verificar_subida.py

CORRE OFFLINE Y SIN TOCAR NADA TUYO. Levanta la app contra un `ALMACEN_RUTA`
temporal que él mismo crea y borra, con una llave de API inventada. No necesita
SQL Server, ni el NAS, ni red: `TestClient` llama a la app en proceso.

POR QUÉ LAS VARIABLES SE PONEN ANTES DE LOS IMPORTS. `servicios/almacen.py`
hace `from config import ALMACEN_RUTA`, que se resuelve UNA vez al importar el
módulo. Ponerlas después no tendría efecto y el script probaría el almacén
apagado creyendo que lo probó encendido — el mismo cuidado que ya documenta
`verificar_almacen.py`.

QUÉ SE PRUEBA, Y POR QUÉ ESO. No solo el camino feliz: sobre todo los que
protegen algo. Que la llave haga falta, que el hash que no cuadra se rechace en
vez de guardarse, que un tenant con `../` no escriba fuera de la raíz, y que
subir dos veces lo mismo no duplique. Esas son las promesas que el endpoint
hace; el 200 es la fácil.
"""

import os
import shutil
import tempfile
from pathlib import Path

# ── ANTES de cualquier import del proyecto (ver el docstring) ────────────────
RAIZ = Path(tempfile.mkdtemp(prefix="verificar-subida-"))
LLAVE = "llave-de-prueba-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = LLAVE
os.environ.setdefault("SQLSERVER_HOST", "")

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from servicios import almacen  # noqa: E402

# Dar de alta el almacén es crear su centinela, igual que se hace una vez en el
# server. Sin él, `guardar` se niega a escribir a propósito — es la defensa
# contra un montaje de red caído. Ver `servicios/almacen.py`.
(RAIZ / almacen.CENTINELA).touch()

CABECERAS = {"X-API-Key": LLAVE}
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)


def _objetos():
    """Lo que hay en el almacén, SIN contar el centinela — que no es un objeto
    guardado sino la marca de que la carpeta es el almacén."""
    return [p for p in RAIZ.rglob("*") if p.is_file() and p.name != almacen.CENTINELA]


fallos = 0


def rev(descripcion: str, ok: bool, extra: str = "") -> None:
    global fallos
    if ok:
        print(f"  OK    {descripcion}")
    else:
        fallos += 1
        print(f"  FALLA {descripcion}" + (f"  -> {extra}" if extra else ""))


def titulo(texto: str) -> None:
    print()
    print("=" * 72)
    print(texto)
    print("=" * 72)


def subir(cliente, *, contenido=PNG, mime="image/png", tenant="csi", sha=None, con_llave=True):
    datos = {"tenant": tenant}
    if sha is not None:
        datos["sha256"] = sha
    return cliente.post(
        "/archivos/",
        files={"archivo": ("prueba.png", contenido, mime)},
        data=datos,
        headers=CABECERAS if con_llave else {},
    )


def main() -> int:
    cliente = TestClient(app)

    titulo("1 · La puerta está cerrada sin llave")
    r = subir(cliente, con_llave=False)
    rev("sin X-API-Key responde 401", r.status_code == 401, f"status={r.status_code}")
    rev(
        "y no escribió nada",
        not _objetos(),
        f"archivos={[str(p) for p in _objetos()][:3]}",
    )

    titulo("2 · Lo que no se acepta, se rechaza antes de guardar")
    r = subir(cliente, mime="application/zip")
    rev("un MIME no soportado da 400", r.status_code == 400, f"status={r.status_code}")
    rev("y el mensaje dice cuáles sí", "Formatos aceptados" in r.text, r.text[:120])

    r = subir(cliente, contenido=b"")
    rev("un archivo vacío da 400", r.status_code == 400, f"status={r.status_code}")

    r = subir(cliente, tenant="../fuera")
    rev("un tenant con ../ da 400", r.status_code == 400, f"status={r.status_code}")
    rev(
        "y NADA se escribió fuera de la raíz",
        not (RAIZ.parent / "fuera").exists(),
        str(RAIZ.parent / "fuera"),
    )

    r = subir(cliente, sha="0" * 64)
    rev("un sha256 que no cuadra da 400", r.status_code == 400, f"status={r.status_code}")
    rev("y no dejó el archivo a medias", not _objetos(), "quedó basura en la raíz")

    titulo("3 · La subida buena")
    r = subir(cliente)
    rev("responde 200", r.status_code == 200, f"status={r.status_code} {r.text[:120]}")
    if r.status_code != 200:
        return 1
    cuerpo = r.json()
    print(f"    {cuerpo}")

    esperado = {"rutaRelativa", "sha256", "tamanoBytes", "yaExistia", "mime", "nombreOriginal"}
    rev("devuelve los campos que la base va a necesitar", set(cuerpo) == esperado, str(set(cuerpo)))

    partes = cuerpo["rutaRelativa"].split("/")
    rev(
        "la ruta tiene la forma {tenant}/{aa}/{bb}/{sha256}",
        len(partes) == 4
        and partes[0] == "csi"
        and partes[1] == cuerpo["sha256"][:2]
        and partes[2] == cuerpo["sha256"][2:4]
        and partes[3] == cuerpo["sha256"],
        cuerpo["rutaRelativa"],
    )
    rev("es RELATIVA, no absoluta", not Path(cuerpo["rutaRelativa"]).is_absolute())
    rev("dice que es nueva", cuerpo["yaExistia"] is False)
    rev("el tamaño es el real", cuerpo["tamanoBytes"] == len(PNG))
    rev("conserva el nombre original", cuerpo["nombreOriginal"] == "prueba.png")
    rev("y el MIME normalizado", cuerpo["mime"] == "image/png")

    enDisco = RAIZ / cuerpo["rutaRelativa"]
    rev("el archivo existe en disco", enDisco.is_file(), str(enDisco))
    rev("con los bytes EXACTOS", enDisco.read_bytes() == PNG)
    rev("sin temporales colgando", not list(RAIZ.rglob(".tmp-*")))

    titulo("4 · Subirlo otra vez no duplica")
    antes = len([p for p in RAIZ.rglob("*") if p.is_file()])
    r2 = subir(cliente)
    rev("responde 200", r2.status_code == 200, f"status={r2.status_code}")
    rev("ahora dice yaExistia", r2.json().get("yaExistia") is True, r2.text[:120])
    rev("misma ruta", r2.json().get("rutaRelativa") == cuerpo["rutaRelativa"])
    despues = len([p for p in RAIZ.rglob("*") if p.is_file()])
    rev("y no hay un archivo más en disco", despues == antes, f"{antes} -> {despues}")

    titulo("5 · El hash que SÍ cuadra se acepta")
    r3 = subir(cliente, sha=cuerpo["sha256"])
    rev("responde 200", r3.status_code == 200, f"status={r3.status_code} {r3.text[:120]}")

    titulo("6 · Cada cliente en su propio prefijo")
    r4 = subir(cliente, tenant="cli-acme")
    rev("responde 200", r4.status_code == 200, f"status={r4.status_code}")
    if r4.status_code == 200:
        rev(
            "el MISMO contenido vive dos veces, una por cliente",
            r4.json()["rutaRelativa"].startswith("cli-acme/")
            and r4.json()["yaExistia"] is False,
            r4.json()["rutaRelativa"],
        )
        rev(
            "no se dedupica entre clientes (es la separación de privacidad)",
            (RAIZ / "csi").exists() and (RAIZ / "cli-acme").exists(),
        )

    titulo("7 · Leer de vuelta lo que se guardó")
    r5 = cliente.get(
        f"/archivos/{cuerpo['rutaRelativa']}", params={"mime": "image/png"}, headers=CABECERAS
    )
    rev("responde 200", r5.status_code == 200, f"status={r5.status_code} {r5.text[:120]}")
    rev("devuelve los bytes EXACTOS", r5.content == PNG)
    rev("con el Content-Type pedido", r5.headers.get("content-type", "").startswith("image/png"))
    rev(
        "y se puede cachear para siempre (el nombre ES el hash)",
        "immutable" in r5.headers.get("cache-control", ""),
        r5.headers.get("cache-control", ""),
    )

    r6 = cliente.get(
        f"/archivos/{cuerpo['rutaRelativa']}", params={"mime": "text/html"}, headers=CABECERAS
    )
    rev(
        "un MIME fuera de la lista se rechaza (si no, sería un XSS desde nuestro origen)",
        r6.status_code == 400,
        f"status={r6.status_code}",
    )

    r7 = cliente.get("/archivos/csi/00/00/" + "0" * 64, params={"mime": "image/png"}, headers=CABECERAS)
    rev("un objeto que no existe da 404, no 503", r7.status_code == 404, f"status={r7.status_code}")

    r8 = cliente.get(f"/archivos/{cuerpo['rutaRelativa']}", params={"mime": "image/png"})
    rev("y sin llave tampoco se lee", r8.status_code == 401, f"status={r8.status_code}")

    print()
    print("=" * 72)
    print(f"{fallos} FALLARON" if fallos else "todas las comprobaciones pasaron")
    return 1 if fallos else 0


if __name__ == "__main__":
    try:
        codigo = main()
    finally:
        shutil.rmtree(RAIZ, ignore_errors=True)
    raise SystemExit(codigo)
