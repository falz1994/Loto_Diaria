# Loto Diaria

Proyecto en Python para extraer, normalizar, analizar y generar predicciones sobre los sorteos de Loto Diaria.

El script principal es [extract_loto_diaria.py](extract_loto_diaria.py). Hace cuatro cosas:

1. Intenta leer el histórico desde la web de Loto.
2. Si falla, usa un HTML local como respaldo.
3. Guarda el histórico limpio en CSV y Excel.
4. Calcula análisis estadístico, evalúa predicciones pendientes y genera nuevas predicciones.

## Requisitos

Instala las dependencias con:

```powershell
pip install -r requirements.txt
```

## Uso básico

Ejecuta el extractor con:

```powershell
python extract_loto_diaria.py
```

Por defecto:

- intenta scrapear `https://loto.com.ni/diaria/`
- si no consigue datos, no genera salida
- guarda el resultado en `loto_diaria.csv`
- guarda el mismo contenido en `loto_diaria.xlsx`
- actualiza `loto_analysis_summary.csv` y `loto_analysis_numbers.csv`
- actualiza `predictions.csv` y `predictions_scores.csv`

Opciones útiles:

- `--csv`: archivo CSV de salida, por defecto `loto_diaria.csv`
- `--excel`: archivo Excel de salida, por defecto `loto_diaria.xlsx`
- `--url`: URL a intentar primero para el scrapeo
- `--no-lock`: desactiva el lockfile
- `--lock-file`: ruta del archivo de bloqueo
- `--lock-timeout`: segundos antes de considerar stale el lock
- `--log-file`: log adicional opcional

## Flujo del script

### 1. Obtención de datos

El script usa esta secuencia:

1. `fetch_records_from_loto_site()` para scrapear `https://loto.com.ni/diaria/`.
2. Si no devuelve datos, el script finaliza sin generar registros.

### 2. Normalización

Antes de guardar, el script:

- convierte fechas como `05 de Mayo 2026` a formato `M/D/YYYY`
- normaliza la hora a `HH:MM` o `HH:MM AM/PM`
- filtra solo filas de `Loto Diaria`
- limpia el número ganador y el número de sorteo
- elimina duplicados por `Sorteo`

### 3. Guardado

El histórico final se guarda en:

- `loto_diaria.csv`
- `loto_diaria.xlsx`

### 4. Análisis

Luego genera:

- `loto_analysis_summary.csv`
- `loto_analysis_numbers.csv`

Estos archivos contienen métricas por número, frecuencia, recencia, probabilidades bayesianas, heurísticas y señales de racha.

### 5. Predicciones

El script:

- evalúa predicciones pendientes cuando ya salió el sorteo correspondiente
- actualiza `predictions_scores.csv`
- genera nuevas predicciones para el siguiente sorteo

Cada método genera una lista de 10 números por sorteo, y esas listas se guardan en `predictions.csv`.

## Cómo funciona cada método de predicción

Los métodos usan como base el histórico convertido a números con `_prepare_numeric_df()` y el análisis por número de `_analyze_per_number()`.

### Métodos principales

| Método | Idea |
|---|---|
| `bucket_first_k2` | Divide el rango 0-99 en 2 bloques grandes y reparte los 10 números según el peso de cada bloque. |
| `bucket_first_auto` | Igual que el anterior, pero el número de bloques se calcula automáticamente con una heurística basada en `sqrt(histórico)`. |
| `group_streaks` | Decide si favorece 0-49 o 50-99 según racha y Bayes, y después elige los números mejor rankeados dentro de ese grupo. |
| `bucket_first_k2_20` | Igual que `bucket_first_k2`, pero genera 20 números. |
| `bucket_first_auto_20` | Igual que `bucket_first_auto`, pero genera 20 números. |
| `group_streaks_20` | Igual que `group_streaks`, pero genera 20 números. |

### Métodos por intervalo

Estos métodos usan bloques de tamaño fijo y dos variantes: determinista y aleatoria.

| Método | Idea |
|---|---|
| `interval_k10_det_10` | Usa bloques de 10 números. Elige el bloque mejor puntuado y toma los 10 números con mejor score. |
| `interval_k10_rand_10` | Usa bloques de 10 números, pero dentro del mejor bloque selecciona con ponderación por recencia + frecuencia. |
| `interval_k10_det_20` | Igual que `interval_k10_det_10`, pero devuelve 20 números. |
| `interval_k10_rand_20` | Igual que `interval_k10_rand_10`, pero devuelve 20 números. |
| `interval_k20_det_10` | Usa bloques de 20 números y selección determinista. |
| `interval_k20_rand_10` | Usa bloques de 20 números y selección aleatoria ponderada. |
| `interval_k20_det_20` | Igual que `interval_k20_det_10`, pero devuelve 20 números. |
| `interval_k20_rand_20` | Usa bloques de 20 números y selección aleatoria ponderada para devolver 20 números. |

## Qué mide `predictions_scores.csv`

El resumen por método incluye métricas de acierto y métricas financieras.

Columnas principales:

- `total_predictions`: cuántas veces se generó/evaluó ese método
- `number_hits`: cuántas veces acertó el número exacto
- `group_hits`: cuántas veces acertó el grupo 0-49 o 50-99
- `rank_sum`: suma de la posición del número acertado dentro de la lista
- `rank_hits_count`: cuántos aciertos tuvieron posición registrada
- `total_invertido`: inversión acumulada
- `rendimiento`: retorno acumulado por aciertos
- `total`: ganancia o pérdida neta
- `roi`: retorno sobre la inversión

### Fórmulas financieras

Se usa esta regla:

- cada número cuesta 5 córdobas
- cada acierto exacto paga 300 córdobas
- si no hay aciertos, el retorno es 0

Entonces:

```text
total_invertido = total_predictions * 10 * 5
rendimiento = number_hits * 300
total = rendimiento - total_invertido
roi = total / total_invertido
```

Si `total` es positivo, el método va en ganancia. Si es negativo, está perdiendo.

## Archivos generados

Los archivos que produce o consume el proyecto son:

- [extract_loto_diaria.py](extract_loto_diaria.py): script principal
- [loto_diaria.csv](loto_diaria.csv): histórico limpio
- [loto_diaria.xlsx](loto_diaria.xlsx): histórico en Excel
- [loto_analysis_summary.csv](loto_analysis_summary.csv): resumen estadístico general
- [loto_analysis_numbers.csv](loto_analysis_numbers.csv): métricas por número
- [predictions.csv](predictions.csv): predicciones por sorteo y método
- [predictions_scores.csv](predictions_scores.csv): resumen por método, con métricas financieras

## Detalles operativos

- El script usa un lockfile para evitar ejecuciones simultáneas.
- Los logs se escriben en `extract_loto_diaria.log` por defecto.
- La escritura de CSV usa reemplazo atómico para evitar archivos corruptos si se interrumpe el proceso.

## Ejecución programada

Si quieres ejecutarlo en Linux con `systemd` o `cron`, puedes usar una tarea diaria apuntando a `extract_loto_diaria.py`.

Ejemplo simple con `cron`:

```cron
0 6 * * * /home/loto/venv/bin/python /home/loto/Loto/extract_loto_diaria.py --csv /home/loto/Loto/loto_diaria.csv --excel /home/loto/Loto/loto_diaria.xlsx
```

## Nota final

El proyecto actual ya no depende de scripts de diagnóstico separados. La lógica importante está concentrada en [extract_loto_diaria.py](extract_loto_diaria.py), que combina scraping, análisis y scoring en un solo flujo.
