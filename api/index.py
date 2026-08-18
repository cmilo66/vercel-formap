"""
Servicio Vercel — puente HTTP puro hacia FORMAP.

Todas las rutas /api/formap/* exigen el header `X-Service-Key` (para PHP) o
`Authorization: Bearer <token>` (para el panel humano, vía /api/auth/login,
autenticado contra bd_respaldonc.panel_usuarios) — este servicio nunca debe
quedar abierto al público, ya que reutiliza una sesión autenticada real de FORMAP.

Regla de oro: `/api/formap/cerrar` es la ÚNICA ruta de escritura, y solo la debe
llamar tu sistema (nc_deploy) cuando un humano confirma el cierre ahí. Este
servicio nunca decide cerrar nada por su cuenta.
"""
import hmac
import os
import sys
from functools import wraps

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from lib.formap_client import FormapClient, FormapSessionExpirada, TIPOEQ_TODOS, CONTRATISTA_FSCR, RUTA_IDS_CONOCIDOS, parsear_nc, descargar_archivo_externo  # noqa: E402
from lib.formap_bot import buscar_por_ruta_bot, FormapBotSesionExpirada  # noqa: E402
from lib import session_store, rutas_aprendidas  # noqa: E402
from lib import db, db_incidencias, db_incidencias_escritura, matching, auth  # noqa: E402

# /docs, /redoc y /openapi.json vienen expuestos públicamente por defecto en
# FastAPI — para un servicio interno que reutiliza una sesión real de FORMAP,
# eso es exposición innecesaria del mapa completo de la API. Se apagan salvo
# que DEBUG_DOCS=true esté puesto explícitamente en las env vars de Vercel.
_docs_habilitados = os.environ.get("DEBUG_DOCS") == "true"
app = FastAPI(
    docs_url="/docs" if _docs_habilitados else None,
    redoc_url="/redoc" if _docs_habilitados else None,
    openapi_url="/openapi.json" if _docs_habilitados else None,
)


def requiere_service_key(x_service_key: str = Header(default=None), authorization: str = Header(default=None)):
    """Acepta DOS formas de acceso, con la misma confianza:
    - X-Service-Key: para llamadas servidor-a-servidor (tu PHP en cPanel).
    - Authorization: Bearer <token>: para el panel humano, emitido por /api/auth/login.
    Nunca se expone el SERVICE_KEY real al navegador — el panel usa su propio
    usuario/contraseña y recibe un token firmado, no la clave de servicio."""
    esperado = os.environ.get("SERVICE_KEY")
    if esperado and x_service_key and hmac.compare_digest(x_service_key, esperado):
        return
    if authorization and authorization.startswith("Bearer "):
        token = authorization[len("Bearer "):]
        if auth.verificar_token(token):
            return
    raise HTTPException(status_code=401, detail="No autorizado.")


@app.post("/api/auth/login")
async def login(request: Request):
    """Login del panel humano contra bd_respaldonc.panel_usuarios (nunca toca
    bd_incidencias). Devuelve un token firmado, no la clave de servicio."""
    body = await request.json()
    usuario_input = (body.get("usuario") or "").strip()
    password_input = body.get("password") or ""
    if not usuario_input or not password_input:
        raise HTTPException(status_code=400, detail="Usuario y contraseña requeridos.")

    try:
        fila = db.obtener_usuario_panel(usuario_input)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error de conexión a la base: {e}")

    if not fila or not fila["activo"] or not auth.verificar_password(password_input, fila["password_hash"]):
        raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos.")

    return {"token": auth.generar_token(fila["usuario"])}


def _cliente() -> FormapClient:
    try:
        cookies = session_store.cargar_sesion()
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return FormapClient(cookies)


def _con_manejo_sesion(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except FormapSessionExpirada as e:
        raise HTTPException(status_code=409, detail={"error": str(e), "sesion_expirada": True})


# ── Salud / diagnóstico ──────────────────────────────────────────────────────────
@app.get("/api/formap/estado-sesion")
def estado_sesion(x_service_key: str = Header(default=None), authorization: str = Header(default=None)):
    requiere_service_key(x_service_key, authorization)
    edad = session_store.edad_sesion_segundos()
    try:
        session_store.cargar_sesion()
        tiene_sesion = True
    except RuntimeError:
        tiene_sesion = False
    return {
        "tiene_sesion": tiene_sesion,
        "edad_segundos": edad,
        "edad_horas": round(edad / 3600, 1) if edad else None,
    }


# ── Catálogos ─────────────────────────────────────────────────────────────────────
@app.get("/api/formap/catalogos/tipos-equipo")
def catalogo_tipos_equipo(x_service_key: str = Header(default=None), authorization: str = Header(default=None)):
    requiere_service_key(x_service_key, authorization)
    return _con_manejo_sesion(lambda: _cliente().get_tipos_equipo())


@app.get("/api/formap/catalogos/departamentos")
def catalogo_departamentos(x_service_key: str = Header(default=None), authorization: str = Header(default=None)):
    requiere_service_key(x_service_key, authorization)
    return _con_manejo_sesion(lambda: _cliente().get_nivel1())


@app.get("/api/formap/catalogos/municipios")
def catalogo_municipios(nivel1: str = "", x_service_key: str = Header(default=None), authorization: str = Header(default=None)):
    requiere_service_key(x_service_key, authorization)
    return _con_manejo_sesion(lambda: _cliente().get_nivel2(nivel1))


@app.get("/api/formap/catalogos/sectores")
def catalogo_sectores(nivel1: str = "", nivel2: str = "", x_service_key: str = Header(default=None), authorization: str = Header(default=None)):
    requiere_service_key(x_service_key, authorization)
    return _con_manejo_sesion(lambda: _cliente().get_nivel3(nivel1, nivel2))


@app.get("/api/formap/catalogos/contratistas")
def catalogo_contratistas(x_service_key: str = Header(default=None), authorization: str = Header(default=None)):
    requiere_service_key(x_service_key, authorization)
    return _con_manejo_sesion(lambda: _cliente().get_contratistas())


@app.get("/api/formap/catalogos/rutas")
def catalogo_rutas(
    fecha_inicio: str, fecha_fin: str, tipoeq: str = "", nivel1: str = "",
    nivel2: str = "", nivel3: str = "",
    x_service_key: str = Header(default=None), authorization: str = Header(default=None),
):
    # `contratista` NO es parámetro de entrada a propósito — este servicio es
    # exclusivamente para FSCR, nunca se acepta desde afuera para evitar traer
    # rutas de otros contratistas por error o por un valor manipulado.
    requiere_service_key(x_service_key, authorization)
    return _con_manejo_sesion(
        lambda: _cliente().get_rutas(
            fecha_inicio=fecha_inicio, fecha_fin=fecha_fin, tipoeq=tipoeq,
            nivel1=nivel1, nivel2=nivel2, nivel3=nivel3, contratista=CONTRATISTA_FSCR,
        )
    )


@app.get("/api/formap/rutas-automaticas")
def rutas_automaticas(fecha_inicio: str, fecha_fin: str, x_service_key: str = Header(default=None), authorization: str = Header(default=None)):
    """fecha_inicio/fecha_fin en formato YYYY-MM-DD. Recorre el catálogo completo
    y devuelve los Ruta IDs listos para pasar a /api/formap/buscar-por-ruta —
    así el panel nunca necesita que el usuario conozca esos IDs internos."""
    requiere_service_key(x_service_key, authorization)
    return _con_manejo_sesion(lambda: {"ruta_ids": _cliente().obtener_ruta_ids_automatico(fecha_inicio, fecha_fin)})


# ── Operación principal: traer NC de FORMAP ─────────────────────────────────────
@app.post("/api/formap/buscar")
async def buscar(request: Request, x_service_key: str = Header(default=None), authorization: str = Header(default=None)):
    """Body JSON: {fecha_inicio, fecha_fin, ruta_ids, search_string?, tipoeq?,
    cantidad?}. fecha_inicio/fin en 'DD/MM/YYYY 0:00:00'. No se acepta
    `contratista` desde el body — este servicio es exclusivo de FSCR, siempre
    se fuerza CONTRATISTA_FSCR sin importar qué mande el cliente.
    Devuelve la lista de hallazgos ya parseados (no el HTML crudo)."""
    requiere_service_key(x_service_key, authorization)
    body = await request.json()

    def _hacer():
        html = _cliente().buscar_nc(
            fecha_inicio=body["fecha_inicio"], fecha_fin=body["fecha_fin"],
            ruta_ids=body["ruta_ids"], search_string=body.get("search_string", ""),
            tipoeq=body.get("tipoeq") or TIPOEQ_TODOS,
            contratista=CONTRATISTA_FSCR,
            cantidad=body.get("cantidad", 100),
        )
        return {"hallazgos": parsear_nc(html)}

    return _con_manejo_sesion(_hacer)


@app.post("/api/formap/buscar-por-ruta")
async def buscar_por_ruta(request: Request, x_service_key: str = Header(default=None), authorization: str = Header(default=None)):
    """Atajo pensado para el uso principal: dado un equipo_ruta_id (el que ya
    vive en no_conformidades.equipo_ruta_id), lo busca directo en FORMAP.
    Body JSON: {equipo_ruta_id, fecha_inicio, fecha_fin, ruta_ids}."""
    requiere_service_key(x_service_key, authorization)
    body = await request.json()

    def _hacer():
        hallazgos = _cliente().buscar_por_equipo_ruta_id(
            equipo_ruta_id=body["equipo_ruta_id"],
            fecha_inicio=body["fecha_inicio"], fecha_fin=body["fecha_fin"],
            ruta_ids=body["ruta_ids"],
        )
        return {"hallazgos": hallazgos}

    return _con_manejo_sesion(_hacer)


@app.post("/api/formap/buscar-por-ruta-bot")
async def buscar_por_ruta_bot_endpoint(request: Request, x_service_key: str = Header(default=None), authorization: str = Header(default=None)):
    """Igual que buscar-por-ruta, pero SIN lista fija de rutas — abre un
    navegador real (Playwright) y sigue el flujo completo del formulario de
    FORMAP en el momento. Más lento (~15-20s) pero nunca queda desactualizado
    con rutas nuevas que FORMAP agregue. Body: {equipo_ruta_id, fecha_inicio,
    fecha_fin} con fechas en YYYY-MM-DD (formato de los date pickers).
    Playwright (sync) no puede correr dentro del event loop de asyncio, así
    que se despacha en un hilo aparte con run_in_threadpool."""
    requiere_service_key(x_service_key, authorization)
    body = await request.json()
    try:
        hallazgos = await run_in_threadpool(
            buscar_por_ruta_bot, body["equipo_ruta_id"], body["fecha_inicio"], body["fecha_fin"],
        )
        return {"hallazgos": hallazgos}
    except FormapBotSesionExpirada as e:
        raise HTTPException(status_code=409, detail={"error": str(e), "sesion_expirada": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error del bot: {e}")


@app.post("/api/formap/buscar-hibrido")
async def buscar_hibrido(request: Request, x_service_key: str = Header(default=None), authorization: str = Header(default=None)):
    """Punto de entrada principal del buscador del panel — patrón de fallback:

    1. Intenta el camino RÁPIDO (HTTP puro, ~1s) con RUTA_IDS_CONOCIDOS +
       lo que ya se haya aprendido antes (rutas_aprendidas.json).
    2. Si no encuentra nada, cae al BOT (Playwright, ~20-40s) — no depende de
       ninguna lista, sigue el flujo real del formulario.
    3. Si el bot SÍ encuentra la NC, extrae el RutaId interno real desde el
       nombre de archivo de la evidencia y lo agrega a rutas_aprendidas.json —
       la próxima vez que alguien busque esa misma ruta, el paso 1 ya la
       encuentra solo, sin volver a necesitar el bot.

    Body: {equipo_ruta_id, fecha_inicio, fecha_fin} con fechas en YYYY-MM-DD."""
    requiere_service_key(x_service_key, authorization)
    body = await request.json()
    equipo_ruta_id = body["equipo_ruta_id"]
    fecha_inicio_iso, fecha_fin_iso = body["fecha_inicio"], body["fecha_fin"]
    fecha_inicio_formap = _iso_a_formap(fecha_inicio_iso)
    fecha_fin_formap = _iso_a_formap(fecha_fin_iso)
    ruta_ids = ",".join(RUTA_IDS_CONOCIDOS + rutas_aprendidas.cargar())

    def _intento_rapido():
        return _cliente().buscar_por_equipo_ruta_id(
            equipo_ruta_id=equipo_ruta_id, fecha_inicio=fecha_inicio_formap,
            fecha_fin=fecha_fin_formap, ruta_ids=ruta_ids,
        )

    # OJO: si el camino rápido falla por sesión expirada (no solo "0
    # resultados"), esto también debe caer al bot -- por eso NO se usa
    # _con_manejo_sesion aquí (esa función relanza como HTTPException de una
    # vez, lo que se saltaría el fallback por completo).
    try:
        hallazgos = _intento_rapido()
        if hallazgos:
            return {"hallazgos": hallazgos, "via": "rapido"}
    except FormapSessionExpirada:
        hallazgos = []  # sesión rápida muerta -> el bot usa su propia sesión (storage_state.json)
    except Exception:
        hallazgos = []  # timeout/error de red contra FORMAP -> tampoco descarta el fallback

    try:
        hallazgos_bot = await run_in_threadpool(
            buscar_por_ruta_bot, equipo_ruta_id, fecha_inicio_iso, fecha_fin_iso,
        )
    except FormapBotSesionExpirada as e:
        raise HTTPException(status_code=409, detail={"error": str(e), "sesion_expirada": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"El camino rápido no encontró nada y el bot falló: {e}")

    ruta_aprendida = None
    for h in hallazgos_bot:
        ruta_interna = rutas_aprendidas.extraer_ruta_interna(h)
        if ruta_interna and rutas_aprendidas.agregar(ruta_interna):
            ruta_aprendida = ruta_interna
            break  # con una ruta nueva aprendida por búsqueda alcanza

    return {"hallazgos": hallazgos_bot, "via": "bot", "ruta_aprendida": ruta_aprendida}


# ── Integración de solo lectura con bd_incidencias (producción de nc_deploy) ────
@app.get("/api/incidencias/por-ruta")
def incidencias_por_ruta(equipo_ruta_id: str, x_service_key: str = Header(default=None), authorization: str = Header(default=None)):
    """Solo lectura contra bd_incidencias — nunca escribe. Dado un equipo_ruta_id
    de FORMAP, busca si ya existe esa NC en producción (nc_deploy) y trae su
    último comentario/estado, para que el panel sepa si hace falta 'actualizar
    cierre' en vez de tratarlo como una NC nueva."""
    requiere_service_key(x_service_key, authorization)
    try:
        return {"coincidencias": db_incidencias.buscar_por_equipo_ruta_id(equipo_ruta_id)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error consultando bd_incidencias: {e}")


@app.get("/api/incidencias/detalle/{nc_id}")
def incidencias_detalle(nc_id: str, x_service_key: str = Header(default=None), authorization: str = Header(default=None)):
    """Solo lectura contra bd_incidencias — cabecera + historial completo (el
    'Flujo de gestión') de una NC, para expandir su tarjeta en el panel sin
    salir a nc_deploy."""
    requiere_service_key(x_service_key, authorization)
    try:
        nc = db_incidencias.detalle_nc(nc_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error consultando bd_incidencias: {e}")
    if nc is None:
        raise HTTPException(status_code=404, detail="NC no encontrada.")
    return nc


@app.get("/api/incidencias/listado")
def incidencias_listado(fecha_inicio: str, fecha_fin: str, estado: str = "", x_service_key: str = Header(default=None), authorization: str = Header(default=None)):
    """Solo lectura contra bd_incidencias — tabla de estado (abiertas y cerradas)
    de las NC de FORMAP en un rango de fechas, para ver de un vistazo qué ya se
    cerró y qué no. estado='' trae todas; 'abierta'/'cerrada' filtra."""
    requiere_service_key(x_service_key, authorization)
    try:
        return {"nc": db_incidencias.listar_formap(fecha_inicio, fecha_fin, estado or None)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error consultando bd_incidencias: {e}")


# ── Cierre Periódico: escanea bd_incidencias + FORMAP, cierra SOLO con confirmación humana ──
def _iso_a_formap(fecha_iso: str) -> str:
    y, m, d = fecha_iso.split("-")
    return f"{d}/{m}/{y} 0:00:00"


@app.get("/api/cierre-periodico/tabla")
def cierre_periodico_tabla(fecha_inicio: str, fecha_fin: str, estado: str = "", x_service_key: str = Header(default=None), authorization: str = Header(default=None)):
    """Tabla unificada: para cada NC de bd_incidencias en el rango (todas, o
    filtradas por estado), busca su equipo_ruta_id en FORMAP y devuelve AMBOS
    lados uno junto al otro — el estado en bd_incidencias y lo que dice FORMAP —
    en una sola fila. `es_candidata=true` marca las que están abiertas/parciales
    en bd_incidencias pero YA tienen respuesta en FORMAP (listas para cerrar).
    Solo lectura + consultas a FORMAP — no escribe nada."""
    requiere_service_key(x_service_key, authorization)
    try:
        filas_bd = db_incidencias.listar_formap(fecha_inicio, fecha_fin, estado or None)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error leyendo bd_incidencias: {e}")

    if not filas_bd:
        return {"filas": []}

    fecha_ini_formap = _iso_a_formap(fecha_inicio)
    fecha_fin_formap = _iso_a_formap(fecha_fin)
    ruta_ids = ",".join(RUTA_IDS_CONOCIDOS + rutas_aprendidas.cargar())
    ESTADOS_PENDIENTES = {"abierta", "en_proceso", "parcialmente_cerrada"}

    def _hacer():
        cliente = _cliente()
        cache_por_ruta = {}
        filas = []
        for nc in filas_bd:
            ruta = nc["equipo_ruta_id"]
            hallazgos_ruta = []
            if ruta:
                if ruta not in cache_por_ruta:
                    cache_por_ruta[ruta] = cliente.buscar_por_equipo_ruta_id(ruta, fecha_ini_formap, fecha_fin_formap, ruta_ids)
                hallazgos_ruta = cache_por_ruta[ruta]

            match, score = matching.mejor_match(nc, hallazgos_ruta, umbral=0.3) if hallazgos_ruta else (None, 0.0)
            formap = None
            es_candidata = False
            if match:
                formap = {
                    "nc_formap_id": match["nc_formap_id"],
                    "estado_formap": match.get("estado_formap"),
                    "subestado_formap": match.get("subestado_formap"),
                    "comentario": match.get("respuesta_comentario"),
                    "evidencias": match.get("evidencias") or [],
                    "score": round(score, 2),
                }
                es_candidata = bool(match.get("respuesta_comentario")) and nc["estado_actual"] in ESTADOS_PENDIENTES

            filas.append({
                "nc_id": nc["id"], "titulo": nc["titulo"], "equipo_ruta_id": ruta,
                "estado_actual": nc["estado_actual"], "creado_en": nc["creado_en"], "actualizado_en": nc["actualizado_en"],
                "formap": formap, "es_candidata": es_candidata,
            })
        return {"filas": filas}

    return _con_manejo_sesion(_hacer)


@app.post("/api/cierre-periodico/cerrar")
async def cierre_periodico_cerrar(request: Request, x_service_key: str = Header(default=None), authorization: str = Header(default=None)):
    """ÚNICA ruta que escribe en bd_incidencias desde este servicio. Siempre
    disparada por confirmación humana explícita en el panel (una NC a la vez o
    un lote ya revisado en pantalla) — nunca se llama de forma automática.
    Body: {nc_id, nc_formap_id, equipo_ruta_id, formap_comentario, formap_evidencias}."""
    requiere_service_key(x_service_key, authorization)
    body = await request.json()
    try:
        return db_incidencias_escritura.cerrar_nc(
            nc_id=body["nc_id"], nc_formap_id=body["nc_formap_id"],
            equipo_ruta_id=body["equipo_ruta_id"], comentario_formap=body["formap_comentario"],
            evidencias=body.get("formap_evidencias") or [],
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error cerrando en bd_incidencias: {e}")


@app.get("/api/formap/detalle/{nc_formap_id}")
def detalle(nc_formap_id: str, x_service_key: str = Header(default=None), authorization: str = Header(default=None)):
    """Detalle estructurado (no HTML crudo de FORMAP) — el historial real de
    observaciones, listo para que el panel lo muestre en su propia ventana flotante."""
    requiere_service_key(x_service_key, authorization)
    return _con_manejo_sesion(lambda: _cliente().detalle_completo(nc_formap_id))


# ── Escritura — ÚNICO disparador válido, siempre iniciado por tu sistema ────────
@app.post("/api/formap/cerrar")
async def cerrar(request: Request, x_service_key: str = Header(default=None), authorization: str = Header(default=None)):
    """EXPERIMENTAL — nunca probado end-to-end contra FORMAP. Body:
    {nc_formap_id, comentario?, evidencias?}.

    `comentario`/`evidencias` (lista de {url, nombre}, hasta 2) los elige el
    HUMANO en el panel, revisando el historial completo de PIGO — a propósito
    NO se auto-deriva "el último comentario" acá: en varios casos reales ese
    campo es solo el mensaje genérico de aprobación de confirmadores ("Cierre
    automático..."), no la explicación técnica real de qué se corrigió. Quien
    llama decidió cuál paso del historial es el que de verdad concilia la NC.

    Si la NC YA tiene una observación en FORMAP (ya está conciliada — ver
    MAPEO_TRAZABILIDAD_FORMAP.md), llama directo a NcResuelta, sin subir nada,
    sin importar qué comentario/evidencias se hayan mandado.

    Si NO tiene ninguna observación todavía y no se mandó `comentario`, no
    inventa nada: devuelve ok=False con el motivo, sin tocar FORMAP."""
    requiere_service_key(x_service_key, authorization)
    body = await request.json()
    nc_formap_id = body["nc_formap_id"]
    comentario = body.get("comentario")
    evidencias = body.get("evidencias") or []

    def _hacer():
        cliente = _cliente()

        historial = cliente.historial_conciliacion(nc_formap_id)
        if not historial:
            if not comentario:
                return {"ok": False, "motivo": "sin_conciliar_sin_comentario_elegido"}

            archivos = []
            for ev in evidencias[:2]:
                descarga = descargar_archivo_externo(ev["url"])
                if descarga:
                    contenido, tipo_mime = descarga
                    archivos.append((ev.get("nombre") or "evidencia", contenido, tipo_mime))

            resultado_obs = cliente.agregar_observacion(nc_formap_id, observacion=comentario, archivos=archivos)
            if not resultado_obs.get("ok"):
                return {"ok": False, "motivo": "fallo_conciliacion", "detalle_conciliacion": resultado_obs}

        return cliente.marcar_resuelta(nc_formap_id)

    return _con_manejo_sesion(_hacer)
