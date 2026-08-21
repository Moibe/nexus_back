# Handoff — instalar el driver ODBC de SQL Server en el server de CSI

Para el agente que corre **en el server**, no en la máquina de desarrollo.

## Contexto

`nexus_back` es una API FastAPI que va a consumir SQL Server vía `pyodbc`. Vive
en `/home/mbriseno/code/nexus_back`, corre bajo pm2 como `nexus-back-api` en el
puerto 8083. La base todavía **no existe** (el DBA la está diseñando), pero el
driver ODBC hay que dejarlo listo desde ahora porque es un requisito de sistema
operativo, no de Python: `pip install pyodbc` NO trae el driver.

Sin el driver, cualquier conexión falla con un error que **parece** de
credenciales o de red, cuando en realidad es de driver faltante. Por eso se
resuelve antes de que el DBA entregue algo.

## Estado que se cree tener (verificar, no asumir)

Observado el 2026-08-17 desde la máquina de desarrollo, puede haber cambiado:

- Ubuntu 24.04.3 LTS (`noble`), usuario `mbriseno`, hostname `srviaproducto`.
- El **runtime** de unixODBC sí está: `libodbc.so.2` y `libodbcinst.so.2` en
  `/lib/x86_64-linux-gnu/`. Probablemente lo arrastró mariadb.
- El **CLI** `odbcinst` NO está instalado.
- El driver de Microsoft (`msodbcsql18`) NO está instalado.
- 12 procesos pm2 en línea, con días de uptime. **No tocarlos.**
- Había ~111 actualizaciones pendientes (13 de seguridad) y un reinicio
  pendiente. Ver la advertencia de abajo.

## Tareas

### 1. Verificar el punto de partida

```bash
which odbcinst || echo "odbcinst NO está"
odbcinst -q -d 2>/dev/null || echo "no se pueden listar drivers"
ls -la /lib/x86_64-linux-gnu/libodbc* 2>/dev/null
dpkg -l | grep -iE "unixodbc|msodbcsql" || echo "ningún paquete odbc instalado"
```

Reportar qué salió antes de instalar nada.

### 2. Instalar el driver de Microsoft

Seguir la guía oficial de Microsoft para **Ubuntu 24.04**, que consiste en
agregar su repositorio y luego instalar `msodbcsql18`. La variable
`ACCEPT_EULA=Y` es obligatoria o la instalación se queda esperando una respuesta
interactiva que nunca llega.

Instalar también el CLI para poder verificar (`odbcinst`). En Ubuntu 24.04 ese
binario puede venir en `unixodbc` o en un paquete aparte — **verificar con
`apt-cache`/`apt-file` en lugar de adivinar el nombre**.

Si `msodbcsql18` no estuviera disponible para `noble` en el repo, reportarlo y
**detenerse** — no sustituir por `msodbcsql17` sin avisar, porque el `.env` de la
app declara la versión 18 y el nombre del driver tiene que coincidir textual.

### 3. Verificar que quedó registrado

```bash
odbcinst -q -d
```

Debe listar algo como `[ODBC Driver 18 for SQL Server]`. **Copiar el nombre
EXACTO, entre corchetes, tal como aparece** — se necesita en el paso 5.

### 4. Verificar que Python lo ve

Esta es la prueba que de verdad importa: que `pyodbc`, desde el venv de la app,
enumere el driver.

```bash
cd /home/mbriseno/code/nexus_back
venv/bin/python -c "import pyodbc; print(pyodbc.drivers())"
```

Debe aparecer el driver en la lista.

Dos cosas que pueden salir mal aquí, y son distintas:

- **`ImportError` / no se puede importar `pyodbc`** → el paquete de Python no
  quedó bien instalado. Reinstalarlo dentro del venv:
  `venv/bin/pip install --force-reinstall pyodbc`. Ojo: hasta hoy la app nunca
  había importado `pyodbc` en ese server (el router de documentos solo se carga
  si hay `SQLSERVER_HOST`, que está vacío), así que **nunca se ha comprobado
  que ese import funcione ahí**. Es normal que sea la primera vez que se prueba.
- **Importa pero la lista sale vacía o sin el driver** → el driver no quedó
  registrado; volver al paso 3.

### 5. Alinear el nombre del driver en el `.env`

`config.py` ya trae un default en código: `ODBC Driver 18 for SQL Server`. Si
el nombre que reportó `odbcinst -q -d` es **exactamente** ese, no hay nada que
hacer — y es probable que el `.env` del server ni siquiera tenga la línea, lo
cual es correcto.

Solo si el nombre reportado **difiere** (aunque sea en un espacio, o porque
Microsoft cambió la nomenclatura de la versión), hay que fijarlo explícito:

```bash
grep SQLSERVER_DRIVER /home/mbriseno/code/nexus_back/.env   || echo "no está en .env → se usa el default de config.py"
```

Si hay que agregarlo o corregirlo, va el nombre **sin corchetes**, tal cual lo
reportó `odbcinst`. **No tocar ninguna otra variable** del `.env`.

### 6. Confirmar que la app sigue sana

```bash
pm2 restart nexus-back-api --update-env
curl -s http://127.0.0.1:8083/health
```

Debe seguir devolviendo `{"status":"ok",...}`.

**`/health/db` va a seguir fallando y eso es correcto**: `SQLSERVER_HOST` está
vacío porque la base no existe todavía. El mensaje esperado menciona que faltan
`SQLSERVER_HOST`/`SQLSERVER_DB`. Si en cambio apareciera un error de driver, algo
de los pasos anteriores no quedó.

## Qué NO hacer

- **No correr `apt upgrade` ni `apt dist-upgrade`.** Hay ~111 actualizaciones
  pendientes y 12 apps de otros proyectos corriendo en pm2. Instalar solo los
  paquetes específicos con `apt-get install <paquete>`.
- **No reiniciar el server**, aunque haya un reinicio pendiente. Eso se coordina
  aparte.
- **No tocar los otros procesos de pm2** ni `pm2 save` sin necesidad.
- **No llenar `SQLSERVER_HOST`, `SQLSERVER_DB`, `SQLSERVER_USER` ni
  `SQLSERVER_PASSWORD`.** Esos datos los va a dar el DBA; inventarlos haría que
  la app publique el grupo `/documentos/*` apuntando a una base inexistente.
- **No commitear el `.env`** (está en `.gitignore` a propósito).

## Qué reportar de vuelta

1. Qué había instalado antes (salida del paso 1).
2. El **nombre exacto del driver** que reportó `odbcinst -q -d`.
3. La salida de `pyodbc.drivers()`.
4. Si hubo que corregir `SQLSERVER_DRIVER` en el `.env`.
5. Que `/health` sigue en verde.

Con eso, del lado de desarrollo se cierra el paso 1 de
[`solicitudes-dba.md`](solicitudes-dba.md) — la pista paralela que no depende del
DBA.
