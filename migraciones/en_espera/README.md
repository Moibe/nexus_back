# Migraciones en espera

Aquí van las migraciones **ya escritas y probadas** que todavía no se pueden aplicar porque dependen de algo que hace el DBA. El corredor
solo lee los `.sql` de `migraciones/` (no entra a subcarpetas), así que lo que está aquí no lo aplica ningún deploy.

Cuando llegue lo que falta:

1. Mover el archivo a `migraciones/`. Si mientras tanto otra migración ya ocupó su número, se renumera a la siguiente libre (el número va
   en el nombre del archivo; el texto no lo repite en ningún lado que importe). Las versiones tienen que ser consecutivas.
2. Probar contra la base real sin confirmar: `python -m migraciones aplicar --base IA_Nexus_Dev` (corre y se deshace).
3. Commit y deploy de dev: el `POST_BUILD` la aplica. `/health/db` debe quedar al día.
4. No promoverla a staging ni a producción hasta que el DBA haya hecho lo mismo en esas bases: el deploy se detendría en su guarda.

## Lo que espera

| Archivo | Espera a | Prueba |
|---|---|---|
| `0016_consulta_por_organizacion.sql` | Que el DBA cree el esquema `[consulta]` (dueño `nexusOwner`), el usuario `usrNexusLectura` y le dé a `usrNexusDDL` `ALTER` sobre `[consulta]`, en `IA_Nexus_Dev` (`docs/solicitudes-dba.md` §8) | `verificar_consulta_por_organizacion.py` |
