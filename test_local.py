"""Prueba local del cliente (fuera de Vercel) reusando storage_state.json del
proyecto formap-bot, para validar el cliente antes de desplegar."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.formap_client import FormapClient  # noqa: E402

STORAGE_STATE = Path(__file__).parent.parent / "formap-bot" / "storage_state.json"

state = json.loads(STORAGE_STATE.read_text(encoding="utf-8"))
cookies = {c["name"]: c["value"] for c in state["cookies"] if c["domain"] in ("formap.co", ".formap.co")}
print(f"Cookies cargadas: {list(cookies.keys())}")

client = FormapClient(cookies)

print("\n--- Test 1: catálogo TipoEquipo ---")
tipos = client.get_tipos_equipo()
print(f"Tipos de equipo encontrados: {len(tipos)}")

print("\n--- Test 2: buscar por equipo_ruta_id (esperado: 3 hallazgos) ---")
hallazgos = client.buscar_por_equipo_ruta_id(
    equipo_ruta_id="38607927",
    fecha_inicio="01/07/2026 0:00:00",
    fecha_fin="31/07/2026 0:00:00",
    ruta_ids="1526568,1526571,1526572,1526573,1526578,1526579,1526580,1526583,1526584,1526585,1526594,1526595,1526596,1526597,1531923",
)
print(f"Hallazgos encontrados: {len(hallazgos)}")
for h in hallazgos:
    print(f"  NC #{h['nc_formap_id']} | {h['item_valor'] or h['observacion_valor']} | evidencias: {len(h['evidencias'])}")

assert len(hallazgos) == 3, f"Se esperaban 3 hallazgos, llegaron {len(hallazgos)}"
print("\nOK — coincide con lo esperado.")
