"""
Escritura CONTROLADA en bd_incidencias — exclusiva para el cierre de una NC que
un humano ya confirmó en pantalla (panel 'Cierre Periódico'), nunca automático.

Deliberadamente separado de db_incidencias.py (que sigue siendo de SOLO lectura,
usado por el buscador) para que esa garantía siga siendo cierta sin excepciones —
si algo escribe en bd_incidencias, solo puede venir de este archivo.

Replica exactamente el patrón ya validado en producción en
formap-bot/cerrar_34_produccion.py (34/36 NC de Andrea Anillo, agosto 2026):
lock FOR UPDATE + optimistic version check, cabeza de cadena de historial
robusta vía NOT EXISTS (inmune al bug de desempate por fecha del trigger
before_insert_historial), y verificación de la cadena antes de comprometer.
"""
import hashlib
import os
import time
import uuid

import pymysql

BASE_DATOS_PERMITIDA = "bd_incidencias"
BOT_USER_ID = "94a4ad3f-97ff-11f1-93bd-fa163efdd0ae"  # usuarios('Bot FORMAP') en bd_incidencias


def conectar():
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASS"],
        database=BASE_DATOS_PERMITIDA,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=8,
        autocommit=False,
    )


def _insertar_historial(cur, nc_id, actor_id, estado_anterior, estado_nuevo, comentario, prev_hash, prev_hist_id):
    nuevo_id = str(uuid.uuid4())
    hash_actual = hashlib.sha256(
        (
            (prev_hash or "genesis") + nc_id + estado_nuevo + "0" + actor_id
            + (comentario or "") + str(time.time()) + os.urandom(8).hex()
        ).encode("utf-8")
    ).hexdigest()
    cur.execute(
        """INSERT INTO estados_historial
             (id, no_conformidad_id, actor_id, estado_anterior, estado_nuevo,
              sub_estado_paso, comentario, hash_anterior, hash_actual, prev_historial_id, tipo)
           VALUES (%s,%s,%s,%s,%s,0,%s,%s,%s,%s,'nc_estado')""",
        (nuevo_id, nc_id, actor_id, estado_anterior, estado_nuevo, comentario, prev_hash, hash_actual, prev_hist_id),
    )
    return nuevo_id


def _tipo_archivo(url: str) -> str:
    return "documento" if url.lower().endswith(".pdf") else "foto"


def cerrar_nc(nc_id: str, nc_formap_id: str, equipo_ruta_id: str, comentario_formap: str, evidencias: list[dict]) -> dict:
    """Cierra UNA NC en bd_incidencias. Lanza excepción si algo no cuadra —
    nunca deja la NC en un estado a medias (todo dentro de una transacción con
    rollback en cualquier error)."""
    conn = conectar()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT estado_actual, version FROM no_conformidades WHERE id=%s FOR UPDATE", (nc_id,))
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("NC no encontrada en bd_incidencias.")
            if row["estado_actual"] == "cerrada":
                conn.rollback()
                return {"ok": False, "motivo": "ya_cerrada"}
            estado_anterior = row["estado_actual"]
            version_actual = row["version"]

            # Cabeza real de la cadena: la única fila que nadie referencia como
            # prev_historial_id — inmune al desempate por fecha del trigger.
            cur.execute(
                """SELECT h.id AS historial_id, h.hash_actual FROM estados_historial h
                   WHERE h.no_conformidad_id=%s
                   AND NOT EXISTS (SELECT 1 FROM estados_historial h2 WHERE h2.prev_historial_id = h.id)""",
                (nc_id,),
            )
            cabezas = cur.fetchall()
            if len(cabezas) > 1:
                raise RuntimeError(f"Cadena de historial ambigua: {len(cabezas)} cabezas sin resolver.")
            ult = cabezas[0] if cabezas else None

            comentario = (
                f"Cierre importado de FORMAP (NC #{nc_formap_id}, equipo_ruta_id={equipo_ruta_id}). "
                f"Respuesta en FORMAP: {comentario_formap}"
            )
            hist_id = _insertar_historial(
                cur, nc_id, BOT_USER_ID, estado_anterior, "cerrada", comentario,
                ult["hash_actual"] if ult else None, ult["historial_id"] if ult else None,
            )

            for ev in evidencias or []:
                url = (ev.get("url") or "").strip()
                if not url:
                    continue
                full_url = url if url.startswith("http") else "https://formap.co" + url.replace("\\", "/")
                cur.execute(
                    """INSERT INTO nc_fotos
                         (id, no_conformidad_id, paso, dropbox_path, nombre_original,
                          tipo, subido_por, dropbox_url, origen, historial_id)
                       VALUES (%s,%s,0,NULL,%s,%s,%s,%s,'formap',%s)""",
                    (
                        str(uuid.uuid4()), nc_id, (ev.get("titulo") or "Evidencia de cierre FORMAP")[:255],
                        _tipo_archivo(full_url), BOT_USER_ID, full_url, hist_id,
                    ),
                )

            cur.execute(
                """UPDATE no_conformidades
                   SET estado_actual='cerrada', bloqueado=1, version=version+1, actualizado_en=NOW()
                   WHERE id=%s AND version=%s""",
                (nc_id, version_actual),
            )
            if cur.rowcount != 1:
                raise RuntimeError("La NC cambió entre la lectura y la escritura (version distinta) — reintentar.")

            cur.execute(
                """SELECT h.id FROM estados_historial h
                   WHERE h.no_conformidad_id=%s
                   AND NOT EXISTS (SELECT 1 FROM estados_historial h2 WHERE h2.prev_historial_id = h.id)""",
                (nc_id,),
            )
            cabezas_final = cur.fetchall()
            if len(cabezas_final) != 1 or cabezas_final[0]["id"] != hist_id:
                raise RuntimeError("Verificación de cadena de historial falló tras insertar.")

        conn.commit()
        return {"ok": True, "historial_id": hist_id}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
