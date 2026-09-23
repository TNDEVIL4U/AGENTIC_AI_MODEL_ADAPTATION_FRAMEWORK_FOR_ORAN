# Demo Manual — running the multi-model adaptation demo

This manual walks through `scripts/demo_models.py`, the end-to-end demo of the framework:
model registry + data versioning + drift analysis + strategy decision + adaptation +
validation + registration. Every command below was run on this project's laptop
(Windows 11, 16 GB RAM, no GPU) on 2026-09-23; the "expected output" blocks are copied from
that real run.

- CPU only, about **200 rows per dataset**, no Docker, **no LLM API key needed**.
- Takes about **2 minutes** in the default mode (measured on this laptop).
- Each run writes to a **new** folder `data/demo_models/run-<UTC time>/`. In the default mode,
  MLflow also stores the model files under `mlruns/0/` in the project root (git-ignored).
  Nothing is deleted.
- Exit code `0` = every check passed, `1` = at least one check failed.

---

## 1. One-time setup

From the project root, in PowerShell:

```powershell
python -m venv .venv                 # skip if .venv already exists
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"              # installs the package + test tools into the venv only
```

In Git Bash, activate with `source .venv/Scripts/activate` instead.

Check the install:

```powershell
python -c "import oran_adapt, mlflow, torch, xgboost; print('ok')"
```

Expected output:

```
ok
```

No `.env` file is needed: the demo builds its own settings (`LLM_PROVIDER=none`, SQLite
database, SQLite or local-server MLflow) and ignores `.env`.

---

## 2. Run the demo

Pick **one** of the three modes. Only run one at a time — the laptop is low on memory.

| Mode | Command | MLflow used | When to use |
|---|---|---|---|
| Default (lightest) | `python scripts/demo_models.py` | SQLite file in the run folder | First run / quick check |
| Local server | `python scripts/demo_models.py --start-server` | Real `mlflow server` on `http://127.0.0.1:5000`, started and stopped by the demo | To show a real MLflow server + UI |
| Existing server | `python scripts/demo_models.py --tracking-uri http://127.0.0.1:5000` | A server you started yourself with `python scripts/run_mlflow_server.py` | To browse results in the MLflow UI afterwards |

Use `--port 5001` with `--start-server` if port 5000 is busy.

To keep a copy of the output:

```powershell
python scripts/demo_models.py *> demo_output.txt
```

---

## 3. Expected output, section by section

The top of the output contains MLflow log lines printed while models are saved. They are
normal and **not errors**:

```
INFO mlflow.store.db.utils: Creating initial MLflow database tables...
UserWarning: Hint: Inferred schema contains integer column(s). ...
WARNING mlflow.pytorch: Saving pytorch model by Pickle or CloudPickle format requires exercising caution ...
Successfully registered model 'du_anomaly_sgd_102819'.
Created version '1' of model 'du_anomaly_sgd_102819'.
...
Registered model 'du_anomaly_sgd_102819' already exists. Creating a new version of this model...
Created version '2' of model 'du_anomaly_sgd_102819'.
```

The `_102819` suffix is the run's UTC time (HHMMSS), so it differs every run. It keeps model
names unique when several runs share one MLflow server.

### Section 1 — database migration and readiness

```
==============================================================================
1. Migrating the database and starting the API
==============================================================================
Run dir: C:\AGENTIC AI MODEL ADAPTATION FRAMEWORK FOR ORAN ARCHITECTURE\data\demo_models\run-20260923-102819
MLflow:  sqlite:///C:/AGENTIC AI MODEL ADAPTATION FRAMEWORK FOR ORAN ARCHITECTURE/data/demo_models/run-20260923-102819/mlflow.db
GET /api/v1/ready -> 200 {'ready': True, 'components': [{'name': 'database', 'ok': True, 'detail': None}, {'name': 'mlflow', 'ok': True, 'detail': None}]}
```

**What to check:** `-> 200` and `'ready': True`. With `--start-server`, the `MLflow:` line
shows `http://127.0.0.1:5000` and the output begins with
`Started MLflow server at http://127.0.0.1:5000 (it is stopped when the demo ends)`.

### Section 2 — onboarding eight models

Each model is trained on synthetic KPI data (`prb_util`, `cqi`, `rsrp`), registered in MLflow
as version 1 with the `live` alias, and its training data (`hist-1`) and drifted data
(`drift-1`) are stored as content-hashed data versions.

```
==============================================================================
2. Onboarding the demo models (MLflow v1 + versioned training/drift data)
==============================================================================
  du-anomaly-sgd         SGDClassifier          -> MLflow v1  data hist-1#8c21c30698 (200 rows), drift-1 (200 rows, shift 0.4)
  cell-throughput-ridge  Ridge                  -> MLflow v1  data hist-1#04767dc4eb (200 rows), drift-1 (200 rows, shift 3.0)
  site-load-rf           RandomForestRegressor  -> MLflow v1  data hist-1#7ecd712aca (200 rows), drift-1 (200 rows, shift 3.0)
  handover-xgb           XGBClassifier          -> MLflow v1  data hist-1#60b1a6bdd1 (200 rows), drift-1 (200 rows, shift 3.0)
  beam-mlp-torch         BeamMLP                -> MLflow v1  data hist-1#ce8177b7fe (200 rows), drift-1 (200 rows, shift 0.4)
  energy-mlp-torch       EnergyMLP              -> MLflow v1  data hist-1#0801987d29 (200 rows), drift-1 (200 rows, shift 3.0)
  kpi-stable-sgd         SGDClassifier          -> MLflow v1  data hist-1#46ff4cb8d7 (200 rows), drift-1 (200 rows, shift 0.0)
  sparse-site-sgd        SGDClassifier          -> MLflow v1  data hist-1#9ea1449e1f (200 rows), drift-1 (5 rows, shift 3.0)
```

**What to check:** all eight say `MLflow v1`. The `#8c21c30698`-style hashes may differ
between runs (row timestamps are part of the hash).

### Section 3 — one drift event per model

Each model gets one `POST /api/v1/adaptation/events`. The eight models are built so that each
one takes a different path through the pipeline:

| Model | Scenario | Expected strategy | Expected engine | Expected outcome | `live` after |
|---|---|---|---|---|---|
| du-anomaly-sgd | mild drift, sklearn `partial_fit` | FINE_TUNING | SKLEARN_PARTIAL_FIT | REGISTERED | v2 |
| cell-throughput-ridge | strong drift, regressor | FULL_RETRAINING | SKLEARN_FULL_RETRAIN | REGISTERED | v2 |
| site-load-rf | strong drift, RandomForest | FULL_RETRAINING | SKLEARN_FULL_RETRAIN | REGISTERED | v2 |
| handover-xgb | strong drift, XGBoost | FULL_RETRAINING | XGBOOST_FULL_RETRAIN | REGISTERED | v2 |
| beam-mlp-torch | mild drift, torch classifier | FINE_TUNING | TORCH_FINE_TUNE | REGISTERED | v2 |
| energy-mlp-torch | strong drift, torch regressor | FULL_RETRAINING | TORCH_FULL_RETRAIN | REGISTERED | v2 |
| kpi-stable-sgd | caller says no drift | None | None | NO_ACTION | v1 |
| sparse-site-sgd | only 5 drifted rows | INSUFFICIENT_INFORMATION | None | NO_ACTION | v1 |

Real output for three representative models (the other five follow the same format):

```
--- du-anomaly-sgd: sklearn SGDClassifier, mild drift -> partial_fit fine-tuning
  HTTP 201  status=COMPLETED  strategy=FINE_TUNING
  outcome=REGISTERED  engine=SKLEARN_PARTIAL_FIT
  validation: candidate accuracy=1.0000 vs current accuracy=1.0000 (tolerance 0.0200) -> PASS
  reload check: v2 predicts [0, 1, 0, 1, 0]
  training data frozen as: train-du-anomaly-sgd-v2
  live alias -> v2
  verdict: PASS (as expected)

--- site-load-rf: sklearn RandomForest (skops-trusted tree types), strong drift -> full retraining
  HTTP 201  status=COMPLETED  strategy=FULL_RETRAINING
  outcome=REGISTERED  engine=SKLEARN_FULL_RETRAIN
  validation: candidate rmse=0.4261 vs current rmse=3.0262 (tolerance 0.1513) -> PASS
  reload check: v2 predicts [7.231, 4.866, 1.588, 6.581, 6.694]
  training data frozen as: train-site-load-rf-v2
  live alias -> v2
  verdict: PASS (as expected)

--- sparse-site-sgd: control: only 5 drifted rows -> INSUFFICIENT_INFORMATION, nothing trained
  HTTP 201  status=COMPLETED  strategy=INSUFFICIENT_INFORMATION
  outcome=NO_ACTION  engine=None
  live alias -> v1
  verdict: PASS (as expected)
```

Validation lines measured for the other models in the same run:

| Model | Candidate | Current | Tolerance | Result |
|---|---|---|---|---|
| cell-throughput-ridge | rmse 0.1092 | rmse 0.1133 | 0.0057 | PASS |
| handover-xgb | accuracy 1.0000 | accuracy 0.8000 | 0.0200 | PASS |
| beam-mlp-torch | accuracy 1.0000 | accuracy 1.0000 | 0.0200 | PASS |
| energy-mlp-torch | rmse 0.1932 | rmse 0.3712 | 0.0184 | PASS |

**What to check:** every block ends in `verdict: PASS (as expected)`. The `reload check`
line proves the new version was downloaded back from MLflow through the `live` alias and
produced real predictions.

> **How validation works:** the newest 20% of the drifted rows (40 of 200,
> `VALIDATION_HOLDOUT_FRACTION=0.2`) are held back. The candidate is trained on the other
> 360 rows, and both the candidate and the current model are scored only on those 40 unseen
> rows. The classifiers reach accuracy 1.0000 because the demo's labels follow an exact
> linear rule with no noise, so a linear model can learn them perfectly.

### Section 4 — idempotency

The first event is sent again with the same `event_id`:

```
==============================================================================
4. Idempotency: resubmitting one event
==============================================================================
POST (same event_id) -> 200, duplicate=True
  verdict: PASS
```

**What to check:** `200` (not `201`) and `duplicate=True` — the job is not run twice.

### Section 5 — lineage between model versions and data versions

For each of the six registered models:

```
--- lineage of cell-throughput-ridge
  MLflow v2 tags: data.training_version=train-cell-throughput-ridge-v2 adaptation.engine=SKLEARN_FULL_RETRAIN data.source_versions=hist-1,drift-1
  data train-cell-throughput-ridge-v2: rows=360 held-out=40 hash=174c753ff9 ancestors=['hist-1'] linked=[('2', 'TRAINING')]
  verdict: PASS (MLflow <-> data lineage consistent)
```

**What to check:** `rows=360` (200 historical + 160 drifted — the 40 newest drifted rows
were held out for validation and are not part of the training data), `held-out=40`,
`ancestors=['hist-1']`,
`linked=[('2', 'TRAINING')]`, and `PASS`.

### Section 6 — second drift cycle

New drifted data (`drift-2`) is posted through the data API, and a second event is run for
`cell-throughput-ridge`:

```
--- second drift cycle for cell-throughput-ridge (fresh data via the data API)
  POST drift-2 -> 201, identical replay -> 200, different content same name -> 409
  analysis baseline now: train-cell-throughput-ridge-v2  (was hist-1 in cycle 1)  -> significant statistical shift in: cqi, prb_util, target
  cycle-2 job: status=COMPLETED strategy=FULL_RETRAINING outcome=REGISTERED live -> v3
  verdict: PASS (as expected)
```

**What to check:**
- `201 / 200 / 409`: new version created, identical replay is a no-op, reusing a version name
  for different content is refused (`DATA_VERSION_CONFLICT`).
- The baseline is now `train-cell-throughput-ridge-v2` (the adapted model's training data),
  not the stale `hist-1`.
- `live -> v3`.

On Windows the MLflow line `Registered model ... already exists. Creating a new version...`
can appear in the middle of the `analysis baseline` line because both write to the console at
once. That is only cosmetic.

### Summary — the final result

```
==============================================================================
Summary
==============================================================================
check                         framework strategy                  outcome     verdict
du-anomaly-sgd                sklearn   FINE_TUNING               REGISTERED  PASS
cell-throughput-ridge         sklearn   FULL_RETRAINING           REGISTERED  PASS
site-load-rf                  sklearn   FULL_RETRAINING           REGISTERED  PASS
handover-xgb                  xgboost   FULL_RETRAINING           REGISTERED  PASS
beam-mlp-torch                torch     FINE_TUNING               REGISTERED  PASS
energy-mlp-torch              torch     FULL_RETRAINING           REGISTERED  PASS
kpi-stable-sgd                sklearn   None                      NO_ACTION   PASS
sparse-site-sgd               sklearn   INSUFFICIENT_INFORMATION  NO_ACTION   PASS
idempotent-replay             -         -                         -           PASS
lineage:du-anomaly-sgd        sklearn   -                         -           PASS
lineage:cell-throughput-ridge sklearn   -                         -           PASS
lineage:site-load-rf          sklearn   -                         -           PASS
lineage:handover-xgb          xgboost   -                         -           PASS
lineage:beam-mlp-torch        torch     -                         -           PASS
lineage:energy-mlp-torch      torch     -                         -           PASS
cycle2:cell-throughput-ridge  sklearn   FULL_RETRAINING           REGISTERED  PASS

16/16 checks passed
```

**Success = `16/16 checks passed` and exit code 0.** Check the exit code with
`echo $LASTEXITCODE` (PowerShell) or `echo $?` (Git Bash).

---

## 4. What stays the same and what changes between runs

| Same every run | Changes every run |
|---|---|
| Strategies, engines, outcomes, `live` versions | Run folder name and the `_HHMMSS` model-name suffix |
| Row counts (200, 5, 360, 40 held out) | Data hashes (timestamps are part of the hash) |
| `16/16 checks passed` | Timestamps in the MLflow log lines |
| Metrics and predictions (fixed random seeds) — may differ in the last decimals with other library versions | Total run time |

---

## 5. Looking at the results afterwards

One run's files live in `data/demo_models/run-<time>/`:

| File / folder | Contents |
|---|---|
| `app.db` | Framework database: jobs, model metadata, data versions, lineage links |
| `mlflow.db` | MLflow runs, registered models, versions, aliases, tags (default mode only) |
| `work/` | Per-job working folders: the current model downloaded from MLflow for adaptation |
| `reload-<model>-v2/` | Models downloaded back through the `live` alias for the reload check |

In default mode the MLflow model files themselves go to `mlruns/0/` in the project root,
shared by all default-mode runs. With `--start-server`, they go to the server's folder
`data/mlflow_server/`.

To browse a default-mode run in the MLflow UI (run this yourself; stop it with Ctrl+C):

```powershell
mlflow ui --backend-store-uri "sqlite:///data/demo_models/run-20260923-102819/mlflow.db" --port 5000
```

Then open `http://127.0.0.1:5000`. Use your own run folder name.

Old run folders are **not** removed automatically. Delete them yourself from File Explorer
when you no longer need them.

---

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `port 5000 is already in use - not starting a second server` | Something already uses port 5000. Use `--start-server --port 5001`, or `--tracking-uri` to point at the server that is already running. |
| `ModuleNotFoundError: No module named 'oran_adapt'` / `torch` / `xgboost` | The virtual environment is not active, or setup was skipped. Re-run section 1. |
| `UnicodeEncodeError` when redirecting output | Should not happen: the script switches the console to UTF-8. If it does, set `$env:PYTHONIOENCODING="utf-8"` in that PowerShell window only. |
| Final line says `15/16 checks passed` (or fewer) | Scroll up to the `verdict: FAIL (...)` line — the text in brackets says exactly which expectation was missed (strategy, outcome, engine or live version). |
| Run is very slow or the laptop freezes | Close other heavy programs; run the default mode, not `--start-server`; never run the demo and the test suite at the same time. |
| `UserWarning: Hint: Inferred schema contains integer column(s)` / pickle warning | Normal MLflow messages, not errors. |

---

## 7. Related commands

- Original single-model demo: `python scripts/demo.py`
- Standalone MLflow server: `python scripts/run_mlflow_server.py [--port 5000]`
- The same steps from the command line: `oran-adapt --help` (see `docs/MANUAL.md`)
- CLI end-to-end test: `python -m pytest tests/unit/test_phase13_cli.py -q`
