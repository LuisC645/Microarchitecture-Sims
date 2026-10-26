# Exploración del espacio de diseño de un Cortex-A76 (gem5 + McPAT)

Estudio de cómo los parámetros microarquitectónicos de un Cortex-A76 afectan su rendimiento (CPI) y su energía con la aplicación `mp3_enc`.
Se exploraron 1330 simulaciones con NSGA-II (1004) y una búsqueda aleatoria de control (300).
La mejor configuración por EDP lo reduce un 26 % respecto al A76 por defecto.

**Informe completo:** [`Lab1_DSE_CortexA76.ipynb`](Lab1_DSE_CortexA76.ipynb)

## Estructura
```
Lab1_DSE_CortexA76.ipynb   informe y análisis (lee results/)
dse.py                     motor del DSE: simulaciones en paralelo, NSGA-II y búsqueda aleatoria
config/                    espacio.csv, semillas.csv y manuales.txt (lo que lee dse.py)
scripts/                   sim.sh (una simulación a mano) y gem5toMcPAT_cortexA76.py (conversor gem5 -> McPAT)
results/                   results.csv, random_log.csv y registros de runs
figuras/                   figuras del informe
workloads/
test_lab/
requirements.txt

```

## Reproducir el análisis
```bash
pip install -r requirements.txt
jupyter lab Lab1_DSE_CortexA76.ipynb     # ejecutar todo con RUN_SIMS = False
```
