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
    último comentario/estado de su historial y toda la evidencia (fotos/PDF)
    asociada — para decidir si hace falta 'actualizar cierre' con la evidencia
    ya a la vista, sin tener que abrir nc_deploy aparte. Puede devolver más de
    una fila — equipo_ruta_id no es único en bd_incidencias (varias NC pueden
    compartir la misma ruta/equipo)."""
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
            filas = cur.fetchall()
            if not filas:
                return filas

            ids = [f["id"] for f in filas]
            marcadores = ",".join(["%s"] * len(ids))
            cur.execute(
                f"""SELECT no_conformidad_id, dropbox_url, nombre_original, tipo
                    FROM nc_fotos WHERE no_conformidad_id IN ({marcadores})""",
                ids,
            )
            fotos_por_nc = {}
            for foto in cur.fetchall():
                fotos_por_nc.setdefault(foto["no_conformidad_id"], []).append(foto)
            for fila in filas:
                fila["fotos"] = fotos_por_nc.get(fila["id"], [])
            return filas
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


def detalle_nc(nc_id: str) -> dict | None:
    """Lectura: cabecera + historial completo de UNA NC de bd_incidencias (el
    'Flujo de gestión' que ya se ve en nc_deploy), para expandir la tarjeta del
    panel sin salir de aquí. Devuelve None si la NC no existe o no pertenece al
    centro operativo permitido (defensa en profundidad, además del filtro que
    ya se aplica en los listados que originan el nc_id)."""
    conn = conectar()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, titulo, descripcion, estado_actual, prioridad, tipo,
                       equipo_ruta_id, creado_en, actualizado_en
                FROM no_conformidades
                WHERE id = %s AND centro_operativo_id = %s
                """,
                (nc_id, CENTRO_OPERATIVO_PERMITIDO),
            )
            nc = cur.fetchone()
            if nc is None:
                return None

            cur.execute(
                """
                SELECT h.id, h.estado_anterior, h.estado_nuevo, h.comentario, h.fecha, h.tipo,
                       u.nombre AS actor_nombre
                FROM estados_historial h
                LEFT JOIN usuarios u ON u.id = h.actor_id
                WHERE h.no_conformidad_id = %s
                ORDER BY h.fecha ASC
                """,
                (nc_id,),
            )
            historial = cur.fetchall()

            cur.execute(
                """
                SELECT historial_id, dropbox_url, nombre_original, tipo, origen
                FROM nc_fotos
                WHERE no_conformidad_id = %s
                """,
                (nc_id,),
            )
            fotos_por_historial = {}
            for foto in cur.fetchall():
                fotos_por_historial.setdefault(foto["historial_id"], []).append(foto)

            for h in historial:
                h["fotos"] = fotos_por_historial.get(h["id"], [])

            nc["historial"] = historial
            return nc
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
