#!/usr/bin/env python3
"""dse.py - Motor de simulación paralelo + NSGA-II + búsqueda aleatoria (exploración del espacio de diseño de un Cortex-A76).

Uso:
  python3 dse.py import               # carga las 41 simulaciones manuales al results.csv
  python3 dse.py check                # 6 simulaciones reales; compara 3 con lo medido a mano
  python3 dse.py nsga --target 1000   # NSGA-II hasta 1000 simulaciones únicas nuevas
  python3 dse.py random --n 300       # línea base aleatoria
  python3 dse.py status               # resumen de results.csv
Opciones globales: --workers 6, --mock (simulador falso, para probar sin gem5).
El espacio de diseño y las semillas NO están en el código: se leen de config/espacio.csv y config/semillas.csv
(y las simulaciones manuales de config/manuales.txt)
Lo ya simulado se guarda en results.csv.
"""

import argparse, csv, hashlib, json, os, random, re, shutil, subprocess, sys, time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# Paths
HOME   = Path.home()
GEM5   = HOME / "gem5"
MCPAT  = HOME / "mcpat" / "mcpat"
CONV   = GEM5 / "scripts/McPAT/gem5toMcPAT_cortexA76.py"
XML    = GEM5 / "scripts/McPAT/ARM_A76_2.1GHz.xml"
SCRIPT = "scripts/CortexA76_scripts_gem5/CortexA76.py"
BIN    = GEM5 / "workloads/mp3_enc/mp3_enc"
WAV    = GEM5 / "workloads/mp3_enc/mp3enc_testfile.wav"
REPO   = Path(__file__).resolve().parent
RES    = REPO / "results"
SIMS   = GEM5 / "pruebas_lab1" / "dse"

# ESPACIO DE DISEÑO (se lee de config/*.csv)
# config/espacio.csv : parametro,defecto,valores,comentario   (valores separados por ';')
#                      El orden de las filas es el orden de las variables del optimizador.
# config/semillas.csv: una fila por semilla; columnas = parámetros (celda vacía = valor por defecto).
# Para otra carpeta de configuración:  DSE_CONFIG=/ruta/a/otra_config python3 dse.py 

CONFIG = Path(os.environ.get("DSE_CONFIG", REPO / "config"))

def load_space(path=None):
    path = Path(path or CONFIG / "espacio.csv")
    if not path.exists():
        sys.exit(f"No encuentro {path}. Copia la carpeta config/ junto a dse.py (o usa DSE_CONFIG).")
    space, default = {}, {}
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            space[r["parametro"].strip()] = [int(v) for v in r["valores"].split(";")]
            default[r["parametro"].strip()] = int(r["defecto"])
    return space, default

def load_seeds(path=None):
    path = Path(path or CONFIG / "semillas.csv")
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [{k: int(row[k]) if row.get(k, "").strip() else DEFAULT[k] for k in KEYS} for row in csv.DictReader(f)]

SPACE, DEFAULT = load_space() # SPACE: variable -> valores permitidos (el optimizador usa el índice)
KEYS = list(SPACE) # DEFAULT: valores del A76 por defecto
SEEDS = load_seeds() # puntos con los que arranca la población del NSGA-II

def full(p):            return {**DEFAULT, **p}
def phash(p):           return hashlib.sha1(json.dumps(full(p), sort_keys=True).encode()).hexdigest()[:10]
def decode(x):          return {k: SPACE[k][int(round(v))] for k, v in zip(KEYS, x)}
def encode(p):          return [SPACE[k].index(p[k]) for k in KEYS]

def cli_opts(p):
    """Solo pasa a gem5 lo que difiere del valor por defecto (igual que las simulaciones manuales)."""
    o = []
    for k, v in full(p).items():
        if k.startswith("x_"): # parámetros extra de las simulaciones manuales
            o.append(f"--{k[2:]}={v}")
        elif v == DEFAULT[k]:
            continue
        elif k == "width":
            o += [f"--{n}_width={v}" for n in ("fetch", "decode", "rename", "commit")]
        elif k.endswith("_kb"):
            o.append(f"--{k[:-3]}_size={v}kB")  # l1i_kb -> --l1i_size=32kB
        else:
            o.append(f"--{k}={v}")
    return o

# UNA SIMULACIÓN
def grab(pat, txt):
    m = re.search(pat, txt, re.M)
    return float(m.group(1)) if m else None

def mock_sim(p):
    q = full(p)
    cpi = 0.80 + 6.0 / q["iq_entries"] + (0.12 if q["width"] == 3 else 0) + \
          {3: 0.004, 4: 0.0005, 6: 0.0002, 8: 0}.get(q["issue_width"], 0) + 0.0004 * 64 / q["l1d_kb"]
    pw = 1.2 + 0.012 * q["iq_entries"] + 0.15 * q["width"] + 0.003 * q["rob_entries"] + \
         0.002 * (q["lq_entries"] + q["sq_entries"]) + 0.0005 * (q["l1i_kb"] + q["l1d_kb"]) + \
         0.0004 * q["l2_kb"] + 0.03 * q["issue_width"]
    return {"cpi": cpi, "leak": 0.3 * pw, "dyn": 0.7 * pw, "area": 30 + 0.01 * q["l2_kb"],
            "host": 1.0, "gem5_wall": 1.0, "mcpat": 0.1}

def run_sim_once(p, root, mock):
    if mock:
        time.sleep(0.01)
        return mock_sim(p)
    d = Path(root) / phash(p)
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    t0 = time.time()
    cmd = ["./build/ARM/gem5.fast", "-d", str(d), SCRIPT, "-c", str(BIN),
           "-o", f"{WAV} mp3enc_outfile.mp3", f"--cwd={d}"] + cli_opts(p) # misma invocación que `sim`
    with open(d / "salida.log", "w") as f:
        rc = subprocess.run(cmd, cwd=GEM5, stdout=f, stderr=subprocess.STDOUT).returncode
    t_gem5 = time.time() - t0
    if rc != 0 or not (d / "stats.txt").exists():
        return {"error": f"gem5 salió con código {rc} (ver {d}/salida.log)"}
    t1 = time.time()
    r = subprocess.run(["python3", str(CONV), "stats.txt", "config.json", str(XML)],
                       cwd=d, capture_output=True)
    if r.returncode != 0:
        return {"error": f"conversor falló: {r.stderr.decode(errors='replace')[-200:]}"}
    with open(d / "potencia.txt", "w") as f:
        r = subprocess.run([str(MCPAT), "-infile", "config.xml", "-print_level", "1"],
                           cwd=d, stdout=f, stderr=subprocess.STDOUT)
    t_mcpat = time.time() - t1                      # conversión + McPAT
    st = (d / "stats.txt").read_text(errors="replace")
    pw = (d / "potencia.txt").read_text(errors="replace")
    res = {"cpi": grab(r"^system\.cpu\.cpi\s+(\S+)", st), "host": grab(r"^hostSeconds\s+(\S+)", st),
           "leak": grab(r"Total Leakage = ([\d.]+)", pw), "dyn": grab(r"Runtime Dynamic = ([\d.]+)", pw),
           "area": grab(r"Area = ([\d.]+)", pw), "gem5_wall": t_gem5, "mcpat": t_mcpat}
    if any(res[k] is None for k in ("cpi", "leak", "dyn", "area")):
        return {"error": f"no pude leer las métricas en {d}"}
    (d / "params.json").write_text(json.dumps(full(p)))
    for x in d.iterdir():                           # conservar solo lo necesario
        if x.name not in ("stats.txt", "potencia.txt", "params.json"):
            shutil.rmtree(x) if x.is_dir() else x.unlink()
    return res

def run_sim(p, root, mock):
    for intento in (1, 2): # un reintento por si fue un fallo puntual
        try:
            res = run_sim_once(p, root, mock)
        except Exception as e: # noqa
            res = {"error": repr(e)}
        if "error" not in res:
            return res
    return res

# RESULTADOS (CACHÉ)
COLS = ["hash", "source", "name", "gen", "eval_idx"] + KEYS + \
       ["extras", "cpi", "ipc", "leakage_w", "dynamic_w", "power_w", "energy", "edp", "area_mm2",
        "gem5_host_s", "gem5_wall_s", "mcpat_s", "timestamp"]

class Store:
    def __init__(self, path):
        self.path, self.rows = Path(path), {}
        if self.path.exists():
            with open(self.path) as f:
                for r in csv.DictReader(f):
                    self.rows[self.key(r)] = r
    @staticmethod
    def key(r):
        # Las filas manuales
        return ("m:" if r["source"] == "manual" else "") + r["hash"]
    def add(self, row):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        nuevo = not self.path.exists()
        with open(self.path, "a", newline="") as f:
            w = csv.DictWriter(f, COLS)
            if nuevo:
                w.writeheader()
            w.writerow(row)
            f.flush(); os.fsync(f.fileno())
        self.rows[self.key(row)] = row
    def count(self, source):
        return sum(1 for r in self.rows.values() if r["source"] == source)
    def best(self, col):
        v = [r for r in self.rows.values() if r.get(col) not in (None, "")]
        return min(v, key=lambda r: float(r[col])) if v else None

def make_row(p, source, name, gen, idx, cpi, power, energy, edp, area, leak="", dyn="", host="", wall="", mc=""):
    q = full(p)
    extras = {k: v for k, v in q.items() if k.startswith("x_")}
    row = {"hash": phash(p), "source": source, "name": name, "gen": gen, "eval_idx": idx,
           "extras": json.dumps(extras) if extras else "", "cpi": cpi, "ipc": 1 / cpi,
           "leakage_w": leak, "dynamic_w": dyn, "power_w": power, "energy": energy, "edp": edp,
           "area_mm2": area, "gem5_host_s": host, "gem5_wall_s": wall, "mcpat_s": mc,
           "timestamp": datetime.now().isoformat(timespec="seconds")}
    row.update({k: q[k] for k in KEYS})
    return row

def evaluate_batch(store, plist, source, gen, args):
    """Devuelve [(cpi, energía)] en el mismo orden de plist. Simula solo lo que no está en la caché."""
    todo = {}
    for p in plist:
        h = phash(p)
        if h not in store.rows and h not in todo:
            todo[h] = p
    if todo:
        with ProcessPoolExecutor(args.workers) as ex:
            futs = {ex.submit(run_sim, p, args.root, args.mock): p for p in todo.values()}
            for i, fu in enumerate(as_completed(futs), 1):
                p = futs[fu]
                try:
                    res = fu.result()
                except Exception as e:             # noqa
                    res = {"error": repr(e)}
                if "error" in res:
                    print(f"  ! FALLÓ {phash(p)} {p}: {res['error']}", flush=True)
                    continue
                pw = res["leak"] + res["dyn"]
                en = pw * res["cpi"]
                store.add(make_row(p, source, "", gen, len(store.rows) + 1, res["cpi"], pw, en,
                                   en * res["cpi"], res["area"], res["leak"], res["dyn"],
                                   res["host"], round(res["gem5_wall"], 1), round(res["mcpat"], 1)))
                print(f"  [{i}/{len(todo)}] {phash(p)} cpi={res['cpi']:.6f} pot={pw:.4f} "
                      f"gem5={res['gem5_wall']:.0f}s mcpat={res['mcpat']:.0f}s", flush=True)
    out = []
    for p in plist:
        r = store.rows.get(phash(p))
        out.append((float(r["cpi"]), float(r["energy"])) if r else (1e3, 1e3))   # penalización si falló
    return out

# SIMULACIONES MANUALES 
# Se leen de config/manuales.txt (una por línea):
#   nombre ; cambios respecto al A76 por defecto (clave=valor, separados por coma) ; CPI ; potencia W ; energía ; EDP ; área mm2

def load_manual(path=None):
    path = Path(path or CONFIG / "manuales.txt")
    if not path.exists():
        sys.exit(f"No encuentro {path}.")
    manual = []
    for linea in path.read_text(encoding="utf-8").splitlines():
        if not linea.strip() or linea.lstrip().startswith("#"):
            continue
        nombre, cambios, cpi, pw, en, edp, area = [c.strip() for c in linea.split(";")]
        ov = {k.strip(): int(v) for k, v in (c.split("=") for c in cambios.split(",") if c.strip())}
        manual.append((nombre, ov, float(cpi), float(pw), float(en), float(edp), float(area)))
    return manual

def cmd_import(store, args):
    nuevos = 0
    for name, ov, cpi, pw, en, edp, area in load_manual():
        h = "m:" + phash(ov)
        if h in store.rows:
            print(f"  = {name}: misma configuración que '{store.rows[h]['name']}', se omite")
            continue
        store.add(make_row(ov, "manual", name, -1, len(store.rows) + 1, cpi, pw, en, edp, area))
        nuevos += 1
    print(f"Importadas {nuevos} simulaciones manuales. Total en {store.path.name}: {len(store.rows)}")

# CHECK
def cmd_check(args):
    """6 simulaciones reales. Las 3 primeras ya se midieron a mano: deben dar el mismo CPI."""
    args.root = str(SIMS.parent / "dse_check")
    path = RES / ("results_check_mock.csv" if args.mock else "results_check.csv")
    path.unlink(missing_ok=True)
    store = Store(path)
    plist = [S(), S(issue_width=3), S(issue_width=6), S(rob_entries=64), S(l1d_kb=32), S(iq_entries=48)]
    esperado = [0.847317, 0.850355, 0.847151]
    t0 = time.time()
    evaluate_batch(store, plist, "check", 0, args)
    print(f"\nTiempo total de las 6 en paralelo: {time.time() - t0:.0f} s")
    print(f"{'config':26s}{'CPI':>10s}{'esperado':>10s}  resultado")
    for i, p in enumerate(plist):
        r = store.rows.get(phash(p))
        if not r:
            print(f"{str(i):26s}  FALLÓ"); continue
        etq = f"issue={p['issue_width']} rob={p['rob_entries']} l1d={p['l1d_kb']} iq={p['iq_entries']}"
        if i < 3:
            ok = abs(float(r["cpi"]) - esperado[i]) < 6e-7
            print(f"{etq:26s}{float(r['cpi']):10.6f}{esperado[i]:10.6f}  {'IDÉNTICO' if ok else 'DIFERENTE'}")
        else:
            print(f"{etq:26s}{float(r['cpi']):10.6f}{'-':>10s}  nuevo (edp={float(r['edp']):.5f})")

# NSGA-II
def cmd_nsga(store, args):
    import numpy as np
    from pymoo.algorithms.moo.nsga2 import NSGA2
    from pymoo.core.evaluator import Evaluator
    from pymoo.core.problem import Problem
    from pymoo.operators.crossover.sbx import SBX
    from pymoo.operators.mutation.pm import PM
    from pymoo.operators.repair.rounding import RoundingRepair
    from pymoo.problems.static import StaticProblem

    n = len(KEYS)
    xu = np.array([len(SPACE[k]) - 1 for k in KEYS])
    problem = Problem(n_var=n, n_obj=2, xl=np.zeros(n), xu=xu.astype(float), vtype=int)
    rng = np.random.RandomState(args.seed)
    X0 = [encode(s) for s in SEEDS]
    while len(X0) < args.pop:
        X0.append(list(rng.randint(0, xu + 1)))
    algo = NSGA2(pop_size=args.pop, sampling=np.array(X0, dtype=float), eliminate_duplicates=True,
                 crossover=SBX(prob=0.9, eta=3, vtype=float, repair=RoundingRepair()),
                 mutation=PM(prob=0.2, eta=3, vtype=float, repair=RoundingRepair()))
    algo.setup(problem, termination=("n_gen", 10 ** 6), seed=args.seed, verbose=False)
    print(f"NSGA-II: población {args.pop}, semilla {args.seed}, objetivo {args.target} simulaciones nuevas "
          f"(ya hay {store.count('nsga')}), {args.workers} en paralelo", flush=True)
    gen = 0
    while True:
        pop = algo.ask()
        gen += 1
        plist = [decode(x) for x in pop.get("X")]
        antes = store.count("nsga")
        t0 = time.time()
        F = np.array(evaluate_batch(store, plist, "nsga", gen, args))
        Evaluator().eval(StaticProblem(problem, F=F), pop)
        algo.tell(infills=pop)
        b = store.best("edp")
        print(f"[gen {gen}] nuevas={store.count('nsga') - antes} únicas_nsga={store.count('nsga')}/{args.target} "
              f"mejor_EDP={float(b['edp']):.5f} ({time.time() - t0:.0f} s)", flush=True)
        if store.count("nsga") >= args.target:
            break
    print("NSGA-II terminado.")

# ALEATORIA 
def cmd_random(store, args):
    rng = random.Random(args.seed)
    vistos, orden = set(), []
    while len(orden) < args.n:
        idx = tuple(rng.randrange(len(SPACE[k])) for k in KEYS)
        if idx not in vistos:
            vistos.add(idx)
            orden.append({k: SPACE[k][i] for k, i in zip(KEYS, idx)})
    B = 30
    log = RES / ("random_log_mock.csv" if args.mock else "random_log.csv")
    for i in range(0, len(orden), B):
        evaluate_batch(store, orden[i:i + B], "random", i // B + 1, args)
        with open(log, "w", newline="") as f:      # orden de las propuestas aleatorias (para las curvas)
            w = csv.writer(f); w.writerow(["idx", "hash"])
            for j, p in enumerate(orden[:i + B]):
                w.writerow([j + 1, phash(p)])
        print(f"[aleatoria] {min(i + B, len(orden))}/{len(orden)}", flush=True)

# ESTADO
def cmd_status(store, args):
    print(f"Archivo: {store.path}  |  filas: {len(store.rows)}")
    for s in ("manual", "nsga", "random"):
        print(f"  {s:7s}: {store.count(s)}")
    for col in ("cpi", "energy", "edp"):
        b = store.best(col)
        if b:
            print(f"  mejor {col:6s}: {float(b[col]):.6f}  ({b['name'] or b['hash']}, fuente {b['source']})")
    ts = [r for r in store.rows.values() if r["source"] in ("nsga", "random") and r["gem5_wall_s"] not in ("", None)]
    if ts:
        g = sum(float(r["gem5_wall_s"]) for r in ts) / len(ts)
        m = sum(float(r["mcpat_s"]) for r in ts) / len(ts)
        print(f"  tiempo medio por simulación: gem5 {g:.0f} s, conversión+McPAT {m:.0f} s")

# MAIN
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["import", "check", "nsga", "random", "status"])
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--mock", action="store_true", help="simulador falso (no usa gem5)")
    ap.add_argument("--target", type=int, default=1000, help="nsga: simulaciones nuevas a alcanzar")
    ap.add_argument("--pop", type=int, default=48, help="nsga: tamaño de población (múltiplo de 6 = rondas completas)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--n", type=int, default=300, help="random: número de configuraciones")
    args = ap.parse_args()
    args.root = str(SIMS)
    if args.cmd == "check":
        return cmd_check(args)
    store = Store(RES / ("results_mock.csv" if args.mock else "results.csv"))
    {"import": cmd_import, "nsga": cmd_nsga, "random": cmd_random, "status": cmd_status}[args.cmd](store, args)

if __name__ == "__main__":
    main()
