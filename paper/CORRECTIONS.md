# Corrections to the IEEE paper draft

The draft (`IEEE_Paper-3.pdf`) was checked claim by claim against the code on `main` and against
a rerun of its experiments. This file lists what was wrong and what `main.tex` now says. Code
references are to `src/oran_adapt/`.

## A. Method claims that did not match the code

| # | Draft said | The code does | Evidence |
|---|---|---|---|
| 1 | Similar model exchange compares the current dataset with historical datasets and swaps in the model whose recorded environment matches | Re-scores up to 10 registered versions (and LIVE) on the held-out newest slice of the current data; promotes one that cuts LIVE's RMSE by at least 5 % (or +0.02 accuracy). No dataset-similarity test exists | `analysis/version_eval.py`, `analysis/reuse_decision.py`, `core/config.py` `reuse_*` |
| 2 | The readapter bypasses / replaces drift detection; PSI is computed only after the exchange fails | The framework acts on a reported degradation (it has no drift detector), and computes KS and PSI *before* the exchange, in the analysis stage | `orchestrator/pipeline.py` (analyze, then evaluate_versions, then decide_reuse) |
| 3 | Finetune uses LoRA, partial-freeze or full fine-tuning; partial-freeze adapters exist | Neither LoRA nor partial freezing is implemented anywhere. Finetune is a warm start: `partial_fit` (scikit-learn) or continued Adam from the current weights with all parameters trainable (PyTorch) | `adaptation/finetune.py`, `adaptation/torch_engine.py` |
| 4 | MLKTL module "selects datasets, configures partial-freeze fine-tuning", "selecting baseline checkpoints" | No such behaviour. The PSI/KS drift summary is `analysis/summary.py`; PSI ≥ 0.5 rules out fine-tuning in `decision/constraints.py` | as cited |
| 5 | The selected action "optimizes the utility given Q, S_data and C" | No utility is optimized. Hard constraints remove actions; an optional LLM picks among the rest; otherwise a fixed preference order (fine-tune, retrain, rollback, no action) | `decision/constraints.py`, `decision/engine.py`, `decision/fallback.py` |
| 6 | Action set {Finetune, Regenerate, Rollback} | Four actions, plus No Action; the constraints can also stop a job as insufficient information or no compatible strategy | `core/enums.py` `Strategy` |
| 7 | Rollback is an executed remediation | A ROLLBACK decision ends the job as NO_ACTION and keeps LIVE; re-promotion of an older version happens only in the similar model exchange | `orchestrator/pipeline.py` `_ADAPTING_STRATEGIES` |
| 8 | "Specific engine, else Generic Model Adapter, else LLM" | Model-type plugin's native engine; a Finetune without one falls back to a full retrain; only then LLM-written code. "Generic Model Adapter" is the model-type plugin (9 registered) | `orchestrator/pipeline.py` `_produce_candidate`, `pyproject.toml` entry points |
| 9 | "Only the Engine Selection Agent calls the LLM" | The Decision Engine calls it too (single prompt, or the tool-calling agent) | `decision/llm_selector.py`, `decision/agent.py` |
| 10 | "The API key that is set selects the provider" | `LLM_PROVIDER` selects it (Gemini by default since PR #11); without a key every decision takes the deterministic path | `core/config.py`, `bootstrap.py` |
| 11 | "Per-stage deadlines" | One optional per-job deadline (`JOB_DEADLINE_S`, off by default) | `core/config.py`, `orchestrator/worker.py` |
| 12 | PSI formula without its binning | Evidently: Sturges bins over the pooled sample, empty bins floored at 1e-4 (why PSI exceeds 15) | `analysis/comparison.py` |
| 14 | Intake checks that the model exists in the MLflow registry (stated in an earlier version of `main.tex`) | Intake validates the schema, removes duplicates and queues; an unknown model fails the job in the first worker stage with `MODEL_NOT_FOUND` | `api/routes_adaptation.py:33`, `orchestrator/jobs.py:368`, `analysis/retrieval.py:170` |
| 13 | Gate only | The gate also applies configurable guardrails (slices, calibration, latency, size) | `validation/gate.py` |

Verified as stated: 33 528 lines in 186 modules; 961 tests (939 passed, 3 failed, 19 skipped in
CI run 86); 39 REST endpoints; 16 live job states; the PSI formula; the gate rule
(lower bound > 0, 95 %, 1000 paired resamples); the AST whitelist and sandbox.

## B. Results that could not be reproduced

The draft says its results "regenerate exactly from the published scripts", but no such script
was in the repository and none of its numbers appear anywhere in it. The experiment was rebuilt
from the paper's own description in `scripts/paper_experiments.py`, running the framework's own
code, and every number and figure was replaced with its output (`paper/results.json`). The
qualitative findings hold; the values changed:

| Finding | Draft | Rerun |
|---|---|---|
| Gain under concept drift (S3–S7) | 75.29–97.99 % | 62.49–97.50 % |
| Covariate shift only (S1–S2) | remediation "costs up to 1.37 %" | changes RMSE by under 1 % (+0.52, +0.63 %); S0 −2.29 % |
| Max PSI | up to 12.43 | up to 16.09 |
| Regenerate vs Finetune at S7 | 13.0 % lower | 46.0 % lower |
| Gate: accepted small gain vs rejected larger point estimate | +0.00117 vs −0.00141 | +0.00084 (100 % > 0) vs −0.00390 (10.0 % > 0) |
| Cost | gate 92 % of 191 ms, drift analysis 4.6 ms | drift analysis 55 % (213 ms), gate 43 % (167 ms) of 385 ms |
| "A fixed-tolerance check would have promoted it" | as stated | made precise: a non-inferiority check with tolerance 0.004 |

## C. Presentation

- IEEEtran conference class; the authors' roll numbers replaced by an affiliation block.
  **The affiliation is a placeholder** (`\affil` at the top of `main.tex`).
- Undefined acronym MLKTL removed from the text and index terms (its expansion is not given
  anywhere in the draft or the code).
- Fig. 1 redrawn (TikZ) in the real stage order; Figs. 2–5 regenerated from the rerun; the
  pie and Gantt chart replaced by one bar chart with error bars.
- Sections merged: one-paragraph sections folded into a standard structure; the contributions
  are listed in the introduction.
- All 19 references were checked (authors, title, venue, volume, issue, pages, year) and are
  correct; they are formatted in IEEE style.

## D. Rebuild

```bash
python scripts/paper_experiments.py --out paper
python paper/make_figures.py paper/results.json paper/figures
cd paper && latexmk -pdf main.tex      # or push: the paper workflow builds and commits main.pdf
```
