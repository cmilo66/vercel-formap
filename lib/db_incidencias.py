"""
Acceso de SOLO LECTURA a `bd_incidencias` (la base viva de producción de nc_deploy,
mismo servidor MySQL que `bd_respaldonc`).

Regla explícita del usuario: este módulo NUNCA debe escribir en `bd_incidencias` —
solo se integra para traer información y mostrarla en el panel (ej. si una NC de
FORMAP ya existe en producción, y cuál fue su último comentario/cierre), nunca para
modificarla. Por eso, a propósito, este archivo no expone ninguna función de
INSERT/UPDATE/DELETE — solo consultas SELECT, todas parametrizadas.

Separado de `db.py` (que sigue siendo exclusivo de `bd_respaldonc`) para que la
garantía "db.py nunca toca bd_incidencias" siga siendo cierta sin excepciones.
"""
import os

import pymysql

BASE_DATOS_PERMITIDA = "bd_incidencias"


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


def buscar_por_equipo_ruta_id(equipo_ruta_id: str) -> list[dict]:
    """Dado el equipo_ruta_id de un hallazgo de FORMAP, busca NC ya existentes en
    bd_incidencias.no_conformidades con ese mismo equipo_ruta_id, trayendo el
    último comentario/estado de su historial (para decidir si hace falta
    'actualizar cierre'). Puede devolver más de una fila — equipo_ruta_id no es
    único en bd_incidencias (varias NC pueden compartir la misma ruta/equipo)."""
    conn = conectar()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    nc.id, nc.titulo, nc.descripcion, nc.estado_actual,
                    nc.equipo_ruta_id, nc.fuente, nc.actualizado_en,
                    uh.comentario AS ultimo_comentario,
                    uh.fecha AS ultimo_comentario_fecha,
                    uh.tipo AS ultimo_comentario_tipo
                FROM no_conformidades nc
                LEFT JOIN estados_historial uh ON uh.id = (
                    SELECT h2.id
                    FROM estados_historial h2
                    WHERE h2.no_conformidad_id = nc.id
                    ORDER BY h2.fecha DESC, h2.id DESC
                    LIMIT 1
                )
                WHERE nc.equipo_ruta_id = %s
                ORDER BY nc.creado_en DESC
                """,
                (equipo_ruta_id,),
            )
            return cur.fetchall()
    finally:
        conn.close()
