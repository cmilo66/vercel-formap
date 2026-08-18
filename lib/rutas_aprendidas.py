"""
Rutas de FORMAP "aprendidas" en caliente por el bot — el complemento dinámico
de RUTA_IDS_CONOCIDOS (formap_client.py).

Arquitectura de fallback: la búsqueda por equipo_ruta_id primero intenta el
camino rápido (HTTP puro, ~1s) con RUTA_IDS_CONOCIDOS + lo que haya aquí. Si no
encuentra nada, cae al bot (Playwright, ~20-40s), que no depende de ninguna
lista. Cuando el bot SÍ encuentra la NC, se extrae el RutaId interno real
(derivable del nombre de archivo de las evidencias) y se guarda aquí — así la
próxima búsqueda de esa misma ruta ya es rápida, sin volver a necesitar el bot.

Persistencia simple en un JSON en disco (no hace falta más para esto: es una
lista de unos pocos cientos de IDs numéricos, sin escritura concurrente
pesada). Sobrevive a reinicios del servidor local; no se sube a git a
propósito (cambia todo el tiempo, no es código) — ver .gitignore.
"""
import json
import re
from pathlib import Path

ARCHIVO = Path(__file__).parent / "rutas_aprendidas.json"

# Patrón real observado en las URLs de evidencia de FORMAP:
# ".../formsmap_81_45204_1531923--31-7-2026-4-45-52--....jpg"
#                        ^^^^^^^ RutaId interno
PATRON_RUTA_EN_URL = re.compile(r"formsmap_\d+_\d+_(\d+)--")


def cargar() -> list[str]:
    if not ARCHIVO.exists():
        return []
    try:
        return json.loads(ARCHIVO.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def agregar(ruta_id: str) -> bool:
    """Agrega un Ruta ID si no estaba ya. Devuelve True si de verdad se agregó
    algo nuevo (para poder loguear/informar), False si ya existía."""
    if not ruta_id:
        return False
    actuales = cargar()
    if ruta_id in actuales:
        return False
    actuales.append(ruta_id)
    ARCHIVO.write_text(json.dumps(actuales, indent=2), encoding="utf-8")
    return True


def extraer_ruta_interna(hallazgo: dict) -> str | None:
    """Dado un hallazgo ya parseado (parsear_nc), intenta extraer el RutaId
    interno real desde la URL de su primera evidencia. None si no hay
    evidencias o el patrón no calza (ej. NC sin fotos todavía)."""
    for ev in hallazgo.get("evidencias") or []:
        m = PATRON_RUTA_EN_URL.search(ev.get("url") or "")
        if m:
            return m.group(1)
    return None
