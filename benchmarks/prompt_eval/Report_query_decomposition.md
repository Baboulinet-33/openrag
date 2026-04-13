# Query Decomposition Prompt Evaluation

**Models:** Mistral-Small-3.1-24B-Instruct-2503, gpt-5.4  
**Dataset:** `datasets/query_contextualizer.json` (80 cases — 20 D1, 39 D2, 21 D3)  
**Date:** 2026-04-13

---

## Results Summary

| Prompt | Model | Overall | D1 | D2 | D3 |
|--------|-------|---------|----|----|-----|
| `query_contextualizer_tmpl.txt` | Mistral-Small | 65/80 (81.2%) | 20/20 (100%) | 36/39 (92.3%) | 9/21 (42.9%) |
| `query_contextualizer_tmpl.txt` | gpt-5.4 | 66/80 (82.5%) | 20/20 (100%) | 33/39 (84.6%) | 13/21 (61.9%) |
| `query_contextualizer_v1_tmpl.txt` | Mistral-Small | 63/80 (78.8%) | 20/20 (100%) | 30/39 (76.9%) | 13/21 (61.9%) |
| `query_contextualizer_v1_tmpl.txt` | gpt-5.4 | 55/80 (68.8%) | 20/20 (100%) | 21/39 (53.8%) | 14/21 (66.7%) |

**Key finding:** v0 outperforms v1 for both models overall. v1's "no split for comparisons" rule causes severe D2 regressions — especially for gpt-5.4 (−31 pp at D2).

---

## Prompt: `query_contextualizer_tmpl.txt`

### Mistral-Small — 15 failures

**D2 — Cross-product over-splitting (expected 2, got 4)**

| ID | Domain | Query | Expected | Got |
|----|--------|-------|----------|-----|
| 30 | technology | Pricing and cold start latency: Lambda vs Cloud Functions? | 2 | 4 |
| 41 | finance | Returns of Growth Fund and Value Fund in Q3 and Q4 2024? | 2 | 4 |
| 57 | HR | Voluntary turnover for engineering and marketing in 2023 and 2024? | 2 | 4 |

**D3 — Over-splitting (expected 1, got 2+)**

| ID | Domain | Query | Expected | Got |
|----|--------|-------|----------|-----|
| 22 | healthcare | How did patient readmission rates change between 2022 and 2023? | 1 | 2 |
| 61 | finance | How have our operating costs evolved over the past two years? | 1 | 2 |
| 64 | legal | Protection duration and disclosure requirements for trade secrets and patents? | 1 | 2 |
| 71 | public_policy | Short-term and long-term effects of Finnish UBI pilot? | 1 | 4 |
| 73 | education | How is differentiated instruction applied in elementary and middle school? | 1 | 2 |
| 76 | insurance | How do flood zone + building age jointly affect premiums? | 1 | 3 |
| 79 | real_estate | How do rising rates and housing supply together affect investment returns? | 1 | 2 |
| 80 | energy | Lithium-ion vs pumped hydro: scalability and round-trip efficiency? | 1 | 4 |

**D3 — Under-splitting (expected 2, got 1)**

| ID | Domain | Query | Expected | Got |
|----|--------|-------|----------|-----|
| 63 | technology | Main architectural differences between Kafka and RabbitMQ? | 2 | 1 |
| 66 | HR | Onboarding for sales and customer success? | 2 | 1 |
| 70 | logistics | Cost and lead time: air freight vs sea freight (transatlantic)? | 2 | 4 |
| 74 | aviation | How do EASA and FAA certification requirements differ? | 2 | 1 |

---

### gpt-5.4 — 14 failures

**D2 — Under-splitting (expected 2, got 1)**

| ID | Domain | Query | Expected | Got |
|----|--------|-------|----------|-----|
| 27 | education | Student outcomes: project-based vs lecture-based instruction? | 2 | 1 |
| 34 | energy | Solar generation: summer 2024 vs winter 2024? | 2 | 1 |
| 38 | telecom | Avg 5G download speed: urban vs rural? | 2 | 1 |

**D2 — Cross-product / grouping errors**

| ID | Domain | Query | Expected | Got |
|----|--------|-------|----------|-----|
| 41 | finance | Returns of Growth Fund and Value Fund in Q3 and Q4 2024? | 2 | 4 |
| 55 | aviation | Load factor and fuel cost: NY–London and LA–Tokyo? | 4 | 2 |
| 57 | HR | Voluntary turnover for engineering and marketing in 2023 and 2024? | 2 | 4 |

**D3 — Over-splitting (expected 1, got 2)**

| ID | Domain | Query | Expected | Got |
|----|--------|-------|----------|-----|
| 22 | healthcare | How did patient readmission rates change between 2022 and 2023? | 1 | 2 |
| 61 | finance | How have our operating costs evolved over the past two years? | 1 | 2 |
| 71 | public_policy | Short-term and long-term effects of Finnish UBI pilot? | 1 | 2 |
| 73 | education | How is differentiated instruction applied in elementary and middle school? | 1 | 2 |

**D3 — Under-splitting (expected 2, got 1)**

| ID | Domain | Query | Expected | Got |
|----|--------|-------|----------|-----|
| 63 | technology | Main architectural differences between Kafka and RabbitMQ? | 2 | 1 |
| 72 | manufacturing | Root causes of surface bubbling and adhesion failure? | 2 | 1 |
| 74 | aviation | How do EASA and FAA certification requirements differ? | 2 | 1 |
| 78 | telecom | Sub-6 GHz vs mmWave 5G trade-offs? | 2 | 1 |

---

## Prompt: `query_contextualizer_v1_tmpl.txt`

### Mistral-Small — 17 failures

**D2 — Under-splitting introduced by v1 (expected 2, got 1)**

The "when NOT to decompose" section suppresses legitimate splits for distinct entities and distinct time periods.

| ID | Domain | Query | Expected | Got |
|----|--------|-------|----------|-----|
| 27 | education | Student outcomes: project-based vs lecture-based instruction? | 2 | 1 |
| 30 | technology | Pricing and cold start latency: Lambda vs Cloud Functions? | 2 | 1 |
| 34 | energy | Solar generation: summer 2024 vs winter 2024? | 2 | 1 |
| 39 | insurance | Avg claim resolution time: auto vs home insurance? | 2 | 1 |
| 40 | aviation | On-time departure rate: Jan 2025 vs Feb 2025? | 2 | 1 |
| 52 | insurance | Flood insurance claims: 2023 vs 2024? | 2 | 1 |
| 53 | telecom | Monthly churn rate: Premium plan vs Basic plan? | 2 | 1 |

**D2 — Cross-product (unchanged)**

| ID | Domain | Query | Expected | Got |
|----|--------|-------|----------|-----|
| 41 | finance | Returns of Growth Fund and Value Fund in Q3 and Q4 2024? | 2 | 4 |
| 57 | HR | Voluntary turnover for engineering and marketing in 2023 and 2024? | 2 | 4 |

**D3 — Remaining failures**

| ID | Domain | Query | Expected | Got | Issue |
|----|--------|-------|----------|-----|-------|
| 63 | technology | Kafka vs RabbitMQ architecture? | 2 | 1 | Under-split |
| 66 | HR | Onboarding for sales and customer success? | 2 | 1 | Under-split |
| 70 | logistics | Air vs sea freight: cost and lead time? | 2 | 1 | Under-split |
| 72 | manufacturing | Root causes of surface bubbling and adhesion failure? | 2 | 1 | Under-split |
| 74 | aviation | EASA vs FAA drone certification? | 2 | 1 | Under-split |
| 78 | telecom | Sub-6 GHz vs mmWave 5G trade-offs? | 2 | 1 | Under-split |
| 71 | public_policy | Short-term and long-term Finnish UBI effects? | 1 | 2 | Over-split |
| 73 | education | Differentiated instruction: elementary vs middle school? | 1 | 2 | Over-split |

---

### gpt-5.4 — 25 failures

v1 is severely worse for gpt-5.4 at D2 (84.6% → 53.8%). The comparison-suppression rule is too aggressive for this model: it collapses nearly all "X vs Y" queries into one, regardless of whether X and Y live in separate documents.

**D2 — Under-splitting (expected 2+, got 1)**

| ID | Domain | Query | Expected | Got |
|----|--------|-------|----------|-----|
| 24 | engineering | Fatigue resistance: carbon steel vs stainless steel? | 2 | 1 |
| 27 | education | Project-based vs lecture-based instruction outcomes? | 2 | 1 |
| 30 | technology | Lambda vs Cloud Functions: pricing and cold start latency? | 2 | 1 |
| 34 | energy | Solar generation: summer 2024 vs winter 2024? | 2 | 1 |
| 38 | telecom | 5G download speed: urban vs rural? | 2 | 1 |
| 39 | insurance | Claim resolution time: auto vs home? | 2 | 1 |
| 40 | aviation | On-time departure: Jan 2025 vs Feb 2025? | 2 | 1 |
| 46 | education | Enrollment: fall 2024 vs spring 2025? | 2 | 1 |
| 49 | agriculture | Soil nitrogen: soybean vs cotton fields? | 2 | 1 |
| 51 | retail | Revenue per sq ft: East Coast vs West Coast? | 2 | 1 |
| 52 | insurance | Flood insurance claims: 2023 vs 2024? | 2 | 1 |
| 53 | telecom | Monthly churn: Premium vs Basic plan? | 2 | 1 |
| 58 | environment | Annual water consumption: manufacturing vs data center? | 2 | 1 |
| 60 | finance | Exposure to FX risk, interest rate risk, and commodity risk? | 3 | 1 |

**D2 — Cross-product / grouping errors**

| ID | Domain | Query | Expected | Got |
|----|--------|-------|----------|-----|
| 41 | finance | Returns: Growth Fund and Value Fund in Q3 and Q4 2024? | 2 | 4 |
| 42 | healthcare | ER wait times and cardiology lead times: Boston vs Philadelphia? | 4 | 2 |
| 55 | aviation | Load factor and fuel cost: NY–London and LA–Tokyo? | 4 | 2 |
| 57 | HR | Voluntary turnover: engineering and marketing in 2023 and 2024? | 2 | 4 |

**D3 — Under-splitting (expected 2, got 1)**

| ID | Domain | Query | Expected | Got |
|----|--------|-------|----------|-----|
| 62 | healthcare | Discharge procedure: elderly vs pediatric patients? | 2 | 1 |
| 63 | technology | Kafka vs RabbitMQ architecture? | 2 | 1 |
| 66 | HR | Onboarding for sales and customer success? | 2 | 1 |
| 70 | logistics | Air vs sea freight: cost and lead time? | 2 | 1 |
| 72 | manufacturing | Root causes of surface bubbling and adhesion failure? | 2 | 1 |
| 74 | aviation | EASA vs FAA certification? | 2 | 1 |
| 78 | telecom | Sub-6 GHz vs mmWave 5G trade-offs? | 2 | 1 |

---

## Key Takeaways

1. **v0 is the better prompt overall.** For Mistral-Small, v0 wins at D2 (92.3% vs 76.9%) and ties at D3 (both ~62%). For gpt-5.4, v0 dominates at both D2 (84.6% vs 53.8%) and overall (82.5% vs 68.8%).

2. **v1's "no split for comparisons" rule is too broad.** It collapses legitimate splits for distinct entities ("Lambda vs Cloud Functions") and distinct time periods ("2023 vs 2024") into one query. The rule successfully avoids some over-splitting at D3, but at too high a cost at D2.

3. **Cross-product issue persists in both prompts.** Queries with 2 entities × 2 time periods (IDs 41, 57) consistently produce 4 sub-queries instead of 2. Neither prompt provides grouping guidance: *"attributes of the same entity belong in one query, one query per entity"*.

4. **Hard failures — consistent across all prompts and models:**
   - Under-split: ID 63 (Kafka vs RabbitMQ), ID 74 (EASA vs FAA), ID 66 (sales vs CS onboarding), ID 78 (sub-6 GHz vs mmWave)
   - Over-split: ID 71 (Finnish UBI short/long-term), ID 73 (elementary vs middle school)

5. **The discriminating signal is document topology.** Questions like "EASA vs FAA" involve two separate regulatory bodies with separate documentation → split. Questions like "short-term and long-term effects of the Finnish UBI" describe two aspects of the same study → keep unified. This requires either targeted few-shot examples or a prompt heuristic: *"split when the two subjects are likely held in separate source documents"*.

6. **gpt-5.4 handles D3 better than Mistral-Small on v0** (61.9% vs 42.9%), suggesting it is more conservative by default. The model's stronger tendency to keep comparative queries unified helps at D3 but also causes more D2 under-splits for v0 (e.g. IDs 27, 34, 38).

7. **Some split decisions are fundamentally ambiguous without corpus knowledge.** A user asking "Kafka vs RabbitMQ architecture?" might be referencing a single comparison study that covers both systems in one document — in which case one query is correct — or two separate technical docs, in which case two queries are needed. The LLM has no visibility into the actual corpus topology, so it must guess. This is a structural limitation of one-shot query generation: the right decomposition depends on information the model doesn't have at planning time.

   In an agentic framework this could be handled more gracefully: rather than committing to a fixed plan upfront, the LLM would be given the power to **inspect retrieval results and replan** — issuing follow-up queries, merging or splitting sub-queries, or escalating to the user when the initial retrieval yields insufficient signal. This would naturally resolve many of the ambiguous cases without requiring better prompt engineering.
