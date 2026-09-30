"""¿Funciona la bandeja de entrada (`/bandeja/`) de punta a punta?

    venv/bin/python verificar_bandeja.py

OFFLINE y sin tocar nada tuyo, igual que `verificar_subida.py`: levanta la app
contra un `ALMACEN_RUTA` temporal que crea y borra, con una llave inventada.
Las variables van ANTES de los imports por la misma razón que allá.

Lo que importa probar no es el 201: es que lo que entra aparezca, que lo que
se retira no vuelva, que un cliente no vea lo de otro, y que dos subidas al
mismo tiempo no se pisen en el registro.
"""

import os
import shutil
import tempfile
import threading
from pathlib import Path

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-bandeja-"))
LLAVE = "llave-de-prueba-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = LLAVE
os.environ.setdefault("SQLSERVER_HOST", "")

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from servicios import almacen  # noqa: E402

(RAIZ / almacen.CENTINELA).touch()

CABECERAS = {"X-API-Key": LLAVE}
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)
PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"

fallos = 0


def rev(descripcion: str, ok: bool, extra: str = "") -> None:
    global fallos
    if ok:
        print(f"  OK    {descripcion}")
    else:
        fallos += 1
        print(f"  FALLA {descripcion}" + (f"  -> {extra}" if extra else ""))


def titulo(texto: str) -> None:
    print("\n" + "=" * 72 + "\n" + texto + "\n" + "=" * 72)


def subir(cliente, *, contenido=PNG, nombre="ine.png", mime="image/png", tenant="demo", con_llave=True):
    return cliente.post(
        "/bandeja/",
        files={"archivo": (nombre, contenido, mime)},
        data={"tenant": tenant},
        headers=CABECERAS if con_llave else {},
    )


def pendientes(cliente, tenant="demo"):
    r = cliente.get("/bandeja/", params={"tenant": tenant}, headers=CABECERAS)
    return r.status_code, (r.json().get("entradas", []) if r.status_code == 200 else r.text)


def retirar(cliente, id_entrada, motivo="pipeline", tenant="demo"):
    return cliente.post(
        f"/bandeja/{id_entrada}/retirar", data={"tenant": tenant, "motivo": motivo}, headers=CABECERAS
    )


def main() -> int:
    c = TestClient(app)

    titulo("1 · Sin llave no se entra")
    rev("subir sin llave da 401", subir(c, con_llave=False).status_code == 401)
    rev("listar sin llave da 401", c.get("/bandeja/", params={"tenant": "demo"}).status_code == 401)

    titulo("2 · Se rechaza lo mismo que en /archivos/")
    rev("un MIME no soportado da 400", subir(c, mime="application/zip").status_code == 400)
    r_webp = subir(c, mime="image/webp", nombre="foto.webp")
    rev("un WebP (el almacén sí lo acepta, el pipeline no) da 400", r_webp.status_code == 400, r_webp.text[:120])
    rev("  y el mensaje dice qué sí se acepta", "PDF, JPEG, PNG y TIFF" in r_webp.text)
    rev("un archivo vacío da 400", subir(c, contenido=b"").status_code == 400)
    rev("un tenant con ../ da 400", subir(c, tenant="../fuera").status_code == 400)
    estado, lista = pendientes(c)
    rev("y nada de eso quedó en la bandeja", estado == 200 and lista == [], str(lista)[:120])

    titulo("3 · Lo que entra, aparece")
    r = subir(c)
    rev("responde 201", r.status_code == 201, f"{r.status_code} {r.text[:160]}")
    e = r.json()
    print(f"    {e}")
    campos = {"id", "tenant", "rutaRelativa", "sha256", "tamanoBytes", "mime", "nombreOriginal", "canal", "llaveId", "recibidoEn"}
    rev("trae lo que pedirá uspCreateFile (para el backfill)", set(e) == campos, str(set(e) ^ campos))
    rev("canal API", e.get("canal") == "API")
    rev("el archivo SÍ está en el almacén", (RAIZ / e["rutaRelativa"]).is_file())
    estado, lista = pendientes(c)
    rev("aparece en la bandeja", estado == 200 and [x["id"] for x in lista] == [e["id"]], str(lista)[:160])

    r2 = subir(c, contenido=PDF, nombre="comprobante.pdf", mime="application/pdf")
    _, lista = pendientes(c)
    rev("un segundo archivo aparece DESPUÉS del primero", [x["id"] for x in lista] == [e["id"], r2.json()["id"]])

    titulo("4 · Subir dos veces lo mismo: dos entradas, un solo objeto")
    r3 = subir(c)
    _, lista = pendientes(c)
    rev("son dos entradas (la bandeja lo marcará como duplicado)", len([x for x in lista if x["sha256"] == e["sha256"]]) == 2)
    rev("con la MISMA ruta: el almacén no duplicó disco", r3.json()["rutaRelativa"] == e["rutaRelativa"])

    titulo("5 · Lo que se retira no vuelve")
    rev("retirar responde 200", retirar(c, e["id"]).status_code == 200)
    _, lista = pendientes(c)
    rev("ya no está en la bandeja", e["id"] not in [x["id"] for x in lista])
    rev("retirarlo otra vez da 404", retirar(c, e["id"]).status_code == 404)
    rev("retirar algo que nunca entró da 404", retirar(c, "no-existe").status_code == 404)
    rev("un motivo inventado da 400", retirar(c, r2.json()["id"], motivo="porque sí").status_code == 400)
    rev("el archivo sigue en el almacén (retirar no borra)", (RAIZ / e["rutaRelativa"]).is_file())

    titulo("6 · Un cliente no ve lo de otro")
    ra = subir(c, tenant="cli-acme")
    _, lista_demo = pendientes(c, "demo")
    _, lista_acme = pendientes(c, "cli-acme")
    rev("acme ve solo lo suyo", [x["id"] for x in lista_acme] == [ra.json()["id"]])
    rev("demo no ve lo de acme", ra.json()["id"] not in [x["id"] for x in lista_demo])
    rev("retirar lo de acme desde demo da 404", retirar(c, ra.json()["id"], tenant="demo").status_code == 404)

    titulo("7 · Veinte subidas al mismo tiempo no se pisan")
    ids = []
    errores = []

    def una(i):
        rr = subir(c, contenido=PNG + bytes([i]), nombre=f"lote-{i}.png")
        (ids if rr.status_code == 201 else errores).append(rr.json().get("id") if rr.status_code == 201 else rr.status_code)

    hilos = [threading.Thread(target=una, args=(i,)) for i in range(20)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    _, lista = pendientes(c)
    rev("las 20 respondieron 201", len(ids) == 20, str(errores))
    rev("y las 20 están en la bandeja", set(ids) <= {x["id"] for x in lista}, f"{len(set(ids) & {x['id'] for x in lista})} de 20")

    titulo("8 · Una línea rota en el registro no tumba la bandeja")
    registro = RAIZ / ".registro" / "bandeja-demo.jsonl"
    with open(registro, "a", encoding="utf-8") as f:
        f.write('{"evento": "entrada", "id": "cort')  # un corte a media escritura
    estado, lista = pendientes(c)
    rev("sigue listando", estado == 200 and len(lista) > 0, str(estado))
    rn = subir(c, contenido=PNG + b"despues", nombre="despues.png")
    _, lista = pendientes(c)
    rev("y lo que entra después también aparece", rn.json()["id"] in [x["id"] for x in lista])

    titulo("9 · Con el montaje caído, 503 — ni se lista ni se escribe")
    (RAIZ / almacen.CENTINELA).unlink()
    try:
        rev("listar da 503", pendientes(c)[0] == 503)
        rev("subir da 503", subir(c, contenido=PNG + b"caido").status_code == 503)
    finally:
        (RAIZ / almacen.CENTINELA).touch()

    print("\n" + "=" * 72)
    print(f"{fallos} FALLARON" if fallos else "todas las comprobaciones pasaron")
    return 1 if fallos else 0


if __name__ == "__main__":
    try:
        codigo = main()
    finally:
        shutil.rmtree(RAIZ, ignore_errors=True)
    raise SystemExit(codigo)
