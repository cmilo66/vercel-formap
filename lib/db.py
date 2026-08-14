"""
Acceso a base de datos — EXCLUSIVAMENTE a `bd_respaldonc`.

Regla explícita del usuario: este servicio nunca debe tocar `bd_incidencias`
(la base viva de producción). El nombre de la base está fijo en código (no viene
de una env var) precisamente para que un error de configuración no pueda apuntar
esto sin querer a otra base.
"""
import os

import pymysql

BASE_DATOS_PERMITIDA = "bd_respaldonc"


def conectar():
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASS"],
        database=BASE_DATOS_PERMITIDA,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=8,
    )


def obtener_usuario_panel(usuario: str) -> dict | None:
    conn = conectar()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, usuario, password_hash, activo FROM panel_usuarios WHERE usuario=%s",
                (usuario,),
            )
            return cur.fetchone()
    finally:
        conn.close()
