"""¿Purga bien `purgar_creado_por.py` lo que crearon administradores retirados, y SOLO eso?

    .venv/Scripts/python verificar_purga.py

OFFLINE: arma un almacén temporal con tres superadmins (dos que se retiran y uno que se queda) y tres organizaciones (una de
cada retirado y una del que se queda), con datos en cada registro. Comprueba que la simulación no toque nada, que
`--confirmar` borre lo de los retirados y deje INTACTO lo del que se queda, y que los candados se nieguen.
"""

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-purga-"))
(RAIZ / ".nexus-almacen").touch()
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"
os.environ.setdefault("SQLSERVER_HOST", "")

import purgar_creado_por as purga  # noqa: E402
from servicios import almacen, auditoria, auth, registro, tenants_registro, usuarios  # noqa: E402

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
    sys.argv = ["purgar_creado_por.py", *argumentos]
    try:
        with contextlib.redirect_stdout(salida), contextlib.redirect_stderr(salida):
            codigo = purga.main()
    finally:
        sys.argv = anterior
    return codigo, salida.getvalue()


def contenido(nombre: str) -> str:
    ruta = RAIZ / ".registro" / nombre
    return ruta.read_text(encoding="utf-8") if ruta.exists() else ""


def instantanea() -> dict:
    return {str(p.relative_to(RAIZ)): p.read_bytes() for p in RAIZ.rglob("*") if p.is_file() and ".respaldo-purga" not in str(p)}


def organizacion(nombre: str, actor: dict, correo_admin: str) -> dict:
    """Como `POST /organizaciones`: el administrador, la organización, su membresía y el USER_CREATED del actor."""
    admin = usuarios.crear_usuario(correo_admin, "Admin", "De " + nombre, auth.hash_de("x"))
    org = tenants_registro.crear(nombre, admin["guid"])
    usuarios.agregar_membresia(admin["guid"], org["guid"], "ADMIN")
    auditoria.registrar(auditoria.USER_CREATED, usuario_guid=admin["guid"], rol="ADMIN", tenantGuid=org["guid"], actor=actor["guid"], correo="enviado")
    miembro = usuarios.crear_usuario(f"miembro.{nombre.lower()}@ejemplo.com", "Miembro", "De " + nombre, auth.hash_de("x"), tenant_guid=org["guid"], rol="OPERADOR")
    auditoria.registrar(auditoria.USER_CREATED, usuario_guid=miembro["guid"], rol="OPERADOR", tenantGuid=org["guid"], actor=admin["guid"], correo="enviado")
    t = org["guid"]
    registro.agregar("tipos-documentales.jsonl", {"op": "guardar", "id": f"td-{nombre}", "tenant": t, "revision": 1, "datos": {}, "por": admin["guid"], "en": "2026-10-20T00:00:00+00:00"})
    registro.agregar("procesos.jsonl", {"op": "guardar", "id": f"pr-{nombre}", "tenant": t, "revision": 1, "datos": {}, "por": admin["guid"], "en": "2026-10-20T00:00:00+00:00"})
    registro.agregar("llaves.jsonl", {"evento": "emitida", "id": f"llave-{nombre}", "tenant": t, "hash": "h" * 8, "creadaEn": "2026-10-20T00:00:00+00:00"})
    registro.agregar("llaves.jsonl", {"evento": "revocada", "id": f"llave-{nombre}", "en": "2026-10-21T00:00:00+00:00"})
    registro.agregar("uso-llaves.jsonl", {"en": "2026-10-20T00:00:00.000+00:00", "llave": f"llave-{nombre}", "ruta": "/x", "metodo": "GET", "status": 200, "ms": 1})
    registro.agregar("webhooks.jsonl", {"evento": "creado", "id": f"wh-{nombre}", "tenant": t, "url": "https://x", "en": "2026-10-20T00:00:00+00:00"})
    registro.agregar("webhooks.jsonl", {"evento": "estado", "id": f"wh-{nombre}", "estado": "ACTIVE", "en": "2026-10-20T00:00:00+00:00"})
    registro.agregar("entregas-webhooks.jsonl", {"evento": "programada", "id": f"en-{nombre}", "webhook": f"wh-{nombre}", "tenant": t, "tipo": "x", "en": "2026-10-20T00:00:00+00:00"})
    registro.agregar("tokens.jsonl", {"evento": "creado", "guid": f"tk-{nombre}", "hash": f"hash-{nombre}", "usuarioGuid": admin["guid"], "tipo": "x", "en": "2026-10-20T00:00:00+00:00"})
    registro.agregar("tokens.jsonl", {"evento": "usado", "hash": f"hash-{nombre}", "en": "2026-10-20T00:00:00+00:00"})
    for prefijo in purga.PREFIJOS_POR_ORGANIZACION:
        registro.agregar(f"{prefijo}-{t}.jsonl", {"op": "x", "tenant": t, "en": "2026-10-20T00:00:00+00:00"})
    contenido_bytes = f"documento de {nombre}".encode()
    almacen.guardar(t, contenido_bytes, hashlib.sha256(contenido_bytes).hexdigest())
    return {"org": org, "admin": admin, "miembro": miembro}


try:
    titulo("0 · Preparar")
    A = usuarios.bootstrap_admin("a@yopmail.com", "Moi", "Uno", auth.hash_de("x"))
    B = usuarios.bootstrap_admin("b@hotmail.com", "Cesar", "Dos", auth.hash_de("x"))
    K = usuarios.bootstrap_admin("kept@grupocsi.com", "Moi", "Queda", auth.hash_de("x"))
    de_a = organizacion("OrgA", A, "admin.a@ejemplo.com")
    de_b = organizacion("OrgB", B, "admin.b@ejemplo.com")
    de_k = organizacion("OrgK", K, "admin.k@ejemplo.com")
    # A y B también dieron de alta a alguien suelto, sin organización.
    suelto = usuarios.crear_usuario("suelto@ejemplo.com", "Suelto", "Uno", auth.hash_de("x"))
    auditoria.registrar(auditoria.USER_CREATED, usuario_guid=suelto["guid"], rol="ADMIN", tenantGuid=None, actor=A["guid"], correo="enviado")
    # Una cuenta con membresía en OrgA Y en OrgK: es el caso del candado.
    compartido = usuarios.crear_usuario("compartido@ejemplo.com", "Compartido", "Uno", auth.hash_de("x"), tenant_guid=de_a["org"]["guid"], rol="OPERADOR")
    usuarios.agregar_membresia(compartido["guid"], de_k["org"]["guid"], "OPERADOR")
    usuarios.eliminar_usuario(A["guid"], "prueba", por="CLI")
    usuarios.eliminar_usuario(B["guid"], "prueba", por="CLI")

    titulo("1 · Listar retirados y los candados")
    codigo, texto = correr("--listar-retirados")
    rev("lista a los dos superadmins retirados y no al que queda", "a@yopmail.com" in texto and "b@hotmail.com" in texto and "kept@" not in texto, texto)
    codigo, texto = correr("--email", "kept@grupocsi.com")
    rev("se niega con un superadmin que SIGUE existiendo", codigo == 2 and "SIGUE existiendo" in texto, texto)
    codigo, texto = correr("--email", "nadie@x.com")
    rev("se niega con un correo que no aparece", codigo == 2 and "no aparece" in texto, texto)
    codigo, texto = correr("--email", "a@yopmail.com", "--email", "b@hotmail.com")
    rev("se niega si alguien a borrar también es miembro de una organización que se queda (sin --forzar)", codigo == 2 and "NO se purgan" in texto, texto)

    titulo("2 · La simulación no toca nada")
    antes = instantanea()
    codigo, texto = correr("--email", "a@yopmail.com", "--email", "b@hotmail.com", "--forzar")
    rev("sale con 0 y dice que es simulación", codigo == 0 and "SIMULACIÓN" in texto, texto)
    rev("lista las dos organizaciones de los retirados y no la que se queda", "OrgA" in texto and "OrgB" in texto and "OrgK" not in texto, texto)
    rev("avisa del compartido que se borraría por --forzar", "AVISO: compartido@ejemplo.com" in texto and "(se borra por --forzar)" in texto, texto)
    rev("no cambió ni un byte del almacén", instantanea() == antes)

    titulo("3 · Con --confirmar borra lo de A y B")
    codigo, texto = correr("--email", "a@yopmail.com", "--email", "b@hotmail.com", "--forzar", "--confirmar")
    rev("sale con 0", codigo == 0, texto)
    respaldos = list(RAIZ.glob(".respaldo-purga-*"))
    rev("dejó un respaldo de .registro/", len(respaldos) == 1 and (respaldos[0] / "usuarios.jsonl").exists())
    for etiqueta, quien in (("OrgA", de_a), ("OrgB", de_b)):
        t = quien["org"]["guid"]
        rev(f"{etiqueta}: su carpeta de documentos ya no existe", not (RAIZ / t).exists())
        rev(f"{etiqueta}: sus registros por organización ya no existen", not any((RAIZ / ".registro" / f"{p}-{t}.jsonl").exists() for p in purga.PREFIJOS_POR_ORGANIZACION))
        todo = "".join(contenido(n) for n in purga.REGISTROS_COMPARTIDOS)
        rev(f"{etiqueta}: no queda su guid ni sus personas ni sus ids en los registros compartidos", t not in todo.replace("PLATFORM_PURGE", "") and quien["admin"]["guid"] not in todo and quien["miembro"]["guid"] not in todo and f"llave-{etiqueta}" not in todo and f"wh-{etiqueta}" not in todo and f"hash-{etiqueta}" not in todo and f"en-{etiqueta}" not in todo)
    rev("la persona suelta que dieron de alta tampoco queda", suelto["guid"] not in contenido("usuarios.jsonl"))
    rev("ni los propios superadmins retirados", A["guid"] not in contenido("usuarios.jsonl") and B["guid"] not in contenido("usuarios.jsonl"))
    rev("la bitácora anota la purga", '"PLATFORM_PURGE"' in contenido("auditoria.jsonl"))

    titulo("4 · Lo del que se queda queda INTACTO")
    tk = de_k["org"]["guid"]
    rev("sigue el superadmin que se quedó", usuarios.para_login("kept@grupocsi.com") is not None)
    rev("sigue la organización OrgK con su administrador y su miembro", tenants_registro.por_guid(tk)["nombre"] == "OrgK" and usuarios.para_login("admin.k@ejemplo.com") is not None and usuarios.para_login("miembro.orgk@ejemplo.com") is not None)
    rev("su carpeta de documentos sigue ahí", (RAIZ / tk).is_dir() and any((RAIZ / tk).rglob("*")))
    rev("sus registros por organización siguen", all((RAIZ / ".registro" / f"{p}-{tk}.jsonl").exists() for p in purga.PREFIJOS_POR_ORGANIZACION))
    for nombre, marca in (("tipos-documentales.jsonl", "td-OrgK"), ("procesos.jsonl", "pr-OrgK"), ("llaves.jsonl", "llave-OrgK"), ("webhooks.jsonl", "wh-OrgK"), ("entregas-webhooks.jsonl", "en-OrgK"), ("uso-llaves.jsonl", "llave-OrgK"), ("tokens.jsonl", "hash-OrgK")):
        rev(f"{nombre}: conserva lo de OrgK", marca in contenido(nombre))
    rev("la persona compartida (también de OrgK) se borró por --forzar", usuarios.para_login("compartido@ejemplo.com") is None)

    titulo("5 · Repetir no rompe nada")
    antes = instantanea()
    codigo, texto = correr("--email", "a@yopmail.com", "--email", "b@hotmail.com", "--forzar", "--confirmar")
    rev("una segunda corrida se niega (ya no aparecen en el registro) y no cambia nada", codigo == 2 and "no aparece" in texto and instantanea() == antes, texto)
    rev("OrgK sigue intacta tras la segunda", tenants_registro.por_guid(tk)["nombre"] == "OrgK")
finally:
    import shutil

    shutil.rmtree(RAIZ, ignore_errors=True)

print(f"\n{'TODO BIEN' if not fallos else str(fallos) + ' FALLA(S)'}")
sys.exit(1 if fallos else 0)
