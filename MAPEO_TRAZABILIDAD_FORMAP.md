# Mapeo de trazabilidad de una NC en FORMAP

Investigación hecha extrayendo y leyendo el JS real que FORMAP le manda al navegador
(no es documentación oficial de FORMAP — es ingeniería inversa sobre su propio código,
usando el bot con sesión real para bajar las páginas y buscar en su fuente). Confirmado
contra datos reales de 77 NC de junio 2026.

## 1. Los dos campos de estado de una NC

Cada NC en FORMAP tiene DOS campos de estado independientes, visibles en el
encabezado de cada fila del listado:

| Campo | Valores observados | Qué significa |
|---|---|---|
| **Estado NC** | `Aplica` (100% de la muestra) | Si el hallazgo de auditoría es válido. El dropdown de edición (`EstadoNcId`) también ofrece `No aplica` como alternativa, pero no se observó ninguna NC en ese estado en la muestra — probablemente se decide al crear la NC, no cambia en el flujo normal de cierre. |
| **Sub Estado NC** | `Generada`, `Aceptada` | El paso real del ciclo de vida (ver mapa abajo). Ninguna NC de la muestra llegó a `Resuelta` todavía. |

## 2. El ciclo de vida real (reconstruido)

```
Generada  →  [se agrega una observación/respuesta]  →  Aceptada  →  [NcResuelta()]  →  Resuelta
   │                                                        │
   │ estado inicial al crear la NC                          │ requiere que la NC ya esté "conciliada"
```

- **Generada**: estado inicial de toda NC nueva.
- **Aceptada**: aparece después de que alguien agrega una observación/respuesta (ver
  punto 3). En la muestra, **75 de 77 NC (97%) ya tienen una respuesta** pero siguen en
  `Aceptada`, no en `Resuelta` — confirma que tener respuesta ≠ estar resuelta.
- **Resuelta**: solo se alcanza manualmente, con el botón "NC RESUELTA" en la fila
  (`onclick="NcResuelta(165991)"`), que dispara `POST /NO_Conformidad/NcResuelta {id}`.

## 3. Qué es "conciliar" una NC (el paso oculto)

El botón "NC RESUELTA" tiene una validación server-side explícita. Su propio mensaje
de error, tomado literal del JS:

> *"Hubo un error al cambiar el Sub Estado de la NC, tenga en cuenta que para indicar
> una NC como resuelta, la NC ya debe haber sido **conciliada**."*

FORMAP nunca expone un botón "Conciliar" separado. La pieza que sí existe y que su
propio código nombra explícitamente `HistorialConciliacion` es la tabla de
observaciones/respuestas — el mismo endpoint que ya integramos en el panel:

```
POST /NO_Conformidad/getTablaHistorialConciliacion  { NoconformidadId }
```

Esto confirma la hipótesis: **"conciliar" = que exista al menos una fila en el
historial de observaciones con un Estado válido** (agregada vía `agregarObserv()` →
`POST /NO_Conformidad/SetObservaciones`, el mismo formulario "Agregar observación"
del modal de detalle). Es decir, el flujo real para cerrar una NC en FORMAP es:

1. Alguien entra al detalle de la NC, llena "Observaciones" + adjunta evidencia +
   elige Estado, y le da "Agregar observación" → esto la concilia (queda en la
   tabla `HistorialConciliacion`, Sub Estado pasa a `Aceptada`).
2. Solo entonces el botón "NC RESUELTA" (`NcResuelta`) funciona y la deja en
   `Resuelta`.

## 4. Endpoints mapeados (todos confirmados contra el JS real)

| Endpoint | Método | Qué hace |
|---|---|---|
| `/NO_Conformidad/IndexPartial` | POST | Listado/búsqueda (ya usado por el panel). |
| `/NO_Conformidad/Details` | POST | Cáscara del modal de edición — Estado, campo Observaciones, adjuntar archivo. NO trae el historial (llega vacío, se llena aparte). |
| `/NO_Conformidad/getTablaHistorialConciliacion` | POST `{NoconformidadId}` | El historial real de observaciones/conciliación — ya integrado en el panel (`detalle_completo()`). |
| `/NO_Conformidad/SetObservaciones` | POST (FormData) | Agrega una observación nueva — esto es lo que "concilia" la NC. Requiere `Estados`, `Observacion`, `NoConformidad`, y opcionalmente `UploadFile`/`UploadFile2`. |
| `/NO_Conformidad/ActualizarNC` / `ActualizarNcSinArchivo` | POST | Edita una observación YA existente (no crea una nueva). |
| `/NO_Conformidad/NcResuelta` | POST `{id}` | Marca la NC como `Resuelta` — **falla si la NC no tiene ninguna observación conciliada todavía**. Devuelve string vacío en error, no-vacío en éxito. |

## 5. Lo que esto significa para el bot/panel

- El endpoint `marcar_resuelta()` que ya existe en `formap_client.py` (marcado
  EXPERIMENTAL) llama directo a `NcResuelta` — que **va a fallar siempre** si antes no
  se llamó a `SetObservaciones` para esa NC. No es un bug del bot, es el flujo real de
  FORMAP.
- Para automatizar un cierre de verdad en FORMAP (no solo en `bd_incidencias`) hacen
  falta DOS llamadas en orden: primero `SetObservaciones` (con el comentario/evidencia
  que ya trae el bot desde `bd_incidencias` o desde donde se origine el cierre),
  después `NcResuelta`. Ninguna de las dos está implementada como escritura hoy —
  todo lo que el panel hace hacia FORMAP sigue siendo de solo lectura.
- No se encontró un tercer estado "Conciliada" explícito en la UI — es un estado
  implícito (tener ≥1 fila en `HistorialConciliacion`), no una columna visible.

## 6. Qué falta para confirmar al 100%

Esto es ingeniería inversa sobre el JS y datos reales, no la documentación de FORMAP —
sigue siendo una hipótesis bien fundamentada, no un hecho verificado end-to-end. Antes
de automatizar una escritura real contra FORMAP con esto, hace falta:
1. Probar `SetObservaciones` + `NcResuelta` contra UNA sola NC de prueba.
2. Verificar manualmente en FORMAP que el Sub Estado sí cambió a `Resuelta` como se
   espera.
