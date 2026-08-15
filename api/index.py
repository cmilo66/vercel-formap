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

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from lib.formap_client import FormapClient, FormapSessionExpirada, TIPOEQ_TODOS, CONTRATISTA_FSCR, RUTA_IDS_CONOCIDOS, parsear_nc  # noqa: E402
from lib import session_store  # noqa: E402
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


@app.get("/api/cierre-periodico/escanear")
def cierre_periodico_escanear(fecha_inicio: str, fecha_fin: str, x_service_key: str = Header(default=None), authorization: str = Header(default=None)):
    """Solo lectura + consultas a FORMAP — NO escribe nada en ninguna base.
    fecha_inicio/fecha_fin en YYYY-MM-DD. Para cada NC abierta de FORMAP en
    bd_incidencias dentro del rango, busca en FORMAP por su equipo_ruta_id y
    propone el mejor match entre los hallazgos que YA tienen respuesta/cierre
    allá — el panel muestra la lista para que un humano decida qué cerrar."""
    requiere_service_key(x_service_key, authorization)
    try:
        candidatas = db_incidencias.listar_abiertas_formap(fecha_inicio, fecha_fin)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error leyendo bd_incidencias: {e}")

    if not candidatas:
        return {"candidatos": [], "total_revisadas": 0}

    fecha_ini_formap = _iso_a_formap(fecha_inicio)
    fecha_fin_formap = _iso_a_formap(fecha_fin)
    ruta_ids = ",".join(RUTA_IDS_CONOCIDOS)

    def _hacer():
        cliente = _cliente()
        cache_por_ruta = {}
        candidatos = []
        for nc in candidatas:
            ruta = nc["equipo_ruta_id"]
            if ruta not in cache_por_ruta:
                hallazgos = cliente.buscar_por_equipo_ruta_id(ruta, fecha_ini_formap, fecha_fin_formap, ruta_ids)
                cache_por_ruta[ruta] = [h for h in hallazgos if h.get("respuesta_comentario")]
            candidatos_formap = cache_por_ruta[ruta]
            if not candidatos_formap:
                continue
            match, score = matching.mejor_match(nc, candidatos_formap)
            if match:
                candidatos.append({
                    "nc_id": nc["id"], "nc_titulo": nc["titulo"], "equipo_ruta_id": ruta,
                    "nc_formap_id": match["nc_formap_id"],
                    "formap_comentario": match["respuesta_comentario"],
                    "formap_evidencias": match["evidencias"],
                    "score_match": round(score, 2),
                })
        return {"candidatos": candidatos, "total_revisadas": len(candidatas)}

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
    """EXPERIMENTAL — ver advertencia en formap_client.marcar_resuelta().
    No integrar a ningún flujo automático sin antes probar manualmente contra
    una sola NC y confirmar en FORMAP que hizo lo esperado."""
    requiere_service_key(x_service_key, authorization)
    body = await request.json()
    return _con_manejo_sesion(lambda: _cliente().marcar_resuelta(body["nc_formap_id"]))
