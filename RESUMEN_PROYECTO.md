# Puente FORMAP ⇄ PIGO — Resumen del proyecto

## Objetivo final
Que tu sistema (nc_deploy / PIGO) pueda **traer NC de FORMAP automáticamente** (estado, evidencia de cierre) y, eventualmente, **disparar el cierre en FORMAP** desde un botón "Confirmar cierre" en tu propio sistema — sin que nadie tenga que entrar manualmente a FORMAP a revisar NC por NC.

## Qué existe hoy

### 1. Repo: `github.com/cmilo66/vercel-formap` → desplegado en Vercel (`vercel-formap.vercel.app`)
- **Backend**: FastAPI (Python) en `api/index.py`, cliente HTTP puro hacia FORMAP en `lib/formap_client.py` (sin navegador — se descubrió que FORMAP no bloquea HTTP puro, solo hay que mandar los headers `sec-ch-ua*` exactos de un Chrome real).
- **Frontend**: panel propio en `public/index.html` (tema industrial oscuro, con login, date picker a la medida, buscador y botón de cierre).
- **Autenticación de doble capa**:
  - `X-Service-Key` para llamadas servidor-a-servidor (tu futuro PHP).
  - Login humano (usuario `C.royero` / contraseña que ya conoces) contra `bd_respaldonc.panel_usuarios` — **nunca toca `bd_incidencias`**, la base viva de producción.
- **Bloqueo de contratista**: el servicio está forzado a `Contratista=FSCR (112)` siempre, no acepta que le pidan otro contratista desde afuera.
- **Rutas conocidas**: en vez de depender del catálogo en vivo de FORMAP (que resultó intermitente incluso para un navegador real), se usa una lista fija de Ruta IDs ya capturados (julio 2026 confirmado; junio pendiente de capturar los IDs numéricos).

### 2. Endpoints funcionando (probados hoy con `curl` directo)
- `POST /api/auth/login` — login del panel, devuelve token.
- `GET /api/formap/estado-sesion` — dice si hay sesión de FORMAP guardada.
- `GET /api/formap/rutas-automaticas` — devuelve la lista fija de rutas (ya no depende del catálogo roto).
- `POST /api/formap/buscar-por-ruta` — la búsqueda principal, por `equipo_ruta_id`.
- `GET /api/formap/detalle/{id}` — detalle de una NC.
- `POST /api/formap/cerrar` — **EXPERIMENTAL, nunca probado de verdad** (llama a `NcResuelta` de FORMAP, que según su propio JS exige que la NC "ya haya sido conciliada" antes — falta entender ese paso previo).

### 3. Variables de entorno ya puestas en Vercel
`SERVICE_KEY`, `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASS`, `FORMAP_SESSION_JSON`.

## El bloqueador REAL, encontrado el 15/08/2026 (no es sesión, no es código)

Se pasó gran parte de la sesión del 14-15/08/2026 asumiendo que las búsquedas en 0 eran por sesión muerta (choque de una sola sesión por cuenta entre el bot y el uso normal en Chrome — eso también pasa, ver abajo, pero **no era la causa de fondo**).

**Hallazgo confirmado con diagnóstico directo (dos pruebas independientes, con la cookie fresca ya verificada como correcta byte a byte contra lo guardado en Vercel):**

**El load balancer de FORMAP (AWS ELB) devuelve `404` con cuerpo vacío a TODA petición que llega desde las IPs de Vercel** — sin importar el endpoint (se probó tanto un GET simple a `/NO_Conformidad/Index` como el POST real de búsqueda `/NO_Conformidad/IndexPartial`), sin importar si la sesión/cookie es válida (la misma cookie, exactamente igual, funciona perfecto cuando se prueba desde una máquina local — trajo datos reales, NC #167018).

Esto es consistente con un bloqueo de infraestructura por origen (WAF o reglas del ELB que rechazan rangos de IP de proveedores cloud/serverless conocidos, como Vercel) — no con un problema de sesión, cookie, ni con el código del servicio.

### Por qué esto cambia el diagnóstico de hoy
Todo el trabajo de hoy (capturar julio, capturar junio, refrescar la cookie varias veces, pelear con el choque de sesión única) fue correcto y sigue siendo válido — pero ninguna búsqueda desde Vercel **puede** funcionar mientras FORMAP siga bloqueando su IP de origen, sin importar qué tan fresca esté la cookie.

### Solución real necesaria (nueva prioridad #1)
El servicio necesita salir a FORMAP desde una IP que FORMAP no bloquee. Opciones, de más a menos simple:
1. **Pedirle al administrador de FORMAP que permita (whitelist) la IP/rango de salida de Vercel** — dado que Camilo tiene acceso contractual/de auditoría a FORMAP, esta puede ser la vía más rápida. Requiere una IP de salida fija de Vercel (add-on de "Secure Compute"/IP estática — a confirmar disponibilidad en el plan Pro Trial actual).
2. **Correr la parte que llama a FORMAP desde algo que no esté bloqueado** (ej. la propia máquina/servidor de Camilo, un VPS con IP residencial/corporativa) y dejar en Vercel solo el frontend, que llamaría a ese backend en vez de a FORMAP directo.
3. Investigar si el bloqueo es específico de Vercel o de proveedores cloud en general (probar con otro hosting) — no confirmado aún.

### Bloqueador secundario, sigue existiendo aparte del anterior
**FORMAP permite una sola sesión activa por usuario** (mismo comportamiento que PIGO). Usar el mismo usuario (`aire_fscr_atln`) para el bot y para el uso normal en Chrome hace que cualquiera de los dos mate la sesión del otro — pasó varias veces en esta sesión de trabajo. Solución recomendada: usuario de FORMAP dedicado solo para el bot. Esto sigue siendo necesario incluso después de resolver el bloqueo de IP.

## Otros hallazgos técnicos importantes (para no repetir el mismo camino)

1. **El login de FORMAP exige resolver un reCAPTCHA v2 a mano** — no es automatizable. El flujo real es: alguien resuelve el captcha una vez (script `test_login.py` en `formap-bot/`), la sesión (cookies) se guarda y dura entre **12 y 24 horas**, después hay que repetirlo.
2. **Los catálogos de FORMAP** (`GetTequipos`, `GetNivel1/2/3`, `GetFiltrarRutasFechas`) no se pueden pedir directo por HTTP ni siquiera con navegador real y sesión ya inicializada (se probó explícitamente y devuelve vacío) — solo se llenan cuando el propio JS de la página los dispara tras seguir el flujo real del formulario (fechas → seleccionar todo en TipoEquipo/Depto/Municipio/Sector → Contratista → clic en "Buscar Rutas"). Por eso se automatizó ese flujo completo con Playwright en `formap-bot/fetch_rutas.py`: reproduce los clics como lo haría un humano y lee los `RutaId` numéricos reales (no los nombres visibles) desde los checkboxes ya poblados del desplegable "Rutas", y actualiza `formap-vercel/lib/formap_client.py` solo. Ya se capturaron así **julio 2026 (15 rutas)** y **junio 2026 (17 rutas)** — para meses nuevos, correr `python fetch_rutas.py <inicio> <fin> <SUFIJO>` (requiere `storage_state.json` con sesión viva, ver bloqueador de sesión única arriba).
3. **El bloqueo "anti-bot" que parecía haber al principio NO era un WAF genérico** — bloqueaba específicamente navegadores automatizados (Playwright headless), no clientes HTTP simples. Por eso terminamos usando `requests` puro con los headers correctos, mucho más simple y barato que Docker/Playwright en producción.
4. Hay un **trigger de base de datos** (`before_insert_historial`) en `bd_incidencias` que recalcula el encadenado de hash del historial de NC y tiene un bug de desempate cuando dos acciones caen en el mismo segundo exacto — no es exclusivo del bot, cualquier acción real del sistema puede toparse con eso. Vale la pena que alguien lo revise en algún momento (no urgente).

## Lo que ya se hizo en `bd_respaldonc` (staging, separado de producción)
- 106 NC de julio 2026 importadas y verificadas contra `bd_incidencias` (título/descripción idénticos donde ya existían).
- 235 fotos de evidencia enlazadas.
- 34 de las 36 NC de Andrea Anillo (junio, seguridad) **cerradas de verdad en producción** (`bd_incidencias`), con cadena de historial verificada, tras encontrar y corregir 2 bifurcaciones preexistentes en el historial que no tenían que ver con este trabajo.

## Próximos pasos (en orden sugerido, actualizado 15/08/2026)
1. **NUEVO, prioridad real #1**: resolver el bloqueo de IP de FORMAP contra Vercel (ver sección de arriba). Sin esto, nada de lo demás importa — ninguna búsqueda va a funcionar sin importar qué tan fresca esté la cookie. Camino más rápido: preguntar si FORMAP puede whitelistear la IP de salida de Vercel, o mover la parte que llama a FORMAP a algo que sí tenga IP permitida.
2. Una vez resuelto el bloqueo de IP: pasar cookie fresca a `FORMAP_SESSION_JSON` (ya se sabe hacer, varias veces hecho hoy) y confirmar que `38443232`/`38607927` aparecen.
3. **Pedir el usuario dedicado de FORMAP para el bot** — resuelve el choque de sesión única (bot vs. uso normal en Chrome), que sigue siendo un problema aparte del bloqueo de IP.
4. ~~Capturar los Ruta IDs de junio~~ — ya hecho (`formap-bot/fetch_rutas.py`, reusable para cualquier mes nuevo).
5. Probar `marcar_resuelta` / `/api/formap/cerrar` contra una sola NC de prueba, entender qué significa "conciliada" antes de resolver, y confirmar en FORMAP que hace lo esperado antes de usarlo en serio.
6. Construir el webhook del lado PHP (`api/formap-webhook.php` en `nc_deploy`) para el canal de aviso "de vuelta" (Vercel → tu sistema).
7. **Construir `formap-bot/refrescar_sesion.py`** — automatiza todo lo que hoy se hace a mano tras el login (extraer cookie, subirla a Vercel vía su API, refrescar rutas del mes, redeploy). Bloqueado por: falta el token de API de Vercel (`vercel.com/account/tokens` → "Create Token"). Nota: aunque se automatice, esto no sirve de nada mientras el punto 1 (bloqueo de IP) siga sin resolver.
