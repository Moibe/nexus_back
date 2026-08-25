# Esquema del procesador de INE en Document AI

Copia versionada del esquema del procesador `bf35151f51b51521`, tomada de la
consola de Google el **2026-08-25**. Se guarda aquí por dos razones: la consola
es el único otro lugar donde vive, y este esquema es el insumo directo de
`field_definition` y `extractor_field_map` (secciones 2.6/2.7 de
[`solicitudes-dba.md`](solicitudes-dba.md)) cuando toque esa plática con el DBA.

Contrastado contra una extracción real de producción, no solo transcrito.

**Actualización 2026-08-25 (mismo día, más tarde):** `Pais` y `edad` fueron
**inhabilitados** en la consola. `Pais` porque su valor era constante (toda INE
es de México — información cero) y su confianza errática (66% en algunas
corridas, probablemente por nunca haber sido etiquetado) arrastraba
`confianza_minima` hacia abajo y habría ruteado documentos perfectos a revisión
humana. `edad` porque no está impresa en la credencial — era una inferencia del
modelo — y además caduca: se deriva de `fecha_nacimiento` al momento de leer.
Verificado en producción tras el cambio: 19 campos, `confianza_minima` subió de
99.62 a 99.96, y el peor campo ahora es `apellido_materno` (99.96) — el semáforo
mide calidad de lectura real, ya no ruido. Las filas de ambos siguen en la tabla
de abajo como registro de por qué se quitaron.

## El esquema tal como está declarado

| Campo | Tipo declarado | Caso | Valor real medido |
|---|---|---|---|
| `apellido_materno` | Texto sin formato | Obligatoria una vez | `VALENZUELA` |
| `apellido_paterno` | Texto sin formato | Obligatoria una vez | `ROMAN` |
| `clave_elector` | Texto sin formato | Obligatoria una vez | `RMVLXC97050826M700` |
| `curp` | Texto sin formato | Obligatoria una vez | `ROVX970508MSRMLC08` |
| `domicilio` | *(compuesto)* | Obligatoria una vez | — |
| `domicilio.calle_numero` | Texto sin formato | Obligatoria una vez | `AV TECNOLOGICO 32` |
| `domicilio.codigo_postal` | **Número** | Obligatoria una vez | `85240` |
| `domicilio.colonia` | Texto sin formato | Obligatoria una vez | `LOC LA UNION` |
| `domicilio.estado` | Texto sin formato | Obligatoria una vez | `SON.` → `SON` |
| `domicilio.localidad` | **Dirección** | Obligatoria una vez | `HUATABAMPO,` → `HUATABAMPO` |
| `edad` | Número | Opcional una vez | *(no llegó)* |
| `emision` | **Fecha y hora** | Opcional una vez | `2015` |
| `estado` | **Número** | Opcional una vez | `26` |
| `fecha_nacimiento` | Fecha y hora | Obligatoria una vez | `08/05/1997` → `1997-05-08` |
| `fecha_registro` | **Número** | Obligatoria una vez | `2015 00` |
| `folio` | Número | Opcional una vez | *(no llegó)* |
| `localidad` | **Número** | Opcional una vez | `0181` |
| `municipio` | **Número** | Opcional una vez | `064` |
| `nombre` | Texto sin formato | Obligatoria una vez | `XOCHITL GUADALUPE` |
| `Pais` | Texto sin formato | Obligatoria una vez | `MÉXICO` |
| `seccion` | Texto sin formato | Obligatoria una vez | `1212` |
| `sexo` | Texto sin formato | Obligatoria una vez | `M` |
| `vigencia` | **Fecha y hora** | Obligatoria una vez | `2025` |

22 campos hoja (17 en la raíz + 5 bajo `domicilio`). En la corrida medida
llegaron 20; los dos ausentes son los dos `Opcional`, así que cuadra.

---

## 1. `Pais` es el único campo fuera de convención

Todos los demás son `snake_case` en minúsculas. `Pais` trae **P mayúscula** (y
sin acento). No es cosmético:

- `extractor_field_map.source_path` va a guardar la cadena **literal** `Pais`.
  Cualquier comparación es sensible a mayúsculas.
- Por la regla de inmutabilidad del diccionario (2.1), renombrarlo **después** de
  que exista una `config_version` activa obliga a crear una versión nueva y
  arrastrar el mapeo. Antes de eso, es gratis.

**Conviene renombrarlo a `pais` ahora**, mientras no hay nada que versionar. Si
se decide dejarlo, entonces hay que dejarlo *a propósito* y que nadie lo
"arregle" luego sin entender el costo.

## 2. Las dos colisiones de nombre quedaron confirmadas a nivel de tipo

Esto refuerza la sección 2.6 con evidencia más fuerte de la que teníamos:

| Nombre | En la raíz | Dentro de `domicilio` |
|---|---|---|
| `estado` | **Número** — `26` (clave del padrón) | **Texto** — `SON` (texto del domicilio) |
| `localidad` | **Número** — `0181` (clave del padrón) | **Dirección** — `HUATABAMPO` |

Cuando escribí que la llave del mapeo tiene que ser una **ruta** y no un nombre,
el argumento era que los valores diferían. El esquema muestra algo más contundente:
**están declarados con tipos distintos**. No son el mismo dato en dos lugares —
son dos campos diferentes que comparten nombre. Un mapeo por nombre simple no
solo perdería un valor, mezclaría dos tipos.

## 3. Tres tipos declarados que no corresponden al dato

Ninguno rompe nada **hoy**, y vale entender por qué: `_valor_normalizado()` en
`servicios/ia.py` solo lee el `normalizedValue` de Google **cuando es una fecha
completa** (año, mes y día presentes); para todo lo demás devuelve el
`mentionText` crudo. Esa decisión es la que nos está salvando, y no se nota.

**a) Identificadores declarados como Número → riesgo de perder ceros.**
`localidad = 0181`, `municipio = 064`, `codigo_postal = 85240`. Son claves, no
cantidades. Si algún día alguien "mejora" esa función para confiar en la
normalización numérica de Google, `0181` se vuelve `181` y `064` se vuelve `64`:
claves del padrón electoral corrompidas **en silencio**, sin error ni alerta.
Un CP de la CDMX (`01000`) tiene el mismo problema.

**b) Años declarados como Fecha y hora.** `emision = 2015` y `vigencia = 2025`
son solo años; en la credencial no hay mes ni día. La condición
`if anio and mes and dia` es la que evita inventar `2015-01-01` — una fecha que
no está impresa en el documento. Si esa condición se relajara, la app estaría
guardando datos que nadie leyó.

**c) `fecha_registro` declarado como Número pero su valor es `"2015 00"`.**
No es un número ni una fecha; es el año seguido de otro campo pegado. Aquí el
problema no es nuestro código sino el esquema: vale revisar en la consola si
debería ser texto, o si son dos campos que el procesador está juntando.

## 4. `edad` no debería ser un campo almacenado

Se deriva de `fecha_nacimiento` y **caduca**: guardarla significa que en un año
es incorrecta. Hoy no llegó (es opcional) así que no hay impacto, pero si algún
día llega y se persiste como `entity_fact`, sería un dato que envejece mal
dentro de una tabla que el diccionario define como append-only. Mejor calcularla
al leer.

## 5. La columna "Caso" es exactamente `field_definition.required`

`Obligatoria una vez` / `Opcional una vez` es el mismo concepto que
`field_definition.required` del diccionario. Es decir: **esta pantalla es el
documento fuente** para poblar `field_definition` y `extractor_field_map` cuando
se llegue al paso 4 con el DBA.

Y deja ver un hueco que ya estaba anotado como diferido: si un campo
**Obligatorio** no llega, hoy la API simplemente lo omite y nadie se entera. El
diccionario pide un `entity_fact` con `null_reason` para ese caso. No se puede
implementar sin `field_definition` (hay que saber qué se esperaba), pero este
esquema es justo la lista de lo que se espera.

---

## Qué conviene probar por esto

Dos comportamientos de los que dependemos y que hoy ningún test protege:

1. Un campo con ceros a la izquierda (`0181`) conserva los ceros en
   `value_normalized`.
2. Un campo de tipo fecha con **solo el año** (`2015`) NO se convierte en una
   fecha completa inventada.

Los dos están garantizados por una sola condición de tres líneas. Si alguien la
toca sin este contexto, la falla es silenciosa y solo se ve al auditar datos
guardados.

**Ya quedaron cubiertos** en [`verificar_normalizacion.py`](../verificar_normalizacion.py)
(11 casos, todos pasando al 2026-08-25). Se corre a mano:

```bash
venv/bin/python verificar_normalizacion.py
```

No usa pytest a propósito — el proyecto no tiene framework de pruebas y esto no
justifica agregar una dependencia. Si algún día se monta pytest, estos casos se
mudan tal cual.
