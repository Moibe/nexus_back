"""¿Los avisos de configuración dicen cuando FRONT_URL apunta mal? (2026-11-03)

    .venv/Scripts/python verificar_avisos_configuracion.py

Los enlaces de los correos (bienvenida, recuperar contraseña) salen de `FRONT_URL`. Tres cosas mal puestas ahí se han visto en los servidores: el localhost del
default, una URL repetida y, el 2026-10-13 y otra vez después, la IP del servidor en vez del dominio de cada ambiente. Offline: solo lee `config`.
"""

import importlib
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
fallos = 0


def rev(descripcion: str, ok: bool, extra: str = "") -> None:
    global fallos
    print(f"  {'OK   ' if ok else 'FALLA'} {descripcion}{'  -> ' + extra if extra and not ok else ''}")
    if not ok:
        fallos += 1


def avisos(front_url: str, ambiente: str = "dev") -> list[str]:
    os.environ["ENVIRONMENT"] = ambiente
    os.environ["FRONT_URL"] = front_url
    os.environ["CORREO_MODO"] = "smtp"
    import config

    importlib.reload(config)
    return [a for a in config.avisos_de_produccion() if a.startswith("FRONT_URL")]


print("\n=== FRONT_URL ===")
rev("un dominio con https: sin aviso", avisos("https://dev-nexus-doc.buzzword.com.mx") == [])
rev("con barra final: sin aviso", avisos("https://staging-biblioratum.buzzword.com.mx/") == [])
rev("la IP del servidor con puerto (como salió en el correo de dev): AVISA, y dice «dominio»", len(a := avisos("http://172.10.30.15:3401")) == 1 and "dominio" in a[0] and "IP" in a[0], str(a))
rev("una IP sin puerto: avisa", len(avisos("http://172.10.30.15")) == 1)
rev("una IP v6: avisa", len(avisos("http://[::1]:3400")) == 1 or len(avisos("http://[2001:db8::1]:3400")) == 1)
rev("localhost: avisa con SU aviso (apuntaría a quien recibe el correo)", len(a := avisos("http://localhost:3400")) == 1 and "RECIBE" in a[0], str(a))
rev("en `local` no avisa nada, ni con IP ni con localhost", avisos("http://172.10.30.15:3401", "local") == [] and avisos("http://localhost:3400", "local") == [])
rev("un dominio que empieza con números (no es IP): sin aviso", avisos("https://10-dev.buzzword.com.mx") == [])

print()
if fallos:
    print(f"{fallos} FALLA(S)")
    raise SystemExit(1)
print("Todo bien.")
