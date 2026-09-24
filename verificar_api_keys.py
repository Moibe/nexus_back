"""Verifica el formato y la verificación de las API keys de cliente. Correr a mano:

    venv/bin/python verificar_api_keys.py

No usa pytest a propósito, igual que los demás verificadores: el proyecto no
tiene framework de pruebas y esto no justifica meter una dependencia.

QUÉ PROTEGE Y POR QUÉ IMPORTA

Dos cosas, y las dos fallarían EN SILENCIO:

1. EL ACUERDO CON EL FRONT. El formato se define en dos lenguajes
   (`src/lib/apiKeys/formato.ts` allá, `seguridad_llaves.py` aquí). Si los dos
   checksums dejan de coincidir, esta API rechazaría llaves BUENAS — y el
   síntoma sería "mi llave no funciona", sin error ni log que apunte al
   verdadero motivo. Por eso los vectores de abajo NO están inventados aquí:
   son llaves generadas por el JavaScript real y pegadas tal cual.

2. EL ORDEN DE LA VERIFICACIÓN. `verificar()` rechaza por cinco motivos y
   todos devuelven lo mismo. Si alguno dejara de aplicarse —sobre todo el de
   revocada— una llave muerta seguiría funcionando y nada lo diría: la
   pantalla del front seguiría mostrándola en rojo, que es exactamente la
   falsa sensación de seguridad que este módulo existe para evitar.
"""

import sys
from datetime import datetime, timedelta, timezone

from seguridad_llaves import (
    checksum_de,
    hash_de,
    id_de,
    validar_formato,
    verificar,
)

# Llaves generadas por `generarApiKey()` del FRONT (Node, 2026-09-24) y pegadas
# tal cual. Si el formato cambia de un lado, estos vectores dejan de validar.
VECTORES_DEL_FRONT = [
    ("nxdoc_live_PWpc_sk_WBaGRiphmOKdoyqmeEtQOzC6EgcI0xvw", "PWpc"),
    ("nxdoc_live_UVrW_sk_VwgksrNFU5hpeX1RovMaCkk8Q9d97RST", "UVrW"),
    ("nxdoc_live_FH6h_sk_c5vXAb1vWyNHrEEGrnRUS6bWSvaDxdbH", "FH6h"),
]

# Calculado por `checksumDe()` del front sobre la misma entrada.
CHECKSUM_CONOCIDO = ("prueba", "b1f5ko")

BASURA = [
    "",
    "hola",
    "nxdoc_live_a7K9_sk_corto",
    "sk_live_" + "a" * 40,
    # Una llave buena con un carácter de más: el largo es parte del formato.
    "nxdoc_live_PWpc_sk_WBaGRiphmOKdoyqmeEtQOzC6EgcI0xvwX",
    # Otro producto, formato calcado.
    "otro_live_PWpc_sk_WBaGRiphmOKdoyqmeEtQOzC6EgcI0xvw",
]

AHORA = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
LLAVE, ID = VECTORES_DEL_FRONT[0]


def fila(**cambios):
    """La fila que el SP devolvería para `LLAVE`, con los cambios del caso."""
    base = {
        "id": ID,
        "secret_hash": hash_de(LLAVE),
        "revocada_en": None,
        "expira_en": AHORA + timedelta(days=30),
    }
    base.update(cambios)
    return base


def buscar(devuelve):
    return lambda _id: devuelve


def truena(_id):
    raise AssertionError("se consultó la base con una llave mal formada")


# (descripción, secret, buscar_por_id, esperado)
CASOS_VERIFICAR = [
    ("llave buena, viva y no revocada", LLAVE, buscar(fila()), True),
    ("el id no existe en la base", LLAVE, buscar(None), False),
    (
        "el hash guardado es de OTRA llave",
        LLAVE,
        buscar(fila(secret_hash=hash_de(VECTORES_DEL_FRONT[1][0]))),
        False,
    ),
    (
        "revocada (lo demás en orden)",
        LLAVE,
        buscar(fila(revocada_en=AHORA - timedelta(days=1))),
        False,
    ),
    (
        "expiró ayer",
        LLAVE,
        buscar(fila(expira_en=AHORA - timedelta(days=1))),
        False,
    ),
    (
        "expira en un segundo: todavía sirve",
        LLAVE,
        buscar(fila(expira_en=AHORA + timedelta(seconds=1))),
        True,
    ),
    (
        "expira_en sin zona horaria (como lo devuelve SQL Server)",
        LLAVE,
        buscar(fila(expira_en=datetime(2026, 12, 31, 0, 0))),
        True,
    ),
    # El más importante de todos: una llave mal formada NO debe llegar a la
    # base. Si `truena` se ejecuta, el orden de la verificación se rompió.
    ("una llave mal formada nunca toca la base", "hola", truena, False),
    ("una llave con dedazo tampoco toca la base", LLAVE[:20] + "X" + LLAVE[21:], truena, False),
]

def main() -> int:
    fallos = 0

    print("--- acuerdo con el front ---")
    entrada, esperado = CHECKSUM_CONOCIDO
    obtenido = checksum_de(entrada)
    bien = obtenido == esperado
    fallos += 0 if bien else 1
    print(f"  {'OK ' if bien else 'FALLA'} checksum_de({entrada!r}) -> {obtenido!r}"
          + ("" if bien else f" (esperado {esperado!r})"))

    for secret, identificador in VECTORES_DEL_FRONT:
        valida = validar_formato(secret)
        mismo_id = id_de(secret) == identificador
        bien = valida and mismo_id
        fallos += 0 if bien else 1
        print(f"  {'OK ' if bien else 'FALLA'} llave del front {secret[:24]}…"
              f"  valida={valida} id={id_de(secret)!r}")

    print("--- se rechaza lo que no es una llave ---")
    for cadena in BASURA:
        bien = not validar_formato(cadena)
        fallos += 0 if bien else 1
        print(f"  {'OK ' if bien else 'FALLA'} {cadena[:46]!r}")

    print("--- el checksum atrapa un dedazo ---")
    # Se cambia UN carácter en cada posición del cuerpo aleatorio.
    atrapados = 0
    intentos = 0
    for i in range(19, len(LLAVE) - 6):
        otro = "a" if LLAVE[i] != "a" else "b"
        intentos += 1
        if not validar_formato(LLAVE[:i] + otro + LLAVE[i + 1:]):
            atrapados += 1
    bien = atrapados == intentos
    fallos += 0 if bien else 1
    print(f"  {'OK ' if bien else 'FALLA'} {atrapados}/{intentos} dedazos rechazados")

    print("--- verificación completa ---")
    for descripcion, secret, lookup, esperado_v in CASOS_VERIFICAR:
        try:
            obtenido_v = verificar(secret, lookup, ahora=AHORA)
        except AssertionError as exc:
            fallos += 1
            print(f"  FALLA {descripcion}: {exc}")
            continue
        bien = obtenido_v == esperado_v
        fallos += 0 if bien else 1
        print(f"  {'OK ' if bien else 'FALLA'} {descripcion} -> {obtenido_v}"
              + ("" if bien else f" (esperado {esperado_v})"))

    print()
    if fallos:
        print(f"{fallos} casos FALLARON")
        return 1
    print("todos los casos pasaron")
    return 0


if __name__ == "__main__":
    sys.exit(main())
