# Roadmap

Tareas ordenadas por prioridad según ROI (impacto ÷ esfuerzo) y dependencias.

Esfuerzo: **S** ≤ 1 día · **M** 2–3 días · **L** 4–6 días

| # | Tarea | Esfuerzo | Impacto | ROI | Depende de | Estado |
|---|-------|----------|---------|-----|------------|--------|
| 1 | Arreglar bugs críticos de parsing y empaquetado | S | Alto | ★★★★★ | — | ✅ Hecha (`6dd8d99`) |
| 2 | Ingesta directa desde la API de GitHub Actions + caché local | L | Muy alto | ★★★★★ | 1 | ✅ Hecha |
| 3 | Métricas accionables: tiempo total, costo estimado, tiempo en cola | S | Alto | ★★★★☆ | 2 | ✅ Hecha |
| 4 | Flaky real (mismo commit / reintentos) y detección de regresiones | M | Alto | ★★★★☆ | 2 | ✅ Hecha |
| 5 | Salida Markdown (`$GITHUB_STEP_SUMMARY`) y umbrales `--fail-on` | S | Medio | ★★★★☆ | 3, 4 | ✅ Hecha |
| 6 | CI propio, publicación en PyPI y README con demo | S | Medio | ★★★★☆ | 1–5 | ✅ Hecha (0.2.0 en PyPI) |
| 7 | GitHub Action que comenta en PRs el impacto en tiempos | M | Alto | ★★★☆☆ | 5, 6 | ✅ Hecha (falta Marketplace) |
| 8 | Recomendaciones cruzando config y datos reales | L | Alto (diferenciador) | ★★★☆☆ | 2, 3 | ✅ Hecha |
| 9 | Proveedores GitLab y CircleCI por API + registro extensible | M c/u | Medio | ★★☆☆☆ | 2 | ⬜ Pendiente |
| 10 | Reporte HTML con gráficos de tendencia | M | Medio | ★★☆☆☆ | 3, 4 | ✅ Hecha |

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

`ci-time-tracker --github owner/repo --workflow ci.yml --last 200`

- [x] Cliente para `/actions/runs` y `/actions/runs/{id}/jobs` (token por `GITHUB_TOKEN` o `GH_TOKEN`),
      solo con la librería estándar, sin dependencias nuevas.
- [x] Mapear jobs y steps a `BuildLog` / `StepExecution` conservando `head_sha`, `run_attempt`,
      tiempo en cola y `runner`. Los pasos se nombran `job / step`, como en la interfaz de GitHub.
- [x] Caché SQLite incremental (solo se cachean runs completados) y peticiones de jobs concurrentes.
- [x] Paginación de runs y de jobs; errores de rate limit, permisos y 404 con mensajes accionables.
- [x] Tests con respuestas grabadas (sin red).

**Aceptación:** analizar 200 runs de un repo público en < 1 min la primera vez y < 5 s con caché.

**Medido** (psf/requests, 10 runs, sin token): 6,6 s en serie → **2,2 s** concurrente en frío y
**0,6 s** con caché. Extrapolado, 200 runs ≈ 15 s.

**Criterio verificado** con 200 runs reales en el CI (workflow `benchmark.yml`, que corre con
token y se repite cuando cambia la ingesta o el análisis). La primera corrida **falló** el
límite con caché (6 s): el cuello de botella era la detección de regresiones de la tarea 4,
cuadrática por paso. Tras hacerla O(n log n): **15 s en frío y < 5 s con caché**
([run 39059830326](https://github.com/0xWhisp/ci-time-tracker/actions/runs/39059830326)).
El margen con caché es poco; lo que queda es arranque, el listado de runs (que no se
cachea porque tiene que ver los nuevos) y el análisis.

## 3. Métricas accionables

- [x] Tiempo total consumido por paso (duración × ejecuciones) y ranking por impacto;
      la tabla se ordena por tiempo consumido y `--top N` la recorta.
- [x] Costo estimado en USD por runner, con el redondeo por job que aplica GitHub y
      tabla de precios configurable (`--pricing`). Los runners sin tarifa conocida se
      listan en `unpriced_runners` en vez de asumirse gratis.
- [x] Tiempo en cola (de los datos de job) separado del tiempo de ejecución, con p50 y p90.
- [x] `--group-matrix` para unir las patas de una matriz.

**Hallazgo:** sin agrupar la matriz el titular engaña. En psf/requests, el top decía
"build (3.12, windows-latest) / Run tests — 4,6%" cuando `Run tests` en realidad se lleva
el **69,2%** del tiempo total. De ahí `--group-matrix`.

**Posible siguiente paso:** comparar costo por runner de la misma matriz (Windows sale
2× y macOS 10× más caro que Linux por minuto), para recomendar mover legs de plataforma.

## 4. Flaky real y regresiones

- [x] Flaky = el mismo paso falló y pasó **en el mismo commit**. Se comparan las patas de
      la matriz por separado (un fallo solo en Windows es una rotura, no flaky) y por
      workflow. Se traen también los intentos anteriores de los runs re-ejecutados, para
      contar los "pasó al reintentar". Sin datos de commit (logs de texto) se usa el
      criterio por tasa, y el reporte lo dice.
- [x] El criterio SLOW (`max > 1.5 × p90`) se eliminó. En su lugar, regresión por punto de
      cambio: la historia se parte donde mejor separa dos tramos estables; debe subir
      ≥25% (`--regression-threshold`) y ≥10 s, y seguir alta en los 5 builds más recientes.
- [x] El reporte nombra el commit y el build donde empezó la regresión.

**En datos reales** (psf/requests, 40 runs): desaparecen los avisos SLOW ruidosos
("Set up job: P90 1s, Max 3s"). No hubo flaky ni regresiones en esa ventana, y solo 3 pasos
tenían los ≥10 builds necesarios: hace falta más historia (y un token) para ver detecciones
reales. La lógica está cubierta por tests con casos construidos.

**De paso:** los tests de propiedades fallaban de forma intermitente por el límite de
200 ms por ejemplo de Hypothesis (máquina cargada). Se quitó ese límite en `tests/conftest.py`.

## 5. Salida Markdown y umbrales

- [x] `--format markdown`, apto para `$GITHUB_STEP_SUMMARY` y comentarios de PR: números
      clave, issues con su evidencia, top de consumo y la tabla completa colapsada.
- [x] `--fail-on flaky,regression` y `--max-duration SEGUNDOS` (mediana de duración de
      build) como gates: salida con código 4, después de escribir el reporte.
- [x] Tabla de códigos de salida en el README.

**Bug encontrado al probar de punta a punta:** en Windows, con la salida redirigida a un
archivo o pipe (justo el caso de `>> $GITHUB_STEP_SUMMARY`), Python usa cp1252 y el emoji
tiraba la corrida con un error `charmap`. Los tests no lo veían porque pytest captura en
UTF-8. Ahora la salida es siempre UTF-8, con un test que simula ese stdout.

## 6. CI propio y publicación

- [x] Workflow con matriz Python 3.10–3.14 en Linux y Windows, con cobertura. Primer run
      verde ([37180942062](https://github.com/0xWhisp/ci-time-tracker/actions/runs/37180942062)):
      es la primera verificación real del soporte de 3.10 y de Windows.
- [x] Workflow de release por tag con trusted publishing; comprueba que tag, `pyproject` y
      `__version__` coincidan y que pasen los tests antes de publicar.
- [x] Demo en vivo: el CI corre la herramienta sobre su propio historial y deja el reporte
      Markdown en la página de resumen de cada run. Badge de CI en el README.
- [x] **Publicado en PyPI:** [`ci-time-tracker` 0.2.0](https://pypi.org/project/ci-time-tracker/0.2.0/),
      vía trusted publishing desde el tag `v0.2.0`. Verificado instalando en un entorno limpio
      y analizando el CI real del repo.
- [x] Lint con ruff: reglas fijadas en `pyproject.toml` (errores, bugbear, orden de imports,
      sintaxis moderna) y job en el CI. 35 hallazgos corregidos; los relevantes: 8 excepciones
      re-lanzadas sin su causa y un patrón de CircleCI definido pero nunca usado.

**Nota:** en el primer run el job del reporte falló de forma esperada: corre durante el
propio run y todavía no existía ningún run completado de `ci.yml` (sale con código 1, "No
workflow runs found"). Está marcado `continue-on-error`, así que no tumba el build.

## 7. GitHub Action para PRs

- [x] Modo `--run-id`: compara un run con la mediana de los últimos N runs del mismo workflow
      en la rama base (por defecto, la del repo), excluyendo el propio run. Tiempo total,
      tiempo de cómputo, costo y los pasos que cambiaron ≥25% y ≥10 s.
- [x] Fallos cruzados con la evidencia flaky de la rama base y de los re-runs del propio run:
      "conocido como flaky, un re-run puede pasar" vs. "no conocido como flaky".
- [x] Comentario único en el PR (se actualiza, no se duplica), con `--comment`; el PR se busca
      por el commit, porque `workflow_run` no lo informa en PRs desde forks.
- [x] `action.yml` compuesta + workflow `pr-impact.yml` con `workflow_run`: comenta en PRs de
      forks sin ejecutar su código. Los inputs pasan por variables de entorno (sin inyección
      de shell), con un test que lo verifica.
- [x] Gates en modo PR: `--fail-on regression` (algún paso más lento) y `flaky`.
- [ ] **Publicar en el Marketplace:** se hace al crear un release en GitHub (casilla
      "Publish this Action to the GitHub Marketplace"), después de mergear a `main`.

**Verificado con datos reales** contra el run 39102310449 de este repo. El workflow
`pr-impact.yml` empieza a comentar recién cuando esté en `main`: `workflow_run` solo usa la
copia de la rama por defecto.

**De paso:** la paginación de runs pedía una página extra después de una incompleta; ahora
corta ahí (una petición menos por listado).

## 8. Recomendaciones config × datos

- [x] Caché de dependencias: el job instala sin ninguna caché (la config lo muestra) **y** la
      instalación tarda una mediana ≥20 s (los datos lo miden).
- [x] Cancelar runs de PR obsoletos: el workflow no tiene `cancel-in-progress` y hubo runs que
      siguieron corriendo después de que un push nuevo lanzó otro.
- [x] Patas caras de la matriz: Windows/macOS que nunca fallaron cuando Linux pasó (≥10 builds).
- [x] Cada recomendación con evidencia, cambio concreto y tiempo/costo en juego (techo del
      ahorro, no promesa). Mínimo de 10 minutos en juego.
- [x] `--estimates` deprecado.
- [ ] Jobs paralelizables por `needs`: descartado. La config no distingue un `needs` que solo
      ordena de uno que usa los resultados del otro job; el riesgo de recomendar mal es alto.

**Prueba de campo** (7 repos, 100 runs, todas las ramas): pytest (patas de Windows: 68 h, 40% del
cómputo, sin fallos propios en 79 builds), cpython y este repo. Calibró tres cosas:

- Recomendaciones triviales (2,5 min en fastapi): mínimo de 10 minutos en juego.
- El costo en repos públicos era engañoso ("$45 en juego" en pytest, que no paga): los runners
  estándar pasan a $0 cuando los runs dicen que el repo es público.
- uv no se podía analizar: un run de 120 jobs hace que GitHub corte por timeout (502 persistente)
  con páginas de 100. Ahora se pagina más chico, y un run imposible se saltea con aviso.

## 9. GitLab y CircleCI por API

- Extraer una interfaz de proveedor (registro) y migrar GitHub a ella.
- GitLab: `/projects/:id/pipelines` y `/jobs`. CircleCI: Insights API.

## 10. Reporte HTML

- [x] `--format html`: una sola página, sin scripts ni recursos externos, modo claro/oscuro.
      Tarjetas de resumen, duración de builds por workflow (fallos marcados, mediana móvil),
      issues con evidencia y enlace al primer build lento, recomendaciones y dónde va el tiempo.
- [x] `timeline` por build en el JSON.

**Revisado renderizado** (pytest, 100 builds), lo que corrigió tres cosas que los tests no veían:
un build colgado de 3 h 19 m aplastaba la escala (ahora el eje se ajusta al p95 y los outliers
van como ▲ con su valor); los workflows mezclados en una línea hacían zigzag (ahora un gráfico
por workflow); y la mediana de build de la tarjeta mezclaba 26 workflows sin decirlo. Los tests
encontraron además un percentil mal calculado con pocos builds.
