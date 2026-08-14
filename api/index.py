"""
Servicio Vercel — puente HTTP puro hacia FORMAP.

Todas las rutas /api/formap/* exigen el header `X-Service-Key` (comparado contra
la env var SERVICE_KEY) — este servicio nunca debe quedar abierto al público, ya
que reutiliza una sesión autenticada real de FORMAP.

Regla de oro: `/api/formap/cerrar` es la ÚNICA ruta de escritura, y solo la debe
llamar tu sistema (nc_deploy) cuando un humano confirma el cierre ahí. Este
servicio nunca decide cerrar nada por su cuenta.
"""
import os
import sys
from functools import wraps

from fastapi import FastAPI, Header, HTTPException, Request

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from lib.formap_client import FormapClient, FormapSessionExpirada, TIPOEQ_TODOS, CONTRATISTA_FSCR, parsear_nc  # noqa: E402
from lib import session_store  # noqa: E402

app = FastAPI()


def requiere_service_key(x_service_key: str = Header(default=None)):
    esperado = os.environ.get("SERVICE_KEY")
    if not esperado or x_service_key != esperado:
        raise HTTPException(status_code=401, detail="No autorizado.")


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
def estado_sesion(x_service_key: str = Header(default=None)):
    requiere_service_key(x_service_key)
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
def catalogo_tipos_equipo(x_service_key: str = Header(default=None)):
    requiere_service_key(x_service_key)
    return _con_manejo_sesion(lambda: _cliente().get_tipos_equipo())


@app.get("/api/formap/catalogos/departamentos")
def catalogo_departamentos(x_service_key: str = Header(default=None)):
    requiere_service_key(x_service_key)
    return _con_manejo_sesion(lambda: _cliente().get_nivel1())


@app.get("/api/formap/catalogos/municipios")
def catalogo_municipios(nivel1: str = "", x_service_key: str = Header(default=None)):
    requiere_service_key(x_service_key)
    return _con_manejo_sesion(lambda: _cliente().get_nivel2(nivel1))


@app.get("/api/formap/catalogos/sectores")
def catalogo_sectores(nivel1: str = "", nivel2: str = "", x_service_key: str = Header(default=None)):
    requiere_service_key(x_service_key)
    return _con_manejo_sesion(lambda: _cliente().get_nivel3(nivel1, nivel2))


@app.get("/api/formap/catalogos/contratistas")
def catalogo_contratistas(x_service_key: str = Header(default=None)):
    requiere_service_key(x_service_key)
    return _con_manejo_sesion(lambda: _cliente().get_contratistas())


@app.get("/api/formap/catalogos/rutas")
def catalogo_rutas(
    fecha_inicio: str, fecha_fin: str, tipoeq: str = "", nivel1: str = "",
    nivel2: str = "", nivel3: str = "", contratista: str = "",
    x_service_key: str = Header(default=None),
):
    requiere_service_key(x_service_key)
    return _con_manejo_sesion(
        lambda: _cliente().get_rutas(
            fecha_inicio=fecha_inicio, fecha_fin=fecha_fin, tipoeq=tipoeq,
            nivel1=nivel1, nivel2=nivel2, nivel3=nivel3, contratista=contratista,
        )
    )


# ── Operación principal: traer NC de FORMAP ─────────────────────────────────────
@app.post("/api/formap/buscar")
async def buscar(request: Request, x_service_key: str = Header(default=None)):
    """Body JSON: {fecha_inicio, fecha_fin, ruta_ids, search_string?, tipoeq?,
    contratista?, cantidad?}. fecha_inicio/fin en 'DD/MM/YYYY 0:00:00'.
    Devuelve la lista de hallazgos ya parseados (no el HTML crudo)."""
    requiere_service_key(x_service_key)
    body = await request.json()

    def _hacer():
        html = _cliente().buscar_nc(
            fecha_inicio=body["fecha_inicio"], fecha_fin=body["fecha_fin"],
            ruta_ids=body["ruta_ids"], search_string=body.get("search_string", ""),
            tipoeq=body.get("tipoeq") or TIPOEQ_TODOS,
            contratista=body.get("contratista") or CONTRATISTA_FSCR,
            cantidad=body.get("cantidad", 100),
        )
        return {"hallazgos": parsear_nc(html)}

    return _con_manejo_sesion(_hacer)


@app.post("/api/formap/buscar-por-ruta")
async def buscar_por_ruta(request: Request, x_service_key: str = Header(default=None)):
    """Atajo pensado para el uso principal: dado un equipo_ruta_id (el que ya
    vive en no_conformidades.equipo_ruta_id), lo busca directo en FORMAP.
    Body JSON: {equipo_ruta_id, fecha_inicio, fecha_fin, ruta_ids}."""
    requiere_service_key(x_service_key)
    body = await request.json()

    def _hacer():
        hallazgos = _cliente().buscar_por_equipo_ruta_id(
            equipo_ruta_id=body["equipo_ruta_id"],
            fecha_inicio=body["fecha_inicio"], fecha_fin=body["fecha_fin"],
            ruta_ids=body["ruta_ids"],
        )
        return {"hallazgos": hallazgos}

    return _con_manejo_sesion(_hacer)


@app.get("/api/formap/detalle/{nc_formap_id}")
def detalle(nc_formap_id: str, x_service_key: str = Header(default=None)):
    requiere_service_key(x_service_key)
    return _con_manejo_sesion(lambda: {"html": _cliente().detalle_nc(nc_formap_id)})


# ── Escritura — ÚNICO disparador válido, siempre iniciado por tu sistema ────────
@app.post("/api/formap/cerrar")
async def cerrar(request: Request, x_service_key: str = Header(default=None)):
    """EXPERIMENTAL — ver advertencia en formap_client.marcar_resuelta().
    No integrar a ningún flujo automático sin antes probar manualmente contra
    una sola NC y confirmar en FORMAP que hizo lo esperado."""
    requiere_service_key(x_service_key)
    body = await request.json()
    return _con_manejo_sesion(lambda: _cliente().marcar_resuelta(body["nc_formap_id"]))
