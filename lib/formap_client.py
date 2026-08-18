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

# Lista fija de Ruta IDs conocidos de FORMAP, capturados con navegador real en
# corridas previas. Los catálogos en vivo de FORMAP (GetTequipos, GetNivel1/2/3,
# GetFiltrarRutasFechas) dependen de JavaScript que corre al cargar la página en
# un navegador real y son intermitentes incluso ahí — por eso NO se recalculan
# en cada búsqueda, se usa esta lista fija como respaldo. IndexPartial igual
# filtra por FechaInicio/FechaFin, así que pasar rutas de más (de otro mes) no
# rompe nada, solo no aporta resultados para ese rango.
#
# Para agregar un mes nuevo: correr `python fetch_rutas.py <inicio> <fin> <SUFIJO>`
# en formap-bot/ (requiere storage_state.json vivo) — automatiza el flujo real del
# formulario y actualiza este archivo solo. No pedir el catálogo por HTTP directo:
# GetFiltrarRutasFechas devuelve vacío incluso con sesión de navegador real ya
# inicializada, solo se llena cuando el propio JS de la página lo dispara tras
# seleccionar los filtros en orden (ver fetch_rutas.py).
RUTA_IDS_JULIO_2026 = [
    "1526568", "1526571", "1526572", "1526573", "1526578", "1526579", "1526580",
    "1526583", "1526584", "1526585", "1526594", "1526595", "1526596", "1526597", "1531923",
]
RUTA_IDS_JUNIO_2026 = [
    "1521208", "1521209", "1521210", "1521211", "1521212", "1521213", "1521214",
    "1521216", "1521218", "1521219", "1521221", "1521222", "1521224",
    "1522407", "1522408", "1522409", "1522410",
]

RUTA_IDS_CONOCIDOS = RUTA_IDS_JULIO_2026 + RUTA_IDS_JUNIO_2026  # unión de todos los meses capturados hasta ahora


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
        """Devuelve la lista fija de Ruta IDs conocidos (ver RUTA_IDS_CONOCIDOS).
        Los catálogos en vivo de FORMAP (GetTequipos/GetNivel1/2/3/GetFiltrarRutasFechas)
        dependen de JavaScript de sesión que es intermitente incluso para un
        navegador real — no vale la pena recalcular esto en cada búsqueda.
        fecha_inicio/fecha_fin quedan como parámetros por compatibilidad con la
        firma anterior, no se usan para filtrar (IndexPartial ya filtra por fecha
        él solo con lo que se le pase)."""
        return ",".join(RUTA_IDS_CONOCIDOS)

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
        Devuelve HTML crudo del FORMULARIO de edición (Estado, Observaciones, adjuntar
        archivo) — es solo la cáscara del modal, NO trae el historial real de
        observaciones (esa tabla llega vacía, se llena aparte por JS). Para el
        historial real usar `historial_conciliacion()`."""
        return self._post("/NO_Conformidad/Details", {"id": nc_formap_id}).text

    def historial_conciliacion(self, nc_formap_id: str) -> list[dict]:
        """POST /NO_Conformidad/getTablaHistorialConciliacion — el historial real de
        observaciones/estados de una NC (lo que en el modal de FORMAP llena la tabla
        'Fecha Observación / Observación / Adjunto / Usuario / Estado' vía AJAX aparte,
        después de cargar Details). Devuelve la lista cruda de FORMAP (FechaSistema en
        formato /Date(ms)/, Comentarios, Adjunto, Adjunto2, NombreAdjunto,
        NombreAdjunto2, UserName, Estado, IdNC)."""
        resp = self._post("/NO_Conformidad/getTablaHistorialConciliacion", {"NoconformidadId": nc_formap_id})
        try:
            return resp.json()
        except ValueError:
            return []

    def detalle_completo(self, nc_formap_id: str) -> dict:
        """Combina Details (estado editable) + el historial real de observaciones,
        ya parseado a algo consumible directo por un frontend propio (sin necesidad
        de renderizar el HTML crudo de FORMAP)."""
        historial_crudo = self.historial_conciliacion(nc_formap_id)
        observaciones = []
        for item in historial_crudo:
            fecha_ms = re.search(r"/Date\((\d+)\)/", item.get("FechaSistema") or "")
            fecha_iso = None
            if fecha_ms:
                fecha_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(int(fecha_ms.group(1)) / 1000))
            observaciones.append({
                "fecha": fecha_iso,
                "comentario": item.get("Comentarios"),
                "usuario": item.get("UserName"),
                "estado": item.get("Estado"),
                "adjunto_url": f"https://formap.co/CarpetaHistorialConciliacion/{item['Adjunto']}" if item.get("Adjunto") else None,
                "adjunto_nombre": item.get("NombreAdjunto"),
                "adjunto2_url": f"https://formap.co/CarpetaHistorialConciliacion/{item['Adjunto2']}" if item.get("Adjunto2") else None,
                "adjunto2_nombre": item.get("NombreAdjunto2"),
            })
        return {"observaciones": observaciones}

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
        # OJO: antes esto solo miraba si el texto venía vacío (igual que el JS
        # del sitio: `if (data != '')`), sin revisar el status HTTP -- un 500 de
        # FORMAP con una página de error (texto no vacío) se habría reportado
        # como "cierre exitoso" siendo mentira. requests NO lanza excepción por
        # su cuenta en 4xx/5xx (no hay raise_for_status), así que hay que
        # comprobarlo explícitamente antes de confiar en el cuerpo.
        if resp.status_code != 200:
            return {"ok": False, "status_code": resp.status_code, "raw": resp.text}
        return {"ok": bool(resp.text.strip()), "status_code": resp.status_code, "raw": resp.text}


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
