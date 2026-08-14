"""
Autenticación de humanos para el panel (distinta del X-Service-Key, que es para
llamadas servidor-a-servidor desde PHP). Contraseñas con bcrypt, sesión como
token firmado (HMAC) con expiración — sin tabla de sesiones, porque Vercel
serverless no mantiene estado entre invocaciones.
"""
import base64
import hashlib
import hmac
import json
import os
import time

import bcrypt

TOKEN_TTL_SEGUNDOS = 12 * 3600  # 12 horas


def _secreto() -> bytes:
    # Reusa SERVICE_KEY como secreto de firma — mismo nivel de confianza que ya
    # exige el resto del servicio. Si se quiere separar más adelante, agregar
    # una env var AUTH_SECRET dedicada y cambiar esta línea.
    return os.environ["SERVICE_KEY"].encode()


def verificar_password(password_plano: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password_plano.encode(), password_hash.encode())


def generar_token(usuario: str) -> str:
    payload = {"usuario": usuario, "exp": time.time() + TOKEN_TTL_SEGUNDOS}
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    firma = hmac.new(_secreto(), payload_b64.encode(), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{firma}"


def verificar_token(token: str) -> str | None:
    """Devuelve el usuario si el token es válido y no expiró, o None."""
    try:
        payload_b64, firma = token.split(".", 1)
        firma_esperada = hmac.new(_secreto(), payload_b64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(firma, firma_esperada):
            return None
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode()))
        if payload["exp"] < time.time():
            return None
        return payload["usuario"]
    except Exception:
        return None
