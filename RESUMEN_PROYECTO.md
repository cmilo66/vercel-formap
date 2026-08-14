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

## El bloqueador actual (no es un bug de código)

**FORMAP permite una sola sesión activa por usuario** (mismo comportamiento que tu propio PIGO). Hoy usamos tu mismo usuario (`aire_fscr_atln`) tanto para el bot como para tu uso normal — cada vez que tú entras a FORMAP en tu Chrome, mata la sesión que tenía el bot guardada, y viceversa.

**Esto se confirmó en vivo hoy**: la sesión del bot dejó de funcionar justo después de que entraste tú a revisar los filtros manualmente.

### Solución recomendada
Pedir un **usuario de FORMAP dedicado solo para el bot** (distinto al tuyo) — así nunca se pisan las sesiones. Es la única forma de que esto funcione de manera confiable sin coordinar manualmente quién está usando FORMAP en cada momento.

## Otros hallazgos técnicos importantes (para no repetir el mismo camino)

1. **El login de FORMAP exige resolver un reCAPTCHA v2 a mano** — no es automatizable. El flujo real es: alguien resuelve el captcha una vez (script `test_login.py` en `formap-bot/`), la sesión (cookies) se guarda y dura entre **12 y 24 horas**, después hay que repetirlo.
2. **Los catálogos de FORMAP** (`GetTequipos`, `GetNivel1/2/3`, `GetFiltrarRutasFechas`) dependen de JavaScript de inicialización de sesión y son intermitentes incluso para un navegador real — no vale la pena automatizarlos, mejor mantener listas fijas de IDs conocidos y ampliarlas a mano cuando haga falta un mes nuevo.
3. **El bloqueo "anti-bot" que parecía haber al principio NO era un WAF genérico** — bloqueaba específicamente navegadores automatizados (Playwright headless), no clientes HTTP simples. Por eso terminamos usando `requests` puro con los headers correctos, mucho más simple y barato que Docker/Playwright en producción.
4. Hay un **trigger de base de datos** (`before_insert_historial`) en `bd_incidencias` que recalcula el encadenado de hash del historial de NC y tiene un bug de desempate cuando dos acciones caen en el mismo segundo exacto — no es exclusivo del bot, cualquier acción real del sistema puede toparse con eso. Vale la pena que alguien lo revise en algún momento (no urgente).

## Lo que ya se hizo en `bd_respaldonc` (staging, separado de producción)
- 106 NC de julio 2026 importadas y verificadas contra `bd_incidencias` (título/descripción idénticos donde ya existían).
- 235 fotos de evidencia enlazadas.
- 34 de las 36 NC de Andrea Anillo (junio, seguridad) **cerradas de verdad en producción** (`bd_incidencias`), con cadena de historial verificada, tras encontrar y corregir 2 bifurcaciones preexistentes en el historial que no tenían que ver con este trabajo.

## Próximos pasos (en orden sugerido)
1. **Pedir el usuario dedicado de FORMAP para el bot** — desbloquea todo lo demás.
2. Con sesión estable, capturar los Ruta IDs de junio (y de ahí en adelante, cada mes nuevo) para que el buscador cubra más que julio.
3. Probar `marcar_resuelta` / `/api/formap/cerrar` contra una sola NC de prueba, entender qué significa "conciliada" antes de resolver, y confirmar en FORMAP que hace lo esperado antes de usarlo en serio.
4. Construir el webhook del lado PHP (`api/formap-webhook.php` en `nc_deploy`) para el canal de aviso "de vuelta" (Vercel → tu sistema).
5. Resolver el refresco de sesión sin copiar/pegar a mano (un script que suba la cookie nueva directo a Vercel vía su API, para que solo haga falta resolver el captcha).
