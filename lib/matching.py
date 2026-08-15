"""
Emparejar hallazgos de FORMAP con NC de bd_incidencias que comparten equipo_ruta_id
(el ID solo no basta — varias NC pueden compartir la misma ruta/equipo).

Arquitectura de dos niveles, en orden de prioridad:

1. OBSERVACIÓN primero: compara `descripcion` de bd_incidencias contra el campo
   "Observación" de FORMAP (observacion_valor/observacion_label). Las NC más
   antiguas se importaron A MANO desde el Excel de FORMAP antes de que existiera
   este pipeline — en esa importación manual, el texto que terminó en
   `descripcion` casi siempre es una copia del campo Observación de FORMAP
   (no del campo Ítem), así que es la señal más confiable para esas NC viejas.
2. Si observación no da un match confiable, cae a TÍTULO/ítem (el criterio
   anterior, validado en formap-bot/load_to_staging.py) como respaldo — sigue
   sirviendo para las NC importadas por el pipeline automático, donde el título
   sí se copió del campo Ítem de FORMAP.

Desempate: si dos candidatos quedan con score parecido (dentro de 0.05), se
prefiere el que tenga la fecha más cercana a la fecha de creación de la NC en
bd_incidencias — mismo espíritu que "ruta + fecha + descripción + título" que
ya se usaba, solo que ahora la fecha desempata en vez de ser un filtro binario.
"""
from datetime import datetime
from difflib import SequenceMatcher

UMBRAL_OBSERVACION = 0.45  # más exigente: es la señal primaria, un falso positivo aquí pesa más
UMBRAL_TITULO = 0.35       # respaldo, como antes
MARGEN_EMPATE = 0.05


def similitud(a: str, b: str) -> float:
    a, b = (a or "").lower().strip(), (b or "").lower().strip()
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _dias_entre(fecha_nc, fecha_formap_str) -> float:
    """Distancia en días entre la fecha de la NC en bd_incidencias (datetime o
    string ISO) y la 'Fecha NC' de FORMAP (string 'YYYY-MM-DD HH:MM:SS' u
    similar). Devuelve infinito si no se puede parsear — no descalifica el
    match, solo lo deja sin ventaja en el desempate."""
    try:
        if isinstance(fecha_nc, str):
            fecha_nc = datetime.fromisoformat(fecha_nc.replace("Z", ""))
        fecha_f = datetime.fromisoformat(str(fecha_formap_str)[:19])
        return abs((fecha_nc - fecha_f).total_seconds()) / 86400
    except (ValueError, TypeError):
        return float("inf")


def mejor_match(nc: dict, hallazgos_formap: list[dict], umbral: float = UMBRAL_TITULO) -> tuple[dict | None, float]:
    """nc: fila de bd_incidencias con 'titulo'/'descripcion'/'creado_en'.
    hallazgos_formap: hallazgos de FORMAP para la MISMA ruta. Devuelve
    (hallazgo, score) del mejor match, o (None, 0.0) si ninguno es confiable —
    mejor no proponer nada que proponer mal.

    `umbral` se mantiene por compatibilidad (usado como umbral de título/respaldo
    cuando se llama desde código que no distingue niveles); el umbral de
    observación usa su propia constante, más exigente."""
    if not hallazgos_formap:
        return None, 0.0

    descripcion_nc = nc.get("descripcion") or ""
    texto_titulo_nc = f"{nc.get('titulo') or ''} {nc.get('descripcion') or ''}"
    fecha_nc = nc.get("creado_en")

    # ── Nivel 1: observación (prioridad para NC viejas importadas a mano) ──────
    candidatos_obs = []
    for h in hallazgos_formap:
        texto_obs_formap = f"{h.get('observacion_valor') or ''} {h.get('observacion_label') or ''}"
        score = similitud(descripcion_nc, texto_obs_formap)
        if score >= UMBRAL_OBSERVACION:
            candidatos_obs.append((h, score))

    if candidatos_obs:
        candidatos_obs.sort(key=lambda t: t[1], reverse=True)
        mejor_score = candidatos_obs[0][1]
        empatados = [c for c in candidatos_obs if mejor_score - c[1] <= MARGEN_EMPATE]
        if len(empatados) > 1 and fecha_nc:
            empatados.sort(key=lambda c: _dias_entre(fecha_nc, c[0].get("fecha_nc")))
        return empatados[0][0], empatados[0][1]

    # ── Nivel 2: título/ítem (respaldo, criterio anterior) ─────────────────────
    candidatos_titulo = []
    for h in hallazgos_formap:
        texto_formap = f"{h.get('item_valor') or ''} {h.get('observacion_valor') or ''} {h.get('item_label') or ''}"
        score = similitud(texto_titulo_nc, texto_formap)
        if score >= umbral:
            candidatos_titulo.append((h, score))

    if not candidatos_titulo:
        return None, 0.0

    candidatos_titulo.sort(key=lambda t: t[1], reverse=True)
    mejor_score = candidatos_titulo[0][1]
    empatados = [c for c in candidatos_titulo if mejor_score - c[1] <= MARGEN_EMPATE]
    if len(empatados) > 1 and fecha_nc:
        empatados.sort(key=lambda c: _dias_entre(fecha_nc, c[0].get("fecha_nc")))
    return empatados[0][0], empatados[0][1]
