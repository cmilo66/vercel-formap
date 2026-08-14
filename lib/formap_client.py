"""
Cliente HTTP puro (sin navegador) para FORMAP (formap.co).

Hallazgo clave de la sesión de investigación: el bloqueo que se creía "anti-headless"
en realidad se dispara por la AUSENCIA de los headers Client Hints (`sec-ch-ua*`) que
un Chrome real manda automáticamente. Replicándolos exactamente, `requests` puro
funciona sin necesidad de Playwright/navegador para todo lo que NO sea el login
(el login sigue requiriendo resolver un reCAPTCHA v2 a mano, ver `sesion.py`).
"""
import re
import time
from html import unescape

import requests

BASE_URL = "https://formap.co"

# Headers copiados EXACTOS de una petición real de Chrome (capturados con Playwright
# contra el propio endpoint). No quitar ninguno — son justo los que distinguen una
# petición "de navegador real" de una de librería HTTP genérica para este servidor.
BROWSER_HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
    "sec-ch-ua": '"Chromium";v="151", "Not=A?Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "x-requested-with": "XMLHttpRequest",
    "accept": "text/html, */*; q=0.01",
    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
    "referer": f"{BASE_URL}/NO_Conformidad/Index",
}

# Catálogo fijo observado para el proyecto "Calidad de Obras SOL" (ProyectoId=81) /
# contratista FSCR INGENIERIA. Si el usuario opera otro proyecto/contratista hay que
# volver a mapearlos (ver catalogos.py).
TIPOEQ_TODOS = "643,644,645,646,647,648,649,650,651,652,653,654,655,656,657,658,659,660,688,689,662,664,887"
CONTRATISTA_FSCR = "112"


class FormapSessionExpirada(Exception):
    """La sesión guardada ya no es válida — hace falta un login asistido nuevo (captcha)."""


class FormapClient:
    def __init__(self, cookies: dict):
        """`cookies` es un dict simple {nombre: valor} — ver session_store.py para cómo
        se obtiene desde donde esté guardada la sesión (KV, DB, etc.)."""
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)
        for name, value in cookies.items():
            self.session.cookies.set(name, value, domain="formap.co")

    # ── Bajo nivel ────────────────────────────────────────────────────────────────
    def _post(self, path: str, data: dict) -> requests.Response:
        resp = self.session.post(f"{BASE_URL}{path}", data=data, timeout=20)
        self._verificar_sesion(resp)
        return resp

    def _get(self, path: str, params: dict | None = None) -> requests.Response:
        resp = self.session.get(f"{BASE_URL}{path}", params=params, timeout=20)
        self._verificar_sesion(resp)
        return resp

    def _verificar_sesion(self, resp: requests.Response):
        if "Account/Login" in resp.url or "txtLoginUserName" in resp.text[:2000]:
            raise FormapSessionExpirada("La sesión de FORMAP expiró o es inválida — hace falta relogin asistido.")

    # ── Catálogos (para armar filtros de búsqueda) ──────────────────────────────────
    def get_tipos_equipo(self) -> list[dict]:
        return self._post("/NO_Conformidad/GetTequipos", {}).json()

    def get_nivel1(self) -> list[dict]:
        """Departamentos."""
        return self._post("/NO_Conformidad/GetNivel1", {}).json()

    def get_nivel2(self, nivel1_id: str) -> list[dict]:
        """Municipios de un departamento."""
        return self._post("/NO_Conformidad/GetNivel2", {"nivel1": nivel1_id}).json()

    def get_nivel3(self, nivel1_id: str, nivel2_id: str) -> list[dict]:
        """Sectores de un municipio."""
        return self._post("/NO_Conformidad/GetNivel3", {"nivel1": nivel1_id, "nivel2": nivel2_id}).json()

    def get_contratistas(self) -> list[dict]:
        return self._post("/NO_Conformidad/GetContratistas", {}).json()

    def get_rutas(self, fecha_inicio: str, fecha_fin: str, tipoeq: str, nivel1: str, nivel2: str, nivel3: str, contratista: str) -> list[dict]:
        """fecha_inicio/fecha_fin en formato YYYY-MM-DD."""
        return self._post("/NO_Conformidad/GetFiltrarRutasFechas", {
            "FechaInicio": fecha_inicio, "FechaFin": fecha_fin,
            "Tipoequipos": tipoeq, "nivel1": nivel1, "nivel2": nivel2, "nivel3": nivel3,
            "Contratista": contratista,
        }).json()

    def obtener_ruta_ids_automatico(self, fecha_inicio: str, fecha_fin: str) -> str:
        """Recorre TODO el catálogo (departamento → municipio → sector → tipo de
        equipo) y devuelve los Ruta IDs correspondientes ya unidos por coma, para
        no depender de que el usuario del panel conozca esos IDs internos de
        FORMAP. fecha_inicio/fecha_fin en formato YYYY-MM-DD."""
        tipoeq_ids = ",".join(str(t["TipoEquipoId"]) for t in self.get_tipos_equipo())

        nivel1_ids = [str(d["UbicacionId"]) for d in self.get_nivel1()]
        nivel2_ids = []
        for n1 in nivel1_ids:
            nivel2_ids += [str(m["UbicacionId"]) for m in self.get_nivel2(n1)]
        nivel3_ids = []
        for n1 in nivel1_ids:
            for n2 in nivel2_ids:
                nivel3_ids += [str(s["UbicacionId"]) for s in self.get_nivel3(n1, n2)]

        rutas = self.get_rutas(
            fecha_inicio=fecha_inicio, fecha_fin=fecha_fin, tipoeq=tipoeq_ids,
            nivel1=",".join(nivel1_ids), nivel2=",".join(nivel2_ids), nivel3=",".join(nivel3_ids),
            contratista=CONTRATISTA_FSCR,
        )
        return ",".join(str(r["RutaId"]) for r in rutas)

    # ── Listado / búsqueda de NC (la operación principal) ───────────────────────────
    def buscar_nc(
        self, fecha_inicio: str, fecha_fin: str, ruta_ids: str,
        search_string: str = "", tipoeq: str = TIPOEQ_TODOS,
        contratista: str = CONTRATISTA_FSCR, cantidad: int = 100,
    ) -> str:
        """Devuelve el HTML crudo del listado (mismo que IndexPartial). Usar
        `parsear_nc()` para convertirlo a datos estructurados.

        fecha_inicio/fecha_fin en formato 'DD/MM/YYYY 0:00:00' (formato que usa
        este endpoint específico — distinto al de get_rutas). ruta_ids: string
        separado por comas.
        """
        resp = self._post("/NO_Conformidad/IndexPartial", {
            "FechaInicio": fecha_inicio, "FechaFin": fecha_fin,
            "RutaId": ruta_ids, "tipoeq": tipoeq,
            "SubEstate": "", "searchString": search_string, "sortOrder": "",
            "CantidadMostrar": str(cantidad), "Contratista": contratista,
        })
        return resp.text

    def buscar_por_equipo_ruta_id(self, equipo_ruta_id: str, fecha_inicio: str, fecha_fin: str, ruta_ids: str) -> list[dict]:
        """Atajo: busca directo por el ID que ya vive en `no_conformidades.equipo_ruta_id`
        (el número entre paréntesis en FORMAP) y devuelve los hallazgos ya parseados."""
        html = self.buscar_nc(fecha_inicio, fecha_fin, ruta_ids, search_string=equipo_ruta_id)
        return parsear_nc(html)

    def detalle_nc(self, nc_formap_id: str) -> str:
        """POST /NO_Conformidad/Details — el modal de detalle que usa el botón de lupa.
        Devuelve HTML crudo; útil si `buscar_nc` no trae algún dato que solo aparece
        en el detalle completo."""
        return self._post("/NO_Conformidad/Details", {"id": nc_formap_id}).text

    # ── Escritura — cerrar NC en FORMAP (EXPERIMENTAL, no verificado end-to-end) ────
    def marcar_resuelta(self, nc_formap_id: str) -> dict:
        """POST /NO_Conformidad/NcResuelta — encontrado en el JS del sitio
        (función `NcResuelta(id)`), pero NUNCA se ha probado en esta investigación.
        Su propio código fuente advierte: 'la NC ya debe haber sido conciliada' antes
        de poder marcarse resuelta — o sea, probablemente hay un paso previo
        (¿conciliación vía la 'Última Respuesta'?) que aún no está mapeado.

        NO llamar desde ningún flujo automático todavía. Cuando se decida probarlo,
        hacerlo primero contra una sola NC de prueba y verificar manualmente en FORMAP
        que el cambio fue el esperado antes de confiar en la respuesta del endpoint.
        """
        resp = self._post("/NO_Conformidad/NcResuelta", {"id": nc_formap_id})
        try:
            return {"ok": bool(resp.text.strip()), "raw": resp.text}
        except Exception:
            return {"ok": False, "raw": resp.text}


def _texto(el) -> str:
    return unescape(re.sub(r"<[^>]+>", " ", el or "")).strip()


def parsear_nc(html: str) -> list[dict]:
    """Parsea el HTML de IndexPartial a una lista de hallazgos estructurados.
    Reimplementación liviana (sin BeautifulSoup, para minimizar dependencias en
    Vercel) del parser validado en formap-bot/parse_formap_html.py — misma lógica,
    portada a regex sobre bloques <table id="table_report">...</table>."""
    resultados = {}
    tablas = re.findall(r'<table id="table_report".*?</table>', html, re.DOTALL)
    for tabla in tablas:
        trs = re.findall(r"<tr>.*?</tr>", tabla, re.DOTALL)
        if len(trs) < 2:
            continue
        header, body = trs[0], trs[1]

        m_id = re.search(r"NC #\s*(\d+)", header)
        if not m_id:
            continue
        nc_formap_id = m_id.group(1)

        m_eq = re.search(r"\((\d+)\)", header)
        equipo_ruta_id = m_eq.group(1) if m_eq else None

        m_estado = re.search(r"Estado NC:\s*</?b[^>]*>?\s*([^<]+)", header) or re.search(r"Estado NC:\s*([^<]+)", header)
        estado = _texto(m_estado.group(1)) if m_estado else None

        m_sub = re.search(r"Sub Estado NC:\s*([^<]+)", header)
        subestado = _texto(m_sub.group(1)) if m_sub else None

        campos = {}
        for k, v in re.findall(r"<b>([^<:]+):?\s*</b>\s*</span>\s*<span[^>]*>(.*?)</span>", body, re.DOTALL):
            campos[_texto(k)] = _texto(v)

        item_label = item_valor = None
        for k, v in campos.items():
            if k.startswith("Items -"):
                item_label, item_valor = k[len("Items -"):].strip(), v
                break

        obs_label = obs_valor = None
        m_obs = re.search(r"<b>Observaci[oó]n\s*-\s*([^<:]+):?\s*</b>\s*</span>\s*<span[^>]*><span>(.*?)</span></span>", body, re.DOTALL)
        if m_obs:
            obs_label, obs_valor = _texto(m_obs.group(1)), _texto(m_obs.group(2))

        evidencias = [
            {"url": url, "titulo": _texto(titulo)}
            for titulo, url in re.findall(r'title="([^"]*)" href="([^"]+)"[^>]*class=" default tooltip-default"', body)
        ]

        respuesta_comentario = None
        m_resp = re.search(r"<b>Comentario:\s*</b>\s*</span>\s*<span[^>]*>(.*?)</span>", body, re.DOTALL)
        if m_resp:
            respuesta_comentario = _texto(m_resp.group(1))

        resultados[nc_formap_id] = {
            "nc_formap_id": nc_formap_id,
            "equipo_ruta_id": equipo_ruta_id,
            "estado_formap": estado,
            "subestado_formap": subestado,
            "formulario": campos.get("Formulario"),
            "item_label": item_label,
            "item_valor": item_valor,
            "observacion_label": obs_label,
            "observacion_valor": obs_valor,
            "auditado_por": campos.get("Auditado Por"),
            "fecha_informe": campos.get("Fecha Informe"),
            "fecha_nc": campos.get("Fecha NC"),
            "evidencias": evidencias,
            "respuesta_comentario": respuesta_comentario,
        }
    return list(resultados.values())
