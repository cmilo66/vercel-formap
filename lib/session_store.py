"""
Dónde vive la sesión de FORMAP (las cookies) que el servicio reutiliza.

Implementación mínima para arrancar: variable de entorno `FORMAP_SESSION_JSON`
(un JSON simple {"cookie_name": "valor", ...}), actualizable desde el dashboard
de Vercel o vía su API. Es lo más simple para salir andando, pero tiene un
límite práctico: cambiarla requiere un redeploy o llamar la API de Vercel.

Para producción real, migrar a Vercel KV (o Upstash Redis, o una tabla en la
propia BD de nc_deploy) para que `guardar_sesion()` pueda escribir en caliente
sin redeploy — dejado como TODO explícito abajo, no implementado todavía para
no adivinar qué backend de KV vas a preferir.
"""
import json
import os
import time

_ENV_VAR = "FORMAP_SESSION_JSON"
_ENV_VAR_TS = "FORMAP_SESSION_TIMESTAMP"


def cargar_sesion() -> dict:
    raw = os.environ.get(_ENV_VAR)
    if not raw:
        raise RuntimeError(f"No hay sesión guardada — falta la variable de entorno {_ENV_VAR}.")
    return json.loads(raw)


def edad_sesion_segundos() -> float | None:
    ts = os.environ.get(_ENV_VAR_TS)
    if not ts:
        return None
    return time.time() - float(ts)


def guardar_sesion(cookies: dict):
    """TODO: implementar contra un KV real (Vercel KV / Upstash / tabla en BD).
    Por ahora esto NO persiste en runtime serverless (cada invocación es un
    proceso nuevo) — es solo el punto de extensión. El refrescador de sesión
    (login asistido con captcha) debe llamar a esto una vez se resuelva el KV
    elegido; mientras tanto, actualizar la env var manualmente en Vercel."""
    raise NotImplementedError(
        "Falta elegir el backend de KV persistente (ver docstring del módulo) "
        "antes de que el servicio pueda auto-actualizar su propia sesión."
    )
