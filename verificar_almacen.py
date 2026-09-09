"""Verifica el almacén de documentos. Correr a mano:

    venv/bin/python verificar_almacen.py

No usa pytest, igual que los otros `verificar_*.py`: el proyecto no tiene
framework de pruebas y esto no justifica meter una dependencia.

NO TOCA NADA REAL. Trabaja en una carpeta temporal que crea y borra él mismo,
así que se puede correr en cualquier máquina sin NAS, sin credenciales y sin
red. Por eso mismo hay que fijar `ALMACEN_RUTA` en el entorno ANTES de importar
el módulo: `servicios/almacen.py` hace `from config import ALMACEN_RUTA`, que
congela el valor al importar (misma convención que el resto del proyecto — la
configuración se lee una vez, al arrancar).

Qué protege y por qué importa. El almacén es el único lugar del sistema donde
un documento del cliente puede perderse de verdad: si se escribe mal, no hay de
dónde recuperarlo. Las tres cosas que se verifican y que fallarían EN SILENCIO
son:

  1. **La escritura es atómica.** Se escribe a un temporal y se hace
     `os.replace`. Si eso se rompiera, un corte del NAS a media escritura
     dejaría un archivo truncado con nombre de archivo completo: basura que se
     ve válida. Aquí se comprueba además que no quede ningún `.tmp-*` tirado.
  2. **No hay deduplicación entre tenants.** Dos clientes con el mismo
     documento tienen que tener DOS objetos. Si se colapsaran, borrar el de uno
     se llevaría el del otro, y el almacén filtraría qué documentos tiene el
     vecino.
  3. **No se puede escribir ni leer fuera de la raíz.** Las rutas que reciben
     `leer` y `borrar` vienen de la BASE, no de código; una fila corrupta no
     debe poder convertirse en un `../../etc/passwd`.
"""

import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

# ANTES del import de servicios.almacen — ver el docstring.
RAIZ = Path(tempfile.mkdtemp(prefix="verificar-almacen-"))
os.environ["ALMACEN_RUTA"] = str(RAIZ)

from servicios import almacen  # noqa: E402

TENANT = "csi"
OTRO_TENANT = "acme"
CONTENIDO = b"%PDF-1.4 finge que soy una INE\n"
# El hash se CALCULA, no se transcribe, para que el verificador no mienta si
# alguien edita CONTENIDO.
HASH = hashlib.sha256(CONTENIDO).hexdigest()

fallos = 0


def revisar(descripcion: str, condicion: bool, detalle: str = "") -> None:
    global fallos
    if condicion:
        print(f"  OK    {descripcion}")
        return
    fallos += 1
    print(f"  FALLA {descripcion}")
    if detalle:
        print(f"        {detalle}")


def revisar_levanta(descripcion: str, excepcion, funcion) -> None:
    """Comprueba que algo truene, y que truene con el tipo CORRECTO.

    El tipo importa: `ValueError` significa "entrada mala, no reintentes" y
    `ErrorAlmacen` significa "el NAS falló, quizá reintentar sirva". Confundir
    uno con otro haría que quien llame tome la decisión equivocada.
    """
    global fallos
    try:
        funcion()
    except excepcion:
        print(f"  OK    {descripcion}")
        return
    except Exception as exc:  # noqa: BLE001
        fallos += 1
        print(f"  FALLA {descripcion}")
        print(f"        levantó {type(exc).__name__} en vez de {excepcion.__name__}: {exc}")
        return
    fallos += 1
    print(f"  FALLA {descripcion}")
    print(f"        no levantó nada; se esperaba {excepcion.__name__}")


def main() -> int:
    print(f"--- almacén de documentos (raíz temporal: {RAIZ}) ---")

    print("\n[forma de la ruta]")
    relativa = almacen.ruta_relativa(TENANT, HASH)
    esperada = f"{TENANT}/{HASH[0:2]}/{HASH[2:4]}/{HASH}"
    revisar(
        "la ruta relativa es tenant/aa/bb/hash",
        relativa == esperada,
        f"esperada={esperada!r} obtenida={relativa!r}",
    )
    revisar(
        "la ruta NO trae la raíz absoluta (para que mover el NAS no migre datos)",
        not Path(relativa).is_absolute() and str(RAIZ) not in relativa,
    )

    print("\n[validación de entradas: la defensa contra path traversal]")
    revisar_levanta(
        "hash que no es sha256 hex", ValueError,
        lambda: almacen.ruta_relativa(TENANT, "no-soy-un-hash"),
    )
    revisar_levanta(
        "hash con la longitud equivocada", ValueError,
        lambda: almacen.ruta_relativa(TENANT, "abc123"),
    )
    revisar_levanta(
        "tenant con separador de ruta", ValueError,
        lambda: almacen.ruta_relativa("../otro", HASH),
    )
    revisar_levanta("tenant vacío", ValueError, lambda: almacen.ruta_relativa("", HASH))
    revisar_levanta(
        "leer una ruta que sale de la raíz", ValueError,
        lambda: almacen.leer("../../etc/passwd"),
    )
    revisar_levanta(
        "borrar una ruta que sale de la raíz", ValueError,
        lambda: almacen.borrar("../../etc/passwd"),
    )

    print("\n[guardar]")
    guardado = almacen.guardar(TENANT, CONTENIDO)
    revisar(
        "devuelve la ruta relativa esperada",
        guardado["rutaRelativa"] == esperada,
        f"obtenida={guardado['rutaRelativa']!r}",
    )
    revisar("devuelve el hash calculado sobre los bytes reales", guardado["hash"] == HASH)
    revisar("devuelve el tamaño en bytes", guardado["bytes"] == len(CONTENIDO))
    revisar("la primera vez, yaExistia=False", guardado["yaExistia"] is False)
    en_disco = RAIZ / esperada
    revisar("el archivo está en disco", en_disco.is_file())
    revisar(
        "los bytes en disco son EXACTAMENTE los que se mandaron",
        en_disco.read_bytes() == CONTENIDO,
    )

    # Si esto falla, la escritura atómica dejó basura: un `.tmp-*.parcial`
    # abandonado por cada guardado llenaría el NAS sin que nadie lo note.
    temporales = list(en_disco.parent.glob(".tmp-*"))
    revisar("no quedó ningún temporal tirado", not temporales, f"quedaron: {temporales}")

    print("\n[idempotencia]")
    repetido = almacen.guardar(TENANT, CONTENIDO)
    revisar("guardar lo mismo dos veces reporta yaExistia=True", repetido["yaExistia"] is True)
    revisar(
        "y no crea un segundo archivo",
        len([p for p in en_disco.parent.iterdir() if p.is_file()]) == 1,
    )

    print("\n[verificación del hash que manda quien llama]")
    revisar_levanta(
        "hash esperado que no coincide con los bytes", ValueError,
        lambda: almacen.guardar(TENANT, b"otros bytes", HASH),
    )
    coincide = almacen.guardar(TENANT, CONTENIDO, HASH)
    revisar("hash esperado correcto: pasa sin quejarse", coincide["hash"] == HASH)
    revisar_levanta("contenido vacío", ValueError, lambda: almacen.guardar(TENANT, b""))

    print("\n[aislamiento entre tenants]")
    ajeno = almacen.guardar(OTRO_TENANT, CONTENIDO)
    revisar(
        "el mismo contenido en otro tenant vive en OTRA ruta",
        ajeno["rutaRelativa"] != guardado["rutaRelativa"],
    )
    revisar(
        "y de verdad son dos archivos en disco",
        (RAIZ / ajeno["rutaRelativa"]).is_file() and en_disco.is_file(),
    )

    print("\n[leer / existe]")
    revisar("leer devuelve los bytes idénticos", almacen.leer(esperada) == CONTENIDO)
    revisar("existe() dice True de lo que está", almacen.existe(esperada) is True)
    inventada = almacen.ruta_relativa(TENANT, "f" * 64)
    revisar("existe() dice False de lo que no está", almacen.existe(inventada) is False)
    revisar_levanta(
        "leer algo que no está es ErrorAlmacen, no ValueError",
        almacen.ErrorAlmacen, lambda: almacen.leer(inventada),
    )

    print("\n[borrar]")
    revisar("borrar lo que está devuelve True", almacen.borrar(esperada) is True)
    revisar("y el archivo ya no está", not en_disco.exists())
    revisar("borrar otra vez devuelve False, sin tronar", almacen.borrar(esperada) is False)
    # El objeto del otro tenant tiene el MISMO hash. Si sobrevivió, la
    # deduplicación no cruza clientes — que es justo lo que se quiere.
    revisar(
        "borrar en un tenant NO se llevó el objeto del otro",
        (RAIZ / ajeno["rutaRelativa"]).is_file(),
    )

    print("\n[apagado: sin ALMACEN_RUTA]")
    # Se apaga a mano el valor ya importado. Es el estado real de hoy en
    # producción: la variable viene vacía y nadie llama al módulo.
    original = almacen.ALMACEN_RUTA
    almacen.ALMACEN_RUTA = ""
    try:
        revisar("esta_configurado() responde False", almacen.esta_configurado() is False)
        revisar_levanta(
            "guardar con el almacén apagado avisa claro y no inventa carpeta",
            almacen.ErrorAlmacen, lambda: almacen.guardar(TENANT, CONTENIDO),
        )
    finally:
        almacen.ALMACEN_RUTA = original

    print()
    if fallos:
        print(f"{fallos} comprobaciones FALLARON")
        return 1
    print("todas las comprobaciones pasaron")
    return 0


if __name__ == "__main__":
    try:
        codigo = main()
    finally:
        shutil.rmtree(RAIZ, ignore_errors=True)
    sys.exit(codigo)
