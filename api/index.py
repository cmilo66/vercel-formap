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

from flask import Flask, jsonify, request

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from lib.formap_client import FormapClient, FormapSessionExpirada, TIPOEQ_TODOS, CONTRATISTA_FSCR, parsear_nc  # noqa: E402
from lib import session_store  # noqa: E402

app = Flask(__name__)


def requiere_service_key(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        esperado = os.environ.get("SERVICE_KEY")
        recibido = request.headers.get("X-Service-Key")
        if not esperado or recibido != esperado:
            return jsonify({"error": "No autorizado."}), 401
        return fn(*args, **kwargs)
    return wrapper


def _cliente() -> FormapClient:
    cookies = session_store.cargar_sesion()
    return FormapClient(cookies)


def _manejar_sesion_expirada(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except FormapSessionExpirada as e:
            return jsonify({"error": str(e), "sesion_expirada": True}), 409
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 500
    return wrapper


# ── Salud / diagnóstico ──────────────────────────────────────────────────────────
@app.route("/api/formap/estado-sesion", methods=["GET"])
@requiere_service_key
def estado_sesion():
    edad = session_store.edad_sesion_segundos()
    try:
        session_store.cargar_sesion()
        tiene_sesion = True
    except RuntimeError:
        tiene_sesion = False
    return jsonify({
        "tiene_sesion": tiene_sesion,
        "edad_segundos": edad,
        "edad_horas": round(edad / 3600, 1) if edad else None,
    })


# ── Catálogos ─────────────────────────────────────────────────────────────────────
@app.route("/api/formap/catalogos/tipos-equipo", methods=["GET"])
@requiere_service_key
@_manejar_sesion_expirada
def catalogo_tipos_equipo():
    return jsonify(_cliente().get_tipos_equipo())


@app.route("/api/formap/catalogos/departamentos", methods=["GET"])
@requiere_service_key
@_manejar_sesion_expirada
def catalogo_departamentos():
    return jsonify(_cliente().get_nivel1())


@app.route("/api/formap/catalogos/municipios", methods=["GET"])
@requiere_service_key
@_manejar_sesion_expirada
def catalogo_municipios():
    nivel1 = request.args.get("nivel1", "")
    return jsonify(_cliente().get_nivel2(nivel1))


@app.route("/api/formap/catalogos/sectores", methods=["GET"])
@requiere_service_key
@_manejar_sesion_expirada
def catalogo_sectores():
    nivel1 = request.args.get("nivel1", "")
    nivel2 = request.args.get("nivel2", "")
    return jsonify(_cliente().get_nivel3(nivel1, nivel2))


@app.route("/api/formap/catalogos/contratistas", methods=["GET"])
@requiere_service_key
@_manejar_sesion_expirada
def catalogo_contratistas():
    return jsonify(_cliente().get_contratistas())


@app.route("/api/formap/catalogos/rutas", methods=["GET"])
@requiere_service_key
@_manejar_sesion_expirada
def catalogo_rutas():
    p = request.args
    rutas = _cliente().get_rutas(
        fecha_inicio=p["fecha_inicio"], fecha_fin=p["fecha_fin"],
        tipoeq=p.get("tipoeq", ""), nivel1=p.get("nivel1", ""),
        nivel2=p.get("nivel2", ""), nivel3=p.get("nivel3", ""),
        contratista=p.get("contratista", ""),
    )
    return jsonify(rutas)


# ── Operación principal: traer NC de FORMAP ─────────────────────────────────────
@app.route("/api/formap/buscar", methods=["POST"])
@requiere_service_key
@_manejar_sesion_expirada
def buscar():
    """Body JSON: {fecha_inicio, fecha_fin, ruta_ids, search_string?, tipoeq?,
    contratista?, cantidad?}. fecha_inicio/fin en 'DD/MM/YYYY 0:00:00'.
    Devuelve la lista de hallazgos ya parseados (no el HTML crudo)."""
    body = request.get_json(force=True)
    html = _cliente().buscar_nc(
        fecha_inicio=body["fecha_inicio"], fecha_fin=body["fecha_fin"],
        ruta_ids=body["ruta_ids"], search_string=body.get("search_string", ""),
        tipoeq=body.get("tipoeq") or TIPOEQ_TODOS,
        contratista=body.get("contratista") or CONTRATISTA_FSCR,
        cantidad=body.get("cantidad", 100),
    )
    return jsonify({"hallazgos": parsear_nc(html)})


@app.route("/api/formap/buscar-por-ruta", methods=["POST"])
@requiere_service_key
@_manejar_sesion_expirada
def buscar_por_ruta():
    """Atajo pensado para el uso principal: dado un equipo_ruta_id (el que ya
    vive en no_conformidades.equipo_ruta_id), lo busca directo en FORMAP.
    Body JSON: {equipo_ruta_id, fecha_inicio, fecha_fin, ruta_ids}."""
    body = request.get_json(force=True)
    hallazgos = _cliente().buscar_por_equipo_ruta_id(
        equipo_ruta_id=body["equipo_ruta_id"],
        fecha_inicio=body["fecha_inicio"], fecha_fin=body["fecha_fin"],
        ruta_ids=body["ruta_ids"],
    )
    return jsonify({"hallazgos": hallazgos})


@app.route("/api/formap/detalle/<nc_formap_id>", methods=["GET"])
@requiere_service_key
@_manejar_sesion_expirada
def detalle(nc_formap_id):
    html = _cliente().detalle_nc(nc_formap_id)
    return jsonify({"html": html})


# ── Escritura — ÚNICO disparador válido, siempre iniciado por tu sistema ────────
@app.route("/api/formap/cerrar", methods=["POST"])
@requiere_service_key
@_manejar_sesion_expirada
def cerrar():
    """EXPERIMENTAL — ver advertencia en formap_client.marcar_resuelta().
    No integrar a ningún flujo automático sin antes probar manualmente contra
    una sola NC y confirmar en FORMAP que hizo lo esperado."""
    body = request.get_json(force=True)
    resultado = _cliente().marcar_resuelta(body["nc_formap_id"])
    return jsonify(resultado)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
