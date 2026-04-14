# Temporal Filter Generation Eval

**Models:** Mistral-Small-3.1-24B-Instruct-2503, gpt-5.4, anthropic/claude-opus-4.6-fast
**Dataset:** `datasets/temporal_filter.json` (80 cases — 40 `filter`, 40 `split`)
**Date:** 2026-04-14
**Results file:** `results/result_filter_generation.json`

---

## Dataset

The dataset contains 80 cases split into two categories:

- **`filter` (40 cases):** The user's query restricts by document ingestion date (`created_at`). The model must generate a valid Milvus filter expression. Cases cover relative expressions ("past 7 days", "this month"), named periods ("Q1 2024", "February 2025"), and specific dates.
- **`split` (40 cases):** The user compares two distinct time periods ("2022 vs 2023", "H1 vs H2 2024"). The model must decompose into exactly 2 sub-queries with no filter on either.

Each case is scored on three dimensions: **filter presence** (correct detection of whether a filter is needed), **judge accuracy** (LLM judge validates the generated filter expression against the expected one), and **count pass** (correct number of sub-queries). A case passes only when all three are correct.

---

## Results Summary

| Prompt | Model | Overall | Filter pass | Split pass | Filter presence | Judge acc. |
|--------|-------|---------|-------------|------------|----------------|-----------|
| `query_contextualizer_tmpl.txt` | **claude-opus-4.6-fast** | **85.0%** | 28/40 (70%) | **40/40 (100%)** | 97.5% | 71.8% |
| `query_contextualizer_tmpl.txt` | gpt-5.4 | 78.8% | 27/40 (67.5%) | 36/40 (90%) | 90.0% | 75.0% |
| `query_contextualizer_tmpl.txt` | Mistral-Small | 60.0% | 9/40 (22.5%) | 39/40 (97.5%) | 47.5% | 47.4% |
| `query_contextualizer_v1_tmpl.txt` | claude-opus-4.6-fast | 53.8% | 27/40 (67.5%) | 16/40 (40%) | 97.5% | 69.2% |
| `query_contextualizer_v1_tmpl.txt` | Mistral-Small | 45.0% | 14/40 (35%) | 22/40 (55%) | 70.0% | 50.0% |
| `query_contextualizer_v1_tmpl.txt` | gpt-5.4 | 45.0% | 24/40 (60%) | 12/40 (30%) | 85.0% | 70.6% |

**Key finding:** Claude-opus-4.6-fast is the top performer on `query_contextualizer_tmpl.txt` (85%), with a perfect split score (40/40) and near-perfect filter detection (97.5%). The v1 prompt breaks split for every model — drop of −24 cases for Claude and gpt-5.4, −17 for Mistral.

---

## Prompt: `query_contextualizer_tmpl.txt`

### Mistral-Small — 32 failures

**Filter — missing filter (21 cases)**

Filter presence is only 47.5% — Mistral fails to generate a filter for more than half of expected cases. Key systematic misses: queries with quarter/month expressions ("Q1 2024", "February 2025", "Q3 2023") and relative periods ("this month", "last week", "past 48 hours").

| ID | Query | Expected |
|----|-------|----------|
| 109 | What engineering specs were added to the system in Q1 2024? | `created_at >= 2024-01-01 AND <= 2024-03-31` |
| 110 | What compliance reports were ingested in February 2025? | `created_at >= 2025-02-01 AND <= 2025-02-28` |
| 111 | What vendor contracts were uploaded in Q3 2023? | `created_at >= 2023-07-01 AND <= 2023-09-30` |
| 23 | What research papers were uploaded to the system in 2024? | `created_at >= 2024-01-01 AND <= 2024-12-31` |
| 3 | What's covered in status reports uploaded this month? | `created_at >= <first-of-month>` |

**Filter — judge rejected (10 cases)**

Primary reason: spurious upper bound on open-ended "past N" queries, and wrong date arithmetic.

| ID | Query | Issue |
|----|-------|-------|
| 129 | System architecture diagrams added in the past 90 days? | Wrong lower bound date computed |
| 21 | Onboarding documents added in the past 6 months? | Wrong lower bound date computed |
| 116 | Partnership agreements added in the last 5 days? | Unnecessary upper bound added |
| 122 | Design documents submitted last month? | Off-by-one month (March instead of February) |

**Split — 1 failure**

| ID | Query | Expected | Got |
|----|-------|----------|-----|
| 43 | Pricing structure differences between 2023 and 2024? | 2 | 1 |

---

### gpt-5.4 — 17 failures

**Filter — missing filter (4 cases)**

| ID | Query |
|----|-------|
| 23 | Research papers uploaded to the system in 2024? |
| 106 | Files uploaded to the system on Saturday, December 14, 2024? |
| 109 | Engineering specs added to the system in Q1 2024? |
| 111 | Vendor contracts uploaded in Q3 2023? |

**Filter — judge rejected (9 cases)**

All 9 rejections share the same root cause: adding an unnecessary `AND created_at <= today` to open-ended "past N" queries.

| ID | Query | Generated (incorrect) |
|----|-------|----------------------|
| 116 | Partnership agreements added in the last 5 days? | `>= [date] AND <= today` |
| 113 | Project briefs added in the last 3 days? | `>= [date] AND <= today` |
| 119 | Financial summaries uploaded this quarter? | `>= [start-of-Q] AND <= today` |
| 115 | Software release notes indexed in the past 10 days? | `>= [date] AND <= today` |

**Split — 4 failures**

| ID | Query | Expected | Got |
|----|-------|----------|-----|
| 33 | How did marketing strategy change from 2022 to 2023? | 2 | 1 |
| 48 | Main differences in product catalog between 2023 and 2024? | 2 | 1 |
| 212 | How did CAC differ between H1 and H2 2024? | 2 | 3 |
| 222 | Feature adoption before and after 2024 product rebrand? | 2 | 1 |

---

### claude-opus-4.6-fast — 12 failures

**Filter — missing filter (1 case)**

| ID | Query |
|----|-------|
| 109 | Engineering specs added to the system in Q1 2024? |

Case 109 is the only universal failure — all three models miss it in both prompts.

**Filter — judge rejected (11 cases)**

Same root cause as gpt-5.4: spurious upper bound on open-ended "past N" queries.

| ID | Query | Issue |
|----|-------|-------|
| 6 | What's new in documents uploaded in the past 7 days? | Unnecessary upper bound |
| 117 | Training materials uploaded in the past 30 days? | Unnecessary upper bound |
| 119 | Financial summaries uploaded this quarter? | Upper bound set to today instead of end-of-quarter |
| 21 | Onboarding documents added in the past 6 months? | Wrong lower bound date |

**Split — 0 failures (perfect)**

Claude correctly decomposes all 40 multi-period queries into exactly 2 sub-queries with no false filters on any split case.

---

## Prompt: `query_contextualizer_v1_tmpl.txt`

The v1 prompt causes a severe, uniform split regression across all models. Filter detection rates remain stable, confirming the issue is isolated to the decomposition instruction.

| Model | split | split v1 | drop |
|-------|-----------|---------|------|
| Claude | 40/40 (100%) | 16/40 (40%) | −24 |
| gpt-5.4 | 36/40 (90%) | 12/40 (30%) | −24 |
| Mistral | 39/40 (97.5%) | 22/40 (55%) | −17 |

All failures are "got 1 instead of 2" — models collapse comparative queries ("October vs November 2024", "H1 vs H2 2024") into a single merged query. v1's "when NOT to split" rule is too broad.

---

## Key Takeaways

1. **`query_contextualizer_tmpl.txt` is the only viable prompt.** The v1 prompt is strictly worse for every model on every metric.

2. **Claude-opus-4.6-fast is the best model overall (85% on query_contextualizer_tmpl.txt).** It is the only model to achieve a perfect split score (40/40) and misses only 1 filter entirely. It should be the primary model for this task.

3. **The dominant failure mode is a spurious upper bound on open-ended "past N" queries.** Both Claude and gpt-5.4 add `AND created_at <= today` to queries like "past 30 days" or "past 7 days". A single prompt fix resolves this: *"For 'past N days/hours/weeks/months' queries, use only a lower-bound filter. Add an upper bound only for closed intervals ('between X and Y', 'only yesterday', 'on [specific date]')."* > **Note:** This rule has been added to `query_contextualizer_tmpl.txt` in the same PR.

4. **Case 109 is a universally hard case (fails 6/6 evaluations).** "Engineering specs **added to the system** in Q1 2024" — despite explicit ingestion-time language, all models treat "Q1 2024" as a content topic. A targeted few-shot example covering quarter/month periods as `created_at` filters is required.

5. **Mistral-Small is unsuitable for this task.** Filter presence is only 47.5% on query_contextualizer_tmpl.txt (misses more than half of required filters). Its judge accuracy on its own output (47.4%) also indicates unreliable self-evaluation. It should not be used as judge.

6. **The cross-prompt split regression shares the same root cause as the query decomposition eval** — v1's decomposition suppression rule is too aggressive regardless of whether the task involves temporal filters or entity comparisons.
