# Pantry Path data pipeline: design spec

**Date:** 2026-10-24 · **Due:** 2026-10-26 · **Owner:** Ansh (data pipeline only)
**Background:** the design decisions were made in a planning session on 2026-10-19. Their reasons are summarized in `docs/data-pipeline-report.md` (Decisions). This spec turns them into files, functions, checks and tests. What was finally built, with results, is in that report and `data-pipeline/README.md`.

## 1. Goal and scope

Build two pipelines that share one codebase:

1. **`build_reference_data`** (weekly Airflow DAG). Builds the reference data a request needs:
   - a **dish catalog** of 100 dishes with ingredients, roles (core/common) and cuisine
   - an **Open Food Facts snapshot** of US products with allergen evidence, looked up by barcode

   It also validates both, computes stats and drift, checks bias, sends alerts and versions the outputs with DVC.
2. **Request flow** (terminal command; a DAG only if time allows). Input: dish, zip, avoid list, diet. Output: `result.json` with ingredients, nearby stores and allergen-checked products.

**Out of scope:** model training and the final user-facing response (that belongs to a teammate), quantities and units, prices, medical, religious and certification diets, and storing any Kroger data.

**Priorities given the deadline:**
- **Must:** pieces 1–10 in section 11, covering all 12 grading criteria plus the terminal request command.
- **Should:** the `serve_request` DAG and the Target fallback.
- **Could:** CI and per-state store providers.

## 2. Layout

```
shared/                          # installable package: pip install -e shared/
  pyproject.toml
  pantrypath_core/
    schema.py                    # moved from pipelines/ (Recipe, Ingredient, Unit, Category)
    catalog.py                   # DishProfile, CatalogIngredient, Catalog (no quantities)
    diet_rules.py                # allergen + diet verdicts (ported from github_lab1, bacon fix)
    barcodes.py                  # GTIN-13 normalization, Kroger UPC -> GTIN-13
    result.py                    # result.json v1.0 pydantic models
  tests/                         # model, diet-rule, barcode and golden-set tests
data-pipeline/
  README.md                      # setup, run, structure, DVC, reproducibility
  docker-compose.yaml            # Airflow 3.3, LocalExecutor + Postgres
  Dockerfile                     # airflow image + a separate pipeline venv
  dvc.yaml                       # declares reports/stats.json as a DVC metric
  requirements.in / .txt         # top-level deps / pinned universal lock (uv pip compile)
  pyproject.toml                 # package + ruff + pytest config
  config/pipeline.yaml           # quotas, thresholds, paths, provider settings (no secrets)
  prompts/refine_dish.txt        # fixed LLM prompt (versioned)
  dags/build_reference_data.py
  dags/serve_request.py          # Should
  scripts/run_all.sh             # whole batch without Airflow
  src/pantrypath_pipeline/
    cli.py                       # python -m pantrypath_pipeline <step>: one entry for every step
    config.py  errors.py  logging_setup.py  alerts.py  notify.py
    llm.py                       # provider-neutral LLM client + cache/replay
    acquire/foodcom.py  acquire/off.py
    preprocess/recipes.py  preprocess/dishes.py  preprocess/off_filter.py
    off_lookup.py                # OFF evidence by barcode
    refine.py  catalog_build.py  # LLM refinement, quotas -> dish_profiles.json
    store.py                     # SQLite writer + find_dish
    validate/expectations.py  validate/stats.py  validate/drift.py
    bias.py
    providers/base.py  providers/kroger.py  providers/target.py
    request.py                   # python -m pantrypath_pipeline.request
    versioning.py                # dvc add + push
  data/{raw,interim,processed}/  # raw + interim gitignored; processed tracked by DVC
  logs/                          # pipeline.log (gitignored)
  reports/                       # stats.json, drift.json, outliers.json, ge/, bias_report.*, llm_usage.json, Gantt screenshots
  results/                       # request outputs (temporary, gitignored)
  tests/
data/golden/recipes.json         # stays; checked by shared/tests/test_golden.py
```

`pipelines/` is removed after the move. Every step is a CLI command (`python -m pantrypath_pipeline <step>`), so Airflow, `scripts/run_all.sh` and a person at a terminal all run the same code.

## 3. Shared models (`pantrypath_core`)

- **`schema.py`**: moved unchanged (the golden set still uses `Recipe`).
- **`catalog.py`**:
  - `CatalogIngredient{name, role: core|common, category: Category, share: float, added_by_llm: bool}`
  - `DishProfile{dish_id, name, aliases[], cuisine: Cuisine, ingredients[], recipe_count, source: "foodcom"|"llm", catalog_version}`
  - `Cuisine` has six values: american, european, latin_american, east_se_asian, south_asian, middle_eastern_african.
- **`diet_rules.py`**:
  - `Allergen` = milk, egg, fish, shellfish, tree_nuts, peanuts, wheat, soy, sesame, gluten (the names in the shared result.json contract).
  - `Diet` = vegan, vegetarian, none.
  - `assess(product: ProductEvidence, avoid: set[Allergen], diet: Diet) -> Verdict{verdict: safe|unsafe|unknown, reason, evidence_source}`.
  - Evidence order: explicit allergen fields → allergen and ingredient tags → ingredient text keywords → name/category keywords (the bacon fix: a "bacon" name counts as meat even when no meat appears in the ingredient text).
  - **No evidence means `unknown`, never `safe`.**
- **`result.py`**: `Result` v1.0, as approved: request, dish, stores, ingredients[products], summary, meta with `expires_at` and `schema_version="1.0"`. Two clarifications: `barcode` and `in_stock` may be null when a store doesn't report them. The model checks that the summary matches the ingredients and that every product's store is in `stores`.

## 4. Batch DAG `build_reference_data`

Tasks:

```
start
 ├─ download_off ─→ filter_off ─→ validate_off ─┐
 └─ download_foodcom ─→ clean_recipes ─→ group_dishes ─→ select_dishes
       ─→ refine_dish (mapped, one per dish) ─→ assemble_catalog ─→ validate_catalog ─┤
                                                                                       ├─→ load_sqlite ─→ compute_stats ─→ check_drift ─→ bias_report ─→ dvc_version ─→ notify
```

| Task | Does | Output |
|---|---|---|
| `download_off` | `hf_hub_download` of `openfoodfacts/product-database` food.parquet; resume, 3 retries | `data/raw/off_food.parquet` (deleted after filtering) |
| `filter_off` | DuckDB: US products with any evidence (allergens, traces, ingredients text, labels); normalize barcode to GTIN-13; keep `unique_scans_n` | `processed/off_products.parquet` (~458k rows) |
| `download_foodcom` | Kaggle `irkaal/foodcom-recipes-and-reviews` recipes.parquet; anonymous download, skipped if already present | `raw/recipes.parquet` (not pushed) |
| `clean_recipes` | unescape HTML, Unicode fractions → text, lowercase, strip quantities and prep words, parse ingredient lists, drop recipes with no ingredients | `interim/recipes_clean.parquet` |
| `group_dishes` | dish key from normalized name (strip "easy", "best", "mom's", …), count recipes and reviews, cuisine hint from Keywords | `interim/dish_candidates.parquet` |
| `select_dishes` | rank by recipes × reviews, take a candidate pool larger than the quotas (the LLM assigns final cuisine); also records the cuisine mix of a plain top 100 for the bias report | `interim/dish_pool.json` |
| `refine_dish` | LLM per dish (prompt file, JSON schema): canonical ingredients, core/common, missing key ingredients (`added_by_llm`), cuisine, merge/drop flag. Replies cached in `processed/llm_cache/` (one file per prompt hash) and replayed | `interim/refined/<dish_id>.json` |
| `assemble_catalog` | apply quotas 60/10/10/10/5/5 from config (a short cuisine is reported, not backfilled); validate each `DishProfile` | `processed/dish_profiles.json`, `reports/catalog_summary.json` |
| `load_sqlite` | tables `dishes`, `dish_ingredients`, `dish_aliases` | `processed/pantrypath.sqlite` |
| `compute_stats` | DuckDB stats: row counts, null/evidence rates, allergen coverage, cuisine counts, ingredients per dish | `reports/stats.json` (DVC metric) |
| `check_drift` | compare against the previous `stats.json` | alert on breach |
| `bias_report` | Fairlearn slices (section 7) | `reports/bias_report.{json,md}` |
| `dvc_version` | `dvc add` the 4 processed outputs + `dvc push`; never commits | `.dvc` pointers for Ansh to commit |
| `notify` | success summary (email if configured) | |

**Error handling:**
- Every task gets `retries=2` with exponential backoff (1 min base).
- The API calls inside a task are retried with tenacity: 3 attempts, backoff on HTTP 429 and 5xx.
- `on_failure_callback` sends an alert.
- A failure on one dish in `refine_dish` is recorded and skipped. The DAG fails only if fewer than 90 dishes survive.
- **Corrupt files:** after each download, the file must open as Parquet, have the expected columns and a sensible size (OFF ≥ 1 GB, Food.com ≥ 400k rows). Otherwise it is deleted, downloaded once more, and then the step fails with an alert.
- **Unavailable sources:** if a source stays unreachable and a previous processed output exists (from DVC), the step uses it and sends an alert; otherwise it fails.

**Gantt:** two runs, two screenshots in `reports/`: "before" in serial mode (one mapped LLM task at a time, branches one after the other) and "after" (parallel branches + mapped LLM calls, concurrency 4). The README gives both timings. Expected bottlenecks: `download_off` and `refine_dish`.

## 5. LLM (`llm.py`)

- **Provider-neutral:** the key is `LLM_API_KEY` in `.env`; `LLM_PROVIDER` (gemini | anthropic | openai) and `LLM_MODEL` are optional overrides. The key's format picks the provider when `LLM_PROVIDER` is empty. Today's runs use an Anthropic key (Claude Opus 5.5).
- `LLMClient` protocol: `complete_json(prompt, schema, purpose) -> LLMReply(data, usage, model, provider, cached)`. The Anthropic adapter uses the official `anthropic` SDK; Gemini and OpenAI are thin HTTP adapters.
- **Cache and replay:** every reply is cached under `processed/llm_cache/`, keyed by a hash of the prompt and schema (not the provider), and DVC-tracked. With no key, `ReplayClient` serves the cache, so graders and reruns get the identical catalog at no cost. (Anthropic's current models reject temperature settings, so replay, not temperature 0, is what makes reruns identical.)
- Every real call logs tokens; `reports/llm_usage.json` sums calls, tokens and estimated cost per model with the rates used (feeds the usage block I report).
- LLM content is tagged (`source: llm`, `added_by_llm`). It is never used for fine-tuning.

## 6. Validation and drift

**Great Expectations 1.x suites** (`validate/expectations.py`); failures go to an alert and to `reports/ge/`.

- **OFF:**
  - `code` is unique, not null, 13 digits
  - `countries_tags` contains `en:united-states`
  - evidence ≥ 1 field present
  - row count between 300k and 700k
- **Catalog:**
  - between `min_dishes` (90) and 100 dishes
  - quotas met
  - every dish has ≥ 3 ingredients, ≥ 1 core
  - `category` is in the enum
  - names are unique
  - `added_by_llm` share is ≤ 30% per dish (otherwise warning)

**Outliers** (IQR, k = 1.5): ingredients per dish, recipes per dish, OFF ingredient-text length. Counts go in `stats.json`, offenders in `reports/outliers.json`, and any outlier raises a warning alert.

**Drift** (`check_drift`):
- A warning at:
  - an OFF row count change > 10%
  - any evidence/null rate shifting > 5 percentage points
  - the catalog cuisine mix differing from the quotas
- A failure at:
  - a row count change > 30%
- Also visible via `dvc metrics diff` on `reports/stats.json`.

**Alerts** (`alerts.py`): always logged. When `ALERT_EMAIL_TO`, `ALERT_SMTP_USER` and `ALERT_SMTP_PASSWORD` (a Gmail app password) are set in `.env`, they are also emailed via Gmail SMTP. With no config, alerts go to the logs only. A broken mail setup never crashes the pipeline.

## 7. Bias (`bias.py`)

Uses `fairlearn.metrics.MetricFrame`. We train no model in this assignment, so the "model" evaluated across slices is `diet_rules`, the classifier that decides safe / unsafe / unknown. The catalog slices cover bias in the data itself.

1. **Catalog slices by cuisine:** dishes per cuisine vs quota, ingredients per dish, `added_by_llm` rate. Mitigation: quotas (before vs after shown: a plain top 100 is 89% American).
2. **Diet-rule slices** on an OFF evaluation sample. Ground truth is the OFF `vegan`/`vegetarian`/`gluten-free` labels (used for evaluation only). Slices:
   - category
   - store vs national brand (brand matches a store-brand list in config)
   - popularity tier (`unique_scans_n` 0 / 1–9 / 10+)

   Metrics: unknown rate and accuracy on the known verdicts. Mitigation: the ingredient-text fallback, reported as before vs after.
3. **Write-up:** `reports/bias_report.md` with tables, what we found, what we changed, and the trade-offs that remain.

## 8. Request flow

`python -m pantrypath_pipeline.request "quiche" --zip 45202 --avoid milk gluten --diet vegetarian [--out result.json]`

1. **Dish lookup:** exact or alias match in SQLite, then fuzzy match (rapidfuzz ≥ 90). If nothing matches, the LLM builds a profile with the same prompt (`source: llm`); the reply goes through the same cache.
2. **Stores:** `KrogerProvider.find_stores(zip, radius=10mi, limit=3)`. OAuth client credentials; token cached in memory.
3. **Products:** for each core + common ingredient and each store, `search_products(term, store_id, limit=5)`. Calls run in parallel through a thread pool (max 8). Results go to an in-memory TTL cache only.
4. **Evidence:** Kroger allergen fields first. If missing, the OFF snapshot by GTIN-13 (Kroger UPC + computed check digit). If still missing, unknown.
5. **Verdicts:** `diet_rules.assess` for each product, then build `Result`, validate it, and write it with `expires_at = now + 24h`.
6. **Target fallback (Should):** only when Kroger has no store within range or no in-stock product for an ingredient.

## 9. Config and secrets

- `config/pipeline.yaml` holds: quotas, thresholds, OFF/Food.com sources, LLM default models and prices, concurrency, radius/store/product limits, the store-brand list, and alert on/off.
- `.env` holds keys (`LLM_API_KEY`, Kroger, RedCircle, Gmail alert settings) and is never committed. `.env.example` lists names only.
- The DVC service account key is kept outside the repo; only its path goes in `.dvc/config.local`.

## 10. Testing (pytest)

| Module | Tests |
|---|---|
| `diet_rules` | each allergen; vegan/vegetarian; the bacon case; no evidence → unknown; lab test cases ported |
| `off_filter` | small fixture parquet → expected rows; GTIN-13 check digit |
| `recipes` / `dishes` | HTML entities, fractions, name normalization, grouping |
| `refine` | `ReplayClient` with recorded replies; malformed JSON → skip and record |
| `assemble_catalog` | quotas; failure under 90 dishes |
| `expectations` / `stats` / drift | good and bad fixtures; threshold behaviour |
| `bias` | MetricFrame on a tiny labelled fixture |
| `kroger` | recorded HTTP responses (no live calls in tests); UPC conversion |
| `result` | contract round-trip; `validate_golden.py` ported as a test |
| DAG | DagBag imports with no errors; task graph as expected |

**Style:** ruff (PEP 8) in `pyproject.toml`.

Tests name edge cases explicitly: missing values, empty lists, HTML entities, bad barcodes, duplicate ids, outliers, first run with no drift baseline.

**Reproducibility:** fresh clone → `docker compose up` → `dvc pull` gives the same processed data; a rerun with `ReplayClient` gives an identical catalog. Graders without an LLM key or the time for the 8 GB download set `off.source: dvc` and run with an empty `LLM_API_KEY`: the pipeline pulls the filtered 88 MB file and replays the cached LLM replies.

**DVC history:** each run's `.dvc` pointers are committed by Ansh (the DAG never commits), so Git keeps the history of data versions next to the code.

## 11. Build order (one piece per review)

1. Skeleton + shared package + move schema
2. `diet_rules` + tests
3. OFF download/filter (start the download in the background)
4. Food.com clean + group + select
5. LLM refine + catalog + SQLite (paid calls, with a usage report)
6. Validation, stats, drift, alerts
7. Bias report
8. Airflow Docker + DAG + Gantt
9. DVC remote (Ansh sets up the bucket and service account, step by step) + versioning task
10. Kroger provider + request command → `result.json`
11. README, ruff, fresh-clone check
12. If time allows: `serve_request` DAG, Target fallback

## 12. Risks

- **Two days for 11 pieces.** Mitigation: the Must list first; the Should items are cut first.
- **LLM quality or cost:** replay cache; usage and cost are reported after each run; the model is a config/env setting.
- **Docker image size or Airflow setup time on the M1:** use the official image plus a few pip installs; no TFDV.
- **Food.com license unverified:** raw data is not pushed to DVC; only derived profiles are.
- **Kroger rate limit (secondhand 10k/day):** about 37 calls per request is fine for a demo.
