# Migraciones de base de datos dentro del deploy de nexus_back

Para: el agente de webhook-central en `srviaproducto`. Pedido por Moisés (dueño de Nexus), 2026-10-28.

## Qué se quiere

Que cada deploy del back de Nexus aplique solo las migraciones de base de datos que falten, **después de bajar el código y antes de reiniciar el proceso**. Así la base siempre llega
antes que el código que la usa, sin que nadie tenga que acordarse. Si la migración falla, el deploy tiene que **detenerse sin reiniciar**, y la app vieja sigue corriendo con su base intacta.

El corredor ya está escrito y probado (`python -m migraciones`, carpeta `migraciones/` del repo `nexus_back`). Tu parte es solo engancharlo al deploy.

## Hoy: solo DEV

Haz **únicamente** `nexus_back_dev`. Staging y producción se activan después, uno por uno (ver «Más adelante»). No agregues nada a esos `.conf` todavía.

## Estado verificado (2026-10-28)

- Código: el back de dev ya trae `migraciones/` (commit `16df457` o posterior, rama `dev`, carpeta `~/code/nexus_back-dev`).
- Credenciales: existe `~/.nexus-sql-ddl.env` de `mbriseno` (host, puerto, usuario `usrNexusDDL`, contraseña; permisos 600). **No lo imprimas ni lo copies.**
- Base: `IA_Nexus_Dev` ya tiene la línea base `0001` marcada. Un `aplicar` hoy responde «no hay migraciones pendientes».
- El `.env` de `nexus_back-dev` tiene `SQLSERVER_DB=IA_Nexus_Dev`.

## Qué hacer, en orden

1. **Comprueba los requisitos** (sin cambiar nada), desde `~/code/nexus_back-dev`:
   ```
   ls -l ~/.nexus-sql-ddl.env                      # debe ser -rw------- y de mbriseno
   venv/bin/python -m migraciones estado --base IA_Nexus_Dev
   ```
   Debe salir `0001_linea_base  marcada`.

2. **Mira cómo `scripts/deploy.sh` ejecuta `POST_BUILD`** y responde estas tres cosas antes de editar nada:
   - ¿Desde qué carpeta corre? (debe ser la del proyecto, para que `venv/bin/python` y `.env` se resuelvan).
   - ¿Corre después del `pip install` y antes del `pm2 restart`?
   - **Si el comando sale con código distinto de cero, ¿el deploy se aborta antes del `pm2 restart` y queda registrado como fallido en `logs/deploys.jsonl`?**
     Si hoy no aborta (por ejemplo, ignora el código de salida o no tiene `set -e`), hay que cambiarlo para que aborte. Es lo más importante de este trabajo. Ojo: `deploy.sh` es compartido por todos
     los proyectos; si lo cambias, comprueba que los demás siguen igual (un `POST_BUILD` que falla debería abortar en todos; las apps con Drizzle ya lo usan para migrar).

3. **Agrega el `POST_BUILD`** en `projects/nexus_back_dev.conf` (ajusta la ruta de Python a como la usa el `deploy.sh` de ese proyecto):
   ```
   POST_BUILD="venv/bin/python -m migraciones aplicar --base IA_Nexus_Dev --confirmar --esperar-env .env"
   ```
   `--esperar-env .env` hace que el corredor se niegue a migrar si el `SQLSERVER_DB` del `.env` de ese back no es la base indicada (protege contra un `.conf` mal copiado).

4. **Pruébalo a mano**, desde `~/code/nexus_back-dev`, con el mismo comando del `POST_BUILD`:
   ```
   venv/bin/python -m migraciones aplicar --base IA_Nexus_Dev --confirmar --esperar-env .env; echo "salida=$?"
   ```
   Debe imprimir `IA_Nexus_Dev: no hay migraciones pendientes.` y `salida=0`.
   Y comprueba que la guarda funciona (esto NO toca nada): `venv/bin/python -m migraciones aplicar --base IA_Nexus_Staging --confirmar --esperar-env .env; echo "salida=$?"` debe negarse con un mensaje
   que dice que el `.env` apunta a `IA_Nexus_Dev`, y dar `salida=2`.

5. **Nota para probar con un deploy de verdad:** `deploy.sh` sale sin hacer nada si no hay commits nuevos, así que apretar el botón de dev sin cambios no ejecuta el `POST_BUILD`. La primera corrida real
   será con el siguiente commit de Moisés a `dev` (la migración `0002`). Avísale cuando termines para que sepa que ya puede empujarlo.

## Qué NO hacer

- No edites ningún `.env` ni imprimas `~/.nexus-sql-ddl.env`.
- No corras `aplicar --confirmar` a mano contra `IA_Nexus_Staging` ni `IA_Nexus` (producción): el corredor tiene reglas de cadena (dev → staging → prod) y esas bases se tocan solo con su deploy, más adelante.
- No agregues el `POST_BUILD` a los `.conf` de staging ni de producción todavía.
- No hagas `pm2 delete` ni `pm2 start` de los backs.
- No toques los demás proyectos de webhook-central más allá de, si hace falta, el arreglo de «un `POST_BUILD` que falla aborta el deploy».

## Qué reportarle a Moisés al terminar

1. El cambio exacto que hiciste (el diff del `.conf` y, si lo hubo, el de `deploy.sh`).
2. Cómo queda el comportamiento si el `POST_BUILD` falla: cita las líneas de `deploy.sh` que lo hacen abortar.
3. La salida de las dos pruebas a mano del paso 4.

## Más adelante (NO ahora): staging y producción

Cada uno se activa cuando su rama ya trae la carpeta `migraciones/` (si no, un deploy de código viejo fallaría con «No module named migraciones»). Es decir: primero se promueve el código desde dev, y solo
entonces se agrega la línea. Moisés avisará.

```
staging   POST_BUILD="venv/bin/python -m migraciones aplicar --base IA_Nexus_Staging --confirmar --esperar-env .env"
prod      POST_BUILD="venv/bin/python -m migraciones aplicar --base IA_Nexus --confirmar --produccion --esperar-env .env"
```

Y antes de cada una, la línea base `0001` tiene que estar marcada en esa base (la marca Moisés desde su laptop).

## Cómo se comporta el corredor (para interpretar los logs)

- Salida `0`: todo bien (aplicó, o no había nada pendiente). Salida `2`: el corredor se negó por una regla y su mensaje dice cuál (empieza con `NO:`). Cualquier otra: un error de SQL Server; el detalle va en la salida.
- Cada migración corre en su propia transacción: si una falla, las anteriores quedan aplicadas y la que falló no deja nada a medias.
- Es seguro repetirlo: si no hay pendientes, no hace nada.
- Para ver sin cambiar nada: `python -m migraciones estado --base <base>`, o `aplicar` sin `--confirmar` (simulación que se deshace).
- Detalle completo: `README.md` del repo, sección «Migraciones de la base», y `migraciones/corredor.py` (encabezado).
