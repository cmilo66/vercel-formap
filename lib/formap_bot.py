"""
Búsqueda de NC en FORMAP vía bot real (Playwright) — SIN depender de una lista
fija de Ruta IDs.

Por qué existe: GetFiltrarRutasFechas (el catálogo de rutas) nunca responde por
HTTP puro, ni siquiera con sesión ya inicializada — solo se llena cuando el
propio JS de la página lo dispara tras seguir el flujo real del formulario
(fechas → seleccionar todo en TipoEquipo/Depto/Municipio/Sector → Contratista →
"Buscar Rutas"). Mantener una lista fija de rutas (RUTA_IDS_CONOCIDOS en
formap_client.py) se vuelve obsoleta cada vez que FORMAP agrega un lote nuevo
(confirmado 2026-08-18: una NC real de julio no aparecía porque su ruta nunca
se había capturado a mano). Este módulo reproduce el flujo completo cada vez
que se busca, así nunca hace falta mantener esa lista.

Contrapartida: abre un navegador real (Playwright, visible) y tarda ~15-20s por
búsqueda, en vez de ~1s del cliente HTTP puro (formap_client.py). Además compite
por la sesión única de FORMAP si el usuario también la está usando en su propio
Chrome en ese momento (mismo choque de siempre, documentado en RESUMEN_PROYECTO.md).
"""
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

from lib.formap_client import parsear_nc

STORAGE_STATE = Path(__file__).parent.parent.parent / "formap-bot" / "storage_state.json"
NC_INDEX_URL = "https://formap.co/NO_Conformidad/Index"
CONTRATISTA_LABEL = "FSCR"

NEUTRALIZE_CSS = """
#modalNotificacionesDeshabilitadas, .modal-backdrop { display:none !important; pointer-events:none !important; }
body.modal-open { overflow: auto !important; }
"""


class FormapBotSesionExpirada(Exception):
    """La sesión guardada (storage_state.json) ya no es válida — hace falta
    correr test_login.py de nuevo (login asistido con captcha)."""


def _select_all(page, ms_list_id):
    wrap = page.locator(f"#{ms_list_id}")
    wrap.locator("button").first.click()
    page.wait_for_timeout(400)
    link = wrap.locator("a.ms-selectall")
    if link.count() > 0 and "seleccionar" in (link.first.inner_text() or "").lower():
        link.first.click()
    page.wait_for_timeout(400)
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)


def _select_one(page, ms_list_id, text_contains):
    wrap = page.locator(f"#{ms_list_id}")
    wrap.locator("button").first.click()
    page.wait_for_timeout(400)
    option = wrap.locator("li", has_text=text_contains)
    if option.count() > 0:
        option.first.locator("label").first.click()
    page.wait_for_timeout(400)
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)


def buscar_por_ruta_bot(equipo_ruta_id: str, fecha_inicio: str, fecha_fin: str) -> list[dict]:
    """fecha_inicio/fecha_fin en formato YYYY-MM-DD (el que usan los date pickers
    del panel). Abre un navegador real, sigue el flujo completo del formulario
    de FORMAP (sin lista fija de rutas), escribe equipo_ruta_id en el buscador
    nativo, y devuelve los hallazgos ya parseados con parsear_nc()."""
    if not STORAGE_STATE.exists():
        raise FormapBotSesionExpirada(
            f"No existe {STORAGE_STATE} — correr test_login.py en formap-bot/ primero."
        )

    y1, m1, d1 = fecha_inicio.split("-")
    y2, m2, d2 = fecha_fin.split("-")
    fecha_inicio_formap = f"{y1}-{m1}-{d1}"
    fecha_fin_formap = f"{y2}-{m2}-{d2}"

    respuestas_html = []

    def _capturar(response):
        if "NO_Conformidad/IndexPartial" in response.url:
            try:
                respuestas_html.append(response.text())
            except Exception:
                pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=30)
        context = browser.new_context(storage_state=str(STORAGE_STATE))
        page = context.new_page()
        page.on("response", _capturar)

        page.goto(NC_INDEX_URL, wait_until="networkidle")
        page.add_style_tag(content=NEUTRALIZE_CSS)

        if "login" in page.url.lower():
            browser.close()
            raise FormapBotSesionExpirada("La sesión guardada expiró — hace falta relogin asistido (test_login.py).")

        page.fill("#TimeInicio", fecha_inicio_formap)
        page.fill("#TimeFin", fecha_fin_formap)

        _select_all(page, "ms-list-2")
        _select_all(page, "ms-list-3")
        page.wait_for_timeout(500)
        _select_all(page, "ms-list-4")
        page.wait_for_timeout(500)
        _select_all(page, "ms-list-5")

        _select_one(page, "ms-list-6", CONTRATISTA_LABEL)

        page.click("#Filtrar")
        page.wait_for_timeout(2500)

        _select_all(page, "ms-list-1")
        page.wait_for_timeout(500)

        # El buscador de texto (#nav-search2-input) vive DENTRO de los resultados
        # de IndexPartial, no en el formulario base — no existe en el DOM hasta
        # que se hace el primer clic en "Buscar Rutas". Por eso hay que cargar
        # resultados una vez (con Mostrar=100 para no paginar) y luego escribir
        # ahí el equipo_ruta_id y presionar Enter para filtrar.
        page.click("#BuscarRutas")
        page.wait_for_timeout(2500)
        try:
            page.wait_for_selector(".blockUI.blockOverlay", state="detached", timeout=15000)
        except Exception:
            pass

        page.wait_for_selector("#nav-search2-input", timeout=15000)
        page.select_option("#Mostrar", "100")
        page.wait_for_timeout(2500)
        try:
            page.wait_for_selector(".blockUI.blockOverlay", state="detached", timeout=15000)
        except Exception:
            pass

        # A partir de aquí, solo interesa la respuesta del Enter (la filtrada) —
        # se descartan las capturadas antes (carga inicial, cambio de Mostrar),
        # si no, parsear_nc() termina mezclando el listado sin filtrar con el
        # resultado real de la búsqueda.
        respuestas_html.clear()
        page.fill("#nav-search2-input", equipo_ruta_id)
        page.press("#nav-search2-input", "Enter")
        page.wait_for_timeout(3000)
        try:
            page.wait_for_selector(".blockUI.blockOverlay", state="detached", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(1000)

        browser.close()

    combinado = "\n<!-- SEP -->\n".join(respuestas_html)
    return parsear_nc(combinado)
