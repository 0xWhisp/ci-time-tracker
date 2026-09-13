# Roadmap

Tareas ordenadas por prioridad según ROI (impacto ÷ esfuerzo) y dependencias.

Esfuerzo: **S** ≤ 1 día · **M** 2–3 días · **L** 4–6 días

| # | Tarea | Esfuerzo | Impacto | ROI | Depende de |
|---|-------|----------|---------|-----|------------|
| 1 | Arreglar bugs críticos de parsing y empaquetado | S | Alto | ★★★★★ | — |
| 2 | Ingesta directa desde la API de GitHub Actions + caché local | L | Muy alto | ★★★★★ | 1 |
| 3 | Métricas accionables: tiempo total, costo estimado, tiempo en cola | S | Alto | ★★★★☆ | 2 |
| 4 | Flaky real (mismo commit / reintentos) y detección de regresiones | M | Alto | ★★★★☆ | 2 |
| 5 | Salida Markdown (`$GITHUB_STEP_SUMMARY`) y umbrales `--fail-on` | S | Medio | ★★★★☆ | 3, 4 |
| 6 | CI propio, publicación en PyPI y README con demo | S | Medio | ★★★★☆ | 1–5 |
| 7 | GitHub Action que comenta en PRs el impacto en tiempos | M | Alto | ★★★☆☆ | 5, 6 |
| 8 | Recomendaciones cruzando config y datos reales | L | Alto (diferenciador) | ★★★☆☆ | 2, 3 |
| 9 | Proveedores GitLab y CircleCI por API + registro extensible | M c/u | Medio | ★★☆☆☆ | 2 |
| 10 | Reporte HTML con gráficos de tendencia | M | Medio | ★★☆☆☆ | 3, 4 |

**Punto de validación:** después de la tarea 6, publicar y recoger feedback de usuarios reales
antes de invertir en 8–10.

---

## 1. Arreglar bugs críticos de parsing y empaquetado

La función principal hoy da resultados incorrectos con logs reales.

- [x] GitHub Actions: la duración de un paso va hasta el siguiente paso (o el teardown del job),
      no hasta `##[endgroup]`; los grupos anidados no se cuentan como pasos; precisión sub-segundo.
- [x] JSON: `duration_ms` / `durationMs` se convierten a segundos.
- [x] JSON: valores `0` (duración, id, estado) ya no se tratan como ausentes.
- [x] GitLab: fallos detectados dentro de la sección (y `ERROR: Job failed` atribuido a
      `step_script`), sin regex construida con el nombre; nombres de sección con `-` y `.`.
- [x] `requires-python` corregido a `>=3.10`; URLs reales; README sin `pip install` inexistente.
- [x] Tests de regresión con logs en formato real.

## 2. Ingesta desde la API de GitHub Actions

`ci-time-tracker github owner/repo --workflow ci.yml --last 200`

- Cliente para `/actions/runs` y `/actions/runs/{id}/jobs` (token por `GITHUB_TOKEN` o `gh auth token`).
- Mapear jobs y steps a `BuildLog` / `StepExecution` conservando `head_sha`, `run_attempt`,
  `created_at` (cola) y `runner` en los modelos.
- Caché SQLite incremental (no volver a descargar runs ya vistos); paginación y rate limit.
- Tests con respuestas grabadas (sin red).

**Aceptación:** analizar 200 runs de un repo público en < 1 min la primera vez y < 5 s con caché.

## 3. Métricas accionables

- Tiempo total consumido por paso/job (duración × ejecuciones) y ranking por impacto.
- Costo estimado en USD por tipo de runner (tabla configurable de precios por minuto).
- Tiempo en cola vs. tiempo de ejecución.

## 4. Flaky real y regresiones

- Flaky = mismo `head_sha` con fallo y éxito, o `run_attempt > 1` que termina en éxito
  (usar `is_retry`, hoy calculado pero ignorado).
- Reemplazar el criterio SLOW actual (`max > 1.5 × p90`, muy ruidoso) por regresión:
  mediana de ventana reciente vs. línea base, con umbral y número mínimo de muestras.
- Reportar el commit/rango donde empezó la regresión.

## 5. Salida Markdown y umbrales

- `--format markdown`, apto para `$GITHUB_STEP_SUMMARY`.
- `--fail-on flaky,regression` y `--max-duration` para usarlo como gate (exit code ≠ 0).

## 6. CI propio y publicación

- Workflow con matriz Python 3.10–3.13, lint y cobertura.
- Publicación en PyPI con trusted publishing en cada tag.
- README con GIF/demo, badges y ejemplo real de salida.

## 7. GitHub Action para PRs

- Action publicada en el Marketplace que comenta: "este PR hace el CI N s más lento", pasos
  flaky tocados y costo estimado.

## 8. Recomendaciones config × datos

- Detectar: dependencias sin caché cuando la instalación es lenta, jobs sin `needs` que podrían
  paralelizarse, falta de `concurrency: cancel-in-progress`, matrices sobredimensionadas.
- Cada recomendación con ahorro estimado en minutos/USD.
- Deprecar el modo `--estimates` manual.

## 9. GitLab y CircleCI por API

- Extraer una interfaz de proveedor (registro) y migrar GitHub a ella.
- GitLab: `/projects/:id/pipelines` y `/jobs`. CircleCI: Insights API.

## 10. Reporte HTML

- Página estática autocontenida con tendencias por paso, top de costo y flaky.
