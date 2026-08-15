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

# centros_operativos.id = 1 -> 'MANTENIMIENTO ATLÁNTICO AIR-E'. Este servicio es
# exclusivo de ese centro operativo (mismo alcance que ya se fuerza del lado de
# FORMAP con CONTRATISTA_FSCR) -- bd_incidencias tiene NC de muchos otros
# centros operativos que no son de este panel.
CENTRO_OPERATIVO_PERMITIDO = 1


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
                WHERE nc.equipo_ruta_id = %s AND nc.centro_operativo_id = %s
                ORDER BY nc.creado_en DESC
                """,
                (equipo_ruta_id, CENTRO_OPERATIVO_PERMITIDO),
            )
            return cur.fetchall()
    finally:
        conn.close()


def listar_abiertas_formap(fecha_inicio: str, fecha_fin: str) -> list[dict]:
    """Lectura: NC abiertas con fuente='formap' y equipo_ruta_id no vacío,
    creadas dentro de fecha_inicio/fecha_fin (formato YYYY-MM-DD). Es el punto de
    partida del escaneo de 'Cierre Periódico' — qué NC hay que ir a revisar
    contra FORMAP."""
    conn = conectar()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, titulo, descripcion, equipo_ruta_id, creado_en
                FROM no_conformidades
                WHERE fuente = 'formap' AND estado_actual = 'abierta'
                  AND equipo_ruta_id IS NOT NULL AND equipo_ruta_id != ''
                  AND centro_operativo_id = %s
                  AND creado_en BETWEEN %s AND %s
                ORDER BY creado_en
                """,
                (CENTRO_OPERATIVO_PERMITIDO, f"{fecha_inicio} 00:00:00", f"{fecha_fin} 23:59:59"),
            )
            return cur.fetchall()
    finally:
        conn.close()


def listar_formap(fecha_inicio: str, fecha_fin: str, estado: str | None = None) -> list[dict]:
    """Lectura: tabla de estado — todas las NC con fuente='formap' creadas en el
    rango dado, sin importar si están abiertas o cerradas (a diferencia de
    listar_abiertas_formap, que es solo el insumo del escaneo de cierre). `estado`
    filtra opcionalmente por 'abierta'/'cerrada'/etc.; None trae todas.
    El volumen total del sistema es pequeño (cientos de filas, no cientos de
    miles) — un filtro simple por rango basta, no hace falta paginación por
    cursor aquí."""
    conn = conectar()
    try:
        with conn.cursor() as cur:
            condiciones = ["fuente = 'formap'", "centro_operativo_id = %s", "creado_en BETWEEN %s AND %s"]
            parametros = [CENTRO_OPERATIVO_PERMITIDO, f"{fecha_inicio} 00:00:00", f"{fecha_fin} 23:59:59"]
            if estado:
                condiciones.append("estado_actual = %s")
                parametros.append(estado)
            cur.execute(
                f"""
                SELECT id, titulo, equipo_ruta_id, estado_actual, creado_en, actualizado_en
                FROM no_conformidades
                WHERE {' AND '.join(condiciones)}
                ORDER BY creado_en DESC
                """,
                parametros,
            )
            return cur.fetchall()
    finally:
        conn.close()
