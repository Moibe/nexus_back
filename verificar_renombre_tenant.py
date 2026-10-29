"""¿`renombrar_tenant.py` cambia `demo` → `plataforma` CONSERVANDO todo lo que el espacio de plataforma tenía, sin tocar lo de nadie más?

    .venv/Scripts/python verificar_renombre_tenant.py

OFFLINE: arma un almacén temporal con datos REALES de cada servicio en `demo` (un documento en la bandeja, tipos documentales con un ejemplo
subido, un tipo de expediente y un expediente, procesos, llaves, webhooks) y los mismos datos en otro tenant que debe quedar IDÉNTICO. Después
de renombrar comprueba, leyendo con los propios servicios, que todo aparece bajo `plataforma`, que el archivo se lee por su ruta nueva, que el
otro tenant no cambió ni un byte, que la simulación no toca nada, que se niega cuando debe y que se deshace corriéndolo al revés.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-renombre-"))
(RAIZ / ".nexus-almacen").touch()
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"
os.environ.setdefault("SQLSERVER_HOST", "")

import renombrar_tenant as renombre  # noqa: E402
from servicios import (  # noqa: E402
    almacen,
    auth,
    bandeja,
    documentos,
    expedientes,
    procesos,
    registro,
    tenants_registro,
    tipos_documentales,
    tipos_expediente,
    usuarios,
)

fallos = 0


def rev(descripcion: str, ok: bool, extra: str = "") -> None:
    global fallos
    print(f"  {'OK   ' if ok else 'FALLA'} {descripcion}{'  -> ' + extra if extra and not ok else ''}")
    if not ok:
        fallos += 1


def titulo(t: str) -> None:
    print(f"\n=== {t} ===")


def correr(*argumentos: str) -> tuple[int, str]:
    salida = io.StringIO()
    anterior = sys.argv
    sys.argv = ["renombrar_tenant.py", *argumentos]
    try:
        with contextlib.redirect_stdout(salida), contextlib.redirect_stderr(salida):
            codigo = renombre.main()
    finally:
        sys.argv = anterior
    return codigo, salida.getvalue()


def instantanea(sin: tuple[str, ...] = ()) -> dict:
    return {str(p.relative_to(RAIZ)): p.read_bytes() for p in RAIZ.rglob("*") if p.is_file() and ".respaldo-renombre" not in str(p) and not any(s in str(p) for s in sin)}


def datos_tipo(nombre: str, ruta_ejemplo: str, tenant: str) -> dict:
    return {
        "nombre": nombre,
        "descripcion": f"Descripción de {nombre}",
        "estado": "activo",
        "campos": [],
        "version": 1,
        "procesadorId": f"proc-{nombre}-{tenant}",
        "documentosEjemplo": [{"id": "ej-1", "rutaRelativa": ruta_ejemplo, "rutaVista": ruta_ejemplo, "mime": "image/png", "nota": "ver demo/leeme.txt y mi-demo/ab/cd/" + "e" * 64}],
    }


def sembrar(tenant: str, contenido: bytes) -> dict:
    """Todo lo que un espacio puede tener, con los servicios de verdad. Devuelve lo necesario para comprobarlo después."""
    guardado = almacen.guardar(tenant, contenido)
    entrada = bandeja.registrar_entrada(tenant, guardado, "image/png", "ine.png", "MANUAL")
    td_id = "tipo-" + (tenant.replace("-", "") + "0" * 32)[:32]
    tipos_documentales.guardar(tenant, td_id, datos_tipo("INE", guardado["rutaRelativa"], tenant), 0, "guid-de-prueba")
    te = tipos_expediente.crear(tenant, "Expediente médico", [td_id], "guid-de-prueba")
    ex = expedientes.crear(tenant, "Juan Pérez", "guid-de-prueba", te["id"])
    expedientes.agregar_documento(tenant, ex["id"], entrada["documentoId"], "guid-de-prueba")
    registro.agregar("procesos.jsonl", {"op": "guardar", "id": f"proceso-{tenant}", "tenant": tenant, "revision": 1, "datos": {"nombre": "P"}, "por": "x", "en": "2026-10-20T00:00:00+00:00"})
    registro.agregar("llaves.jsonl", {"evento": "emitida", "id": f"llave-{tenant}", "tenant": tenant, "hash": "h" * 8, "creadaEn": "2026-10-20T00:00:00+00:00"})
    registro.agregar("webhooks.jsonl", {"evento": "creado", "id": f"wh-{tenant}", "tenant": tenant, "url": "https://x", "en": "2026-10-20T00:00:00+00:00"})
    registro.agregar("auditoria.jsonl", {"evento": "auditoria", "accion": "CROSS_TENANT_ACCESS", "usuarioGuid": None, "tenantPedido": tenant, "recurso": "archivo", "en": "2026-10-20T00:00:00+00:00"})
    return {"guardado": guardado, "entrada": entrada, "td": td_id, "te": te["id"], "ex": ex["id"], "contenido": contenido}


try:
    titulo("0 · Montaje: `demo` (espacio de plataforma) y `cli-acme` (otro tenant que no debe cambiar)")
    # Una organización real, para comprobar que no se puede renombrar hacia ni desde ella.
    temporal = auth.contrasena_temporal()
    admin = usuarios.crear_usuario("rita@ejemplo.com", "Rita", "Admin", auth.hash_de(temporal))
    org = tenants_registro.crear("Alfa", admin["guid"])
    demo = sembrar("demo", b"\x89PNG-demo-" + b"d" * 40)
    otro = sembrar("cli-acme", b"\x89PNG-acme-" + b"a" * 40)
    # Dos líneas de OTRO tenant escritas «a mano»: una con espacios y escapes distintos a los de `json.dumps` y una rota. Tienen que sobrevivir
    # byte por byte: solo se reescribe lo que de verdad cambia.
    LINEA_RARA = '{"op":"guardar",  "id":"proceso-raro","tenant":"cli-acme","revision":1,"datos":{"nombre":"A\\u00f1o"},"por":"x","en":"2026-10-20T00:00:00+00:00"}\n'.encode()
    LINEA_ROTA = b'{"op": "guardar", "id": "proceso-roto", "tenant": "demo", "da\n'
    with open(RAIZ / ".registro" / "procesos.jsonl", "ab") as f:
        f.write(LINEA_RARA + LINEA_ROTA)
    rev("el archivo de demo vive bajo `demo/`", demo["guardado"]["rutaRelativa"].startswith("demo/") and (RAIZ / demo["guardado"]["rutaRelativa"]).is_file())
    rev("y el ejemplo del tipo documental apunta a esa ruta", tipos_documentales.listar("demo")[0]["datos"]["documentosEjemplo"][0]["rutaRelativa"] == demo["guardado"]["rutaRelativa"])

    titulo("1 · La simulación no toca nada")
    antes = instantanea()
    codigo, texto = correr()
    rev("sale con 0 y dice que es simulación", codigo == 0 and "SIMULACIÓN" in texto, texto)
    rev("cuenta lo que se movería (los tres archivos de registro propios y la carpeta)", "bandeja-demo.jsonl → bandeja-plataforma.jsonl" in texto and "demo/ → plataforma/" in texto and "tipos-documentales.jsonl" in texto, texto)
    rev("no cambió ni un byte del almacén", instantanea() == antes)

    titulo("2 · Se niega cuando debe")
    codigo, texto = correr("--de", "demo", "--a", "demo")
    rev("origen y destino iguales", codigo == 2 and "el mismo" in texto, texto)
    codigo, texto = correr("--de", org["guid"], "--a", "plataforma")
    rev("renombrar DESDE una organización", codigo == 2 and "guid de una organización" in texto, texto)
    codigo, texto = correr("--de", "demo", "--a", org["guid"])
    rev("renombrar HACIA el guid de una organización", codigo == 2 and "guid de una organización" in texto, texto)
    codigo, texto = correr("--de", "demo", "--a", "cli-acme")
    rev("hacia un tenant que ya tiene datos: no mezcla dos espacios", codigo == 2 and "ya tiene datos" in texto, texto)
    codigo, texto = correr("--de", "demo", "--a", "../x")
    rev("con un nombre que no es válido", codigo == 2, texto)
    rev("y tras todo eso el almacén sigue igual", instantanea() == antes)

    titulo("3 · Con --confirmar, todo pasa a `plataforma`")
    otro_antes = instantanea()
    otro_bytes = {k: v for k, v in otro_antes.items() if "cli-acme" in k and k.startswith("cli-acme")}
    codigo, texto = correr("--confirmar")
    rev("sale con 0", codigo == 0, texto)
    rev("dejó un respaldo de .registro/", len(list(RAIZ.glob(".respaldo-renombre-*"))) == 1)
    rev("la carpeta de documentos cambió de nombre y la vieja ya no está", (RAIZ / "plataforma").is_dir() and not (RAIZ / "demo").exists())
    ruta_nueva = demo["guardado"]["rutaRelativa"].replace("demo/", "plataforma/", 1)
    rev("el archivo se LEE por su ruta nueva, con los mismos bytes", almacen.leer(ruta_nueva) == demo["contenido"])
    rev("los registros propios ya se llaman `plataforma`", all((RAIZ / ".registro" / f"{p}-plataforma.jsonl").exists() and not (RAIZ / ".registro" / f"{p}-demo.jsonl").exists() for p in ("bandeja", "documentos", "expedientes", "tipos-expediente")))
    pend = bandeja.listar_pendientes("plataforma")
    rev("la bandeja de `plataforma` trae el documento, con su ruta nueva", len(pend) == 1 and pend[0]["rutaRelativa"] == ruta_nueva and pend[0]["nombreOriginal"] == "ine.png", str(pend))
    rev("y la de `demo` ya no trae nada", bandeja.listar_pendientes("demo") == [])
    docs = documentos.listar("plataforma")
    rev("el documento nació en `plataforma` y conserva su id", len(docs) == 1 and docs[0]["id"] == demo["entrada"]["documentoId"] and docs[0]["tenant"] == "plataforma" and docs[0]["rutaRelativa"] == ruta_nueva, str(docs))
    tipos = tipos_documentales.listar("plataforma")
    rev("el tipo documental está en `plataforma` con su procesador intacto", len(tipos) == 1 and tipos[0]["id"] == demo["td"] and tipos[0]["datos"]["procesadorId"] == "proc-INE-demo", str(tipos))
    ej = tipos[0]["datos"]["documentosEjemplo"][0]
    rev("y su ejemplo apunta a la ruta nueva, en la ruta Y en la vista", ej["rutaRelativa"] == ruta_nueva and ej["rutaVista"] == ruta_nueva, str(ej))
    rev("lo que parecía una ruta pero no lo es NO se tocó (`demo/leeme.txt`, `mi-demo/…`)", "demo/leeme.txt" in ej["nota"] and "mi-demo/ab/cd/" + "e" * 64 in ej["nota"] and "plataforma/leeme" not in ej["nota"], ej["nota"])
    rev("`demo` ya no tiene tipos documentales", tipos_documentales.listar("demo") == [])
    rev("el procesador sigue siendo el dueño de ese tipo (la limpieza de huérfanos no lo verá huérfano)", tipos_documentales.procesadores_con_dueno().get("proc-INE-demo", {}).get("tenant") == "plataforma")
    tes = tipos_expediente.listar("plataforma")
    rev("el tipo de expediente pasó, con el tipo documental de su esquema", len(tes) == 1 and tes[0]["id"] == demo["te"] and demo["td"] in json.dumps(tes[0]), str(tes))
    exs = expedientes.listar("plataforma")
    rev("el expediente pasó, con su documento", len(exs) == 1 and exs[0]["id"] == demo["ex"] and exs[0]["totalDocumentos"] == 1, str(exs))
    rev("procesos, llaves y webhooks quedaron con el tenant nuevo", all(
        any(e.get("tenant") == "plataforma" and e.get("id") == i for e in registro.eventos(arch)) and not any(e.get("tenant") == "demo" for e in registro.eventos(arch))
        for arch, i in (("procesos.jsonl", "proceso-demo"), ("llaves.jsonl", "llave-demo"), ("webhooks.jsonl", "wh-demo"))))
    audit = registro.eventos("auditoria.jsonl")
    rev("la bitácora NO se reescribió: lo de antes sigue diciendo `demo`", any(e.get("tenantPedido") == "demo" for e in audit) and not any(e.get("tenantPedido") == "plataforma" for e in audit))
    rev("y quedó el rastro del cambio", any(e.get("accion") == "PLATFORM_TENANT_RENAMED" and e.get("de") == "demo" and e.get("a") == "plataforma" for e in audit))

    titulo("4 · Lo de OTRO tenant queda idéntico")
    despues = instantanea()
    rev("la carpeta y los registros propios de `cli-acme` no cambiaron ni un byte", all(despues.get(k) == v for k, v in otro_bytes.items()) and bool(otro_bytes))
    rev("sus renglones en los registros compartidos siguen siendo suyos", all(any(e.get("tenant") == "cli-acme" and e.get("id") == i for e in registro.eventos(arch)) for arch, i in (("procesos.jsonl", "proceso-cli-acme"), ("llaves.jsonl", "llave-cli-acme"), ("webhooks.jsonl", "wh-cli-acme"))))
    tipos_acme = tipos_documentales.listar("cli-acme")
    contenido_procesos = (RAIZ / ".registro" / "procesos.jsonl").read_bytes()
    rev("una línea de otro tenant escrita a mano (otros espacios y escapes) sigue IDÉNTICA, y una línea rota también", LINEA_RARA in contenido_procesos and LINEA_ROTA in contenido_procesos)
    rev("y su tipo documental sigue apuntando a SU ruta", tipos_acme[0]["datos"]["documentosEjemplo"][0]["rutaRelativa"] == otro["guardado"]["rutaRelativa"] and otro["guardado"]["rutaRelativa"].startswith("cli-acme/"))

    titulo("5 · Repetir se niega (el origen ya no existe) y no cambia nada")
    estado = instantanea(sin=("auditoria.jsonl",))
    codigo, texto = correr("--confirmar")
    rev("una segunda corrida: sin nada que mover (el destino ya tiene datos)", codigo == 2 and "ya tiene datos" in texto, texto)
    rev("y no cambió nada", instantanea(sin=("auditoria.jsonl",)) == estado)

    titulo("6 · Se deshace corriéndolo al revés")
    codigo, texto = correr("--de", "plataforma", "--a", "demo", "--confirmar")
    rev("sale con 0", codigo == 0, texto)
    rev("el archivo vuelve a leerse por su ruta de antes", almacen.leer(demo["guardado"]["rutaRelativa"]) == demo["contenido"])
    rev("el tipo documental vuelve a `demo` con su ejemplo en la ruta de antes", tipos_documentales.listar("demo")[0]["datos"]["documentosEjemplo"][0]["rutaRelativa"] == demo["guardado"]["rutaRelativa"] and tipos_documentales.listar("plataforma") == [])
    rev("y la bandeja, los documentos y el expediente también", [e["rutaRelativa"] for e in bandeja.listar_pendientes("demo")] == [demo["guardado"]["rutaRelativa"]] and len(documentos.listar("demo")) == 1 and len(expedientes.listar("demo")) == 1)
finally:
    shutil.rmtree(RAIZ, ignore_errors=True)

print()
if fallos:
    print(f"{fallos} FALLA(S)")
    raise SystemExit(1)
print("Todo bien.")
