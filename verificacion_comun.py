"""Lo que comparten los `verificar_*` que llaman rutas con la llave de servicio (HU11).

Desde el 2026-10-14 las rutas de bandeja, llaves, webhooks y archivos piden TAMBIÉN el JWT de una persona: el BFF del front
lo reenvía en cada llamada, y el back comprueba que esa persona pueda operar en el tenant (`servicios/aislamiento.py`).

Los verificadores que se escribieron antes hablan con la llave de servicio sola. En vez de tocar cada llamada, el cliente de
prueba hace lo que hace el BFF: agrega el Bearer de un administrador de plataforma a toda petición que lleve la llave de
servicio. Ese administrador opera sobre cualquier tenant que NO sea el guid de una organización ("demo", "cli-acme"…), que
es lo que usan estas pruebas. Las peticiones con una API Key de cliente (`nxdoc_live_…`) pasan tal cual.

Quién prueba que SIN persona, o con la persona equivocada, se rechaza es `verificar_aislamiento.py`.

Uso, ANTES de importar `app`:

    import verificacion_comun                      # fija AUTH_JWT_SECRET
    ...
    c = verificacion_comun.cliente_con_sesion_de_plataforma(app, SERVICIO)
"""

import os

import httpx

# Importar este módulo es lo que deja el secreto puesto: `config` lo lee UNA vez, al importarse.
os.environ.setdefault("AUTH_JWT_SECRET", "secreto-de-prueba-de-al-menos-32-caracteres-xx")

CORREO = "plataforma.verificacion@ejemplo.com"
CONTRASENA = "Cobre-Lento-Azul-47!"


def cliente_con_sesion_de_plataforma(app, servicio: str):
    """Un `TestClient` que, en toda petición con la llave de servicio, manda también el Bearer de un administrador de
    plataforma recién creado (con su primer acceso hecho)."""
    from fastapi.testclient import TestClient

    from servicios import auth, usuarios

    c = TestClient(app)
    llave = {"x-api-key": servicio}

    def entrar(contrasena: str) -> dict:
        r = c.post("/auth/login", json={"email": CORREO, "password": contrasena, "ip": "127.0.0.1", "userAgent": "verificar"}, headers=llave)
        assert r.status_code == 200, r.text
        return r.json()

    temporal = auth.contrasena_temporal()
    usuarios.bootstrap_admin(CORREO, "Ada", "Lovelace", auth.hash_de(temporal))
    primera = entrar(temporal)
    r = c.post("/auth/contrasena", json={"nueva": CONTRASENA, "confirmacion": CONTRASENA}, headers={**llave, "authorization": f"Bearer {primera['accessToken']}"})
    assert r.status_code == 200, r.text
    bearer = entrar(CONTRASENA)["accessToken"]

    pedir = c.request

    def request(method, url, **kw):
        cabeceras = httpx.Headers(kw.get("headers"))
        if cabeceras.get("x-api-key") == servicio and "authorization" not in cabeceras:
            cabeceras["authorization"] = f"Bearer {bearer}"
            kw["headers"] = cabeceras
        return pedir(method, url, **kw)

    c.request = request
    return c
