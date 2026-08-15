"""
Similitud de texto para emparejar hallazgos de FORMAP con NC de bd_incidencias
que comparten equipo_ruta_id — mismo criterio validado en
formap-bot/load_to_staging.py (dos NC distintas pueden compartir ruta, así que
el ID solo no basta para decidir cuál es cuál).
"""
from difflib import SequenceMatcher


def similitud(a: str, b: str) -> float:
    a, b = (a or "").lower().strip(), (b or "").lower().strip()
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def mejor_match(nc: dict, hallazgos_formap: list[dict], umbral: float = 0.35) -> tuple[dict | None, float]:
    """nc: fila de bd_incidencias con 'titulo'/'descripcion'. hallazgos_formap:
    hallazgos de FORMAP (ya filtrados a los que tienen respuesta_comentario) para
    la MISMA ruta. Devuelve (hallazgo, score) del mejor match, o (None, 0.0) si
    ninguno supera el umbral — mejor no proponer nada que proponer mal."""
    texto_nc = f"{nc.get('titulo') or ''} {nc.get('descripcion') or ''}"
    mejor, mejor_score = None, 0.0
    for h in hallazgos_formap:
        texto_formap = f"{h.get('item_valor') or ''} {h.get('observacion_valor') or ''} {h.get('item_label') or ''}"
        score = similitud(texto_nc, texto_formap)
        if score > mejor_score:
            mejor, mejor_score = h, score
    if mejor_score >= umbral:
        return mejor, mejor_score
    return None, 0.0
