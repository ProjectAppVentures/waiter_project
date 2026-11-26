# Entorno y pruebas

Versiones fijadas: Django 6.1, DRF 3.18.0 y pytest 9.1.1
(`experience/requirements.txt`); Next.js 16.3.3, React 19.2.8 y Playwright
1.62.1 (`frontend/package.json`, la app única). Los clientes exigen Node 24.20.x y npm 11.19.x.
Antes de usar APIs de Next se leen las guías incluidas en su paquete, como
exige `frontend/AGENTS.md`.

La base de referencia es MySQL 8.4, utf8mb4, modo estricto y READ COMMITTED.
`ExactCharField` conserva comparaciones binarias de claves; los índices
condicionales se expresan mediante columnas calculadas nullable
(`experience/tenancy/fields.py`, `experience/experience_project/settings.py`).
`DJANGO_ENV` vale `development` (por defecto), `staging` o `production`. Staging
es seguro por defecto: sin DEBUG, secreto real obligatorio, 127.0.0.1 siempre
admitido, correo a `experience/mail` y cookies Secure; `settings_dev` (el de
`manage.py`) no lo pisa. `DJANGO_DB_ENGINE` y las variables de DB
seleccionan el motor; el default SQLite no demuestra compatibilidad MySQL.

El clon principal es el checkout de servicio. Se trabaja en una rama de sesión
y un worktree; se entrega PR abierto con CI verde. Si el operador autoriza la
integración, merge-queue la realiza desde su propio worktree. No ejecutar
`manage.py migrate` desde el worktree ni enlazar una base viva para pruebas.

Para la ronda se usa MySQL aislado y nombres `test_waiter_qa_backend` y
`test_waiter_qa_ui`. `scripts/calidad/seed_ronda.py` exige desarrollo, loopback
y nombres de prueba; el runner de Django crea y migra la fixture. No lee
credenciales del servicio. `scripts/calidad/gate_ronda.py` presenta temporalmente
experience como `backend/` y el POS como `frontend/` al core canónico,
sin copiar tests ni cambiar reglas.

Los tests backend son pytest; los unitarios de los clientes, Jest; los recorridos,
Playwright. La configuración de rutas está en `.testquality.yml` y el CI de esta
ronda en `.github/workflows/validacion-ronda.yml`. Cada test documenta en español
«Falla si…», con una observación del error que detecta. Skips, capturas aisladas
o autoría sin ejecución no validan una mejora.

## Perfil QA de caja cerrada

Para un montaje expresamente autorizado, `scripts/calidad/seed_perfil_qa.py
prepare --config <ruta>` prepara la receta `caja-cerrada` v1. La misma
invocación devuelve `reutilizado` si conserva el corpus compatible. La variante
`verify --config <ruta>` verifica sólo con lecturas. La receta de la ronda de
CI de caja abierta conserva su propia entrada y su salida JSON.

La tabla define los campos del JSON privado del operador.

| Campos | Contrato |
|---|---|
| `version`, `profile`, `profile_version`, `environment` | `1`, `caja-cerrada`, `1` y `development`. |
| `mount_id`, `owner`, `checkout`, `sha`, `namespace` | ID del montaje, titular, ruta real y SHA del checkout limpio, y prefijo de los slugs de prueba. Montaje y namespace usan minúsculas ASCII, números y guiones. |
| `state_dir`, `credentials_ref` | Directorio privado existente y archivo JSON privado dentro de él. El archivo tiene `db_password` y `accounts`, por los localizadores `dueno`, `encargado`, `cajero`, `mesero`, `mesero.a2` y `mesero.b1`. |
| `db` | `host` igual a `127.0.0.1`, `port`, `source` igual a `waiter_qa_source`, `name` igual a `test_waiter_qa_<mount_id>` con guiones convertidos en guiones bajos, `user`, `container_id` completo y `volume`. El contenedor MySQL 8.4 y su volumen llevan la etiqueta `io.waiter.qa.mount` con el ID del montaje. La publicación de MySQL es exclusiva de loopback. |
| `origins` | `central` con host `qa-<dupla>.waiter.localhost` y puerto, `platform` y `menu` bajo el mismo origen base, `organizations` con las claves `<namespace>-a` y `<namespace>-b` y sus respectivos orígenes por subdominio, y `api` en loopback. Son coordenadas declaradas, pendientes de comprobar con el lanzador del montaje. |

`state_dir/perfil-datos.json` guarda la identidad efectiva de la base, la huella
del esquema, el inventario y los localizadores. El QR se resuelve desde la fila
referida en el manifiesto con acceso privado a esa base. Las claves, cookies y
tokens quedan fuera del manifiesto. Si aparece un marcador `preparando`, falta
la confirmación del corpus. El comando devuelve conflicto y preserva el montaje
para su diagnóstico. También rechaza bases existentes sin manifiesto y
discrepancias de pertenencia, versión o estado. Un acceso autorizado posterior
puede dejar sesión, asistencia y pase sin cambiar el estado de caja cerrada.
