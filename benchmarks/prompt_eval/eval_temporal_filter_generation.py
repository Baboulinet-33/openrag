"""
Prompt evaluation script for temporal filter generation.

Tests whether the query contextualizer correctly generates (or suppresses) date filters
based on whether the user is asking about document creation time vs. document content.

Dataset categories:
  - filter   : model SHOULD generate at least one filter (user restricts by document creation date)
  - no_filter: model should NOT generate any filter (date refers to content, not creation time)
  - split    : model should split into multiple queries WITHOUT any filter
                (multiple distinct content periods that live in separate documents)

Scoring per case:
  - filter_pass        : filter presence matches expectation
                         (expects_filter == any(q.filter is not None for q in output))
  - judge_correct      : LLM judge confirms the generated filter semantically matches the expected
                         filter description (only evaluated when expects_filter=True and filter present)
  - count_pass         : number of generated queries matches n_expected_queries
  - pass               : filter_pass AND judge_correct is not False AND count_pass

Judge model:
  Configured via JUDGE_BASE_URL, JUDGE_API_KEY, JUDGE_MODEL env vars.
  Falls back to the first entry in MODELS if judge vars are not set.

Usage:
    uv run python eval_temporal_filter_gen.py [OPTIONS]

Options:
    --dataset PATH   Path to the dataset JSON file
                     (default: datasets/temporal_filter.json)
    --prompt PATH    Path to a specific prompt template file.
                     If omitted, all *.txt files in ./prompts/ are evaluated.
    --output PATH    Write JSON results to this file.
                     Output structure: {"dataset": "...", "prompts": [{"prompt": "...", "models": [...]}]}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from tqdm.asyncio import tqdm

load_dotenv()

# ---------------------------------------------------------------------------
# Models to evaluate — configured via .env (BASE_URLs, API_KEYs, MODELs)
# Each variable is a semicolon-separated list of equal length, e.g.:
#   BASE_URLs=https://api.mistral.ai/v1;http://localhost:8000/v1;
#   API_KEYs=sk-key1;sk-key2;
#   MODELs=mistral-small-latest;Qwen/Qwen2.5-7B-Instruct;
#
# Judge model — optional, falls back to the first entry in MODELS:
#   JUDGE_BASE_URL=https://api.mistral.ai/v1
#   JUDGE_API_KEY=sk-judge-key
#   JUDGE_MODEL=mistral-large-latest
# ---------------------------------------------------------------------------


def _parse_env_list(key: str) -> list[str]:
    return [v for v in os.environ.get(key, "").split(";") if v.strip()]


def _build_models() -> dict[str, dict]:
    base_urls = _parse_env_list("BASE_URLs")
    api_keys = _parse_env_list("API_KEYs")
    models = _parse_env_list("MODELs")
    if not (base_urls and api_keys and models):
        return {}
    if not (len(base_urls) == len(api_keys) == len(models)):
        raise ValueError(
            f"BASE_URLs ({len(base_urls)}), API_KEYs ({len(api_keys)}), and MODELs ({len(models)}) "
            "must have the same number of semicolon-separated entries."
        )
    return {
        model: {"base_url": base_url, "api_key": api_key, "model": model}
        for base_url, api_key, model in zip(base_urls, api_keys, models)
    }


MODELS: dict[str, dict] = _build_models()

# ---------------------------------------------------------------------------
# Pydantic models — SearchQueries mirrors pipeline.py
# ---------------------------------------------------------------------------


class Query(BaseModel):
    """A single vector database search query with an optional Milvus `created_at` filter.

    Set `filter` only when the user restricts by **when the document was created** —
    not when the date describes what the document is *about*.

    NO filter — date is a content topic:
      "Sales figures in 2023"          → 2023 is the reporting period.
      "Q3 2024 financial results"      → Q3 2024 is the document subject.

    YES filter — date constrains the document index:
      "Last month's client meeting"    → created_at covers last month.
      "Latest safety bulletin"         → created_at for most recent docs.
      "Report published this week"     → created_at covers this week.

    Syntax: created_at <op> ISO "YYYY-MM-DDTHH:MM:SS+00:00"  (operators: ==,!=,>,<,>=,<=,AND,OR,NOT)
    Example: created_at >= ISO "2024-05-01T00:00:00+00:00" AND created_at <= ISO "2024-05-31T23:59:59+00:00"
    """

    query: str = Field(description="A semantically enriched, descriptive query for vector similarity search.")
    filter: str | None = Field(
        default=None,
        description=(
            "Milvus filter on `created_at` — set ONLY when the user restricts by document creation date."
            ' Format: created_at <op> ISO "YYYY-MM-DDTHH:MM:SS+00:00". Null when date refers to content topic.'
        ),
    )

    def __str__(self) -> str:
        return f"Query: {self.query}, Filter: {self.filter}"


class SearchQueries(BaseModel):
    query_list: list[Query] = Field(..., description="Search sub-queries to retrieve relevant documents.")

    def __str__(self) -> str:
        return " --- ".join(str(q) for q in self.query_list)


# ---------------------------------------------------------------------------
# LLM judge — evaluates whether a generated filter matches the expected one
# ---------------------------------------------------------------------------

_JUDGE_SYSTEM_PROMPT = """\
You are a Milvus filter expression evaluator.

Milvus filter syntax for the `created_at` field:
  Field    : created_at
  Operators: ==, !=, >, <, >=, <=, AND, OR, NOT
  Dates    : full ISO 8601 with timezone — ISO "YYYY-MM-DDTHH:MM:SS+00:00"
  Example  : created_at >= ISO "2024-05-01T00:00:00+00:00" AND created_at <= ISO "2024-05-31T23:59:59+00:00"

Today's date: {current_date}

You receive the user query, the expected filter description, and the generated filter.
Decide whether the generated filter is correct:

  correct=true  : The filter correctly represents the intended time constraint.
                  For relative expressions (e.g. "last week"), allow reasonable interpretations
                  (rolling 7-day window OR Mon–Sun of last week are both acceptable).
                  Exact second-level precision (e.g. 23:59:59 vs 00:00:00) is not required.
                  For open-ended recency queries ("past N days/hours/weeks/months", "this week",
                  "this quarter"), a lower-bound-only filter is correct — do NOT penalize the
                  absence of an upper bound.

  correct=false : The filter has one or more of these defects:
                  - Wrong field (not created_at)
                  - Wrong or missing operator for the stated constraint
                  - Date values clearly inconsistent with the stated time window
                  - Missing a required bound for a closed interval (e.g. upper bound for "only yesterday" or "on [date]")
                  When false, provide a concise reason.\
"""


class FilterJudgement(BaseModel):
    """LLM judge decision on a generated Milvus filter expression."""

    correct: bool = Field(description="True if the generated filter correctly represents the expected time constraint.")
    reason: str | None = Field(
        default=None,
        description="Required when correct=False. Concise explanation of the defect in the generated filter.",
    )


def _build_judge_llm() -> tuple[ChatOpenAI | None, str | None]:
    """Build the judge LLM from JUDGE_* env vars, falling back to the first configured model.
    Returns (llm, base_url) so callers can skip vLLM-specific params for OpenAI endpoints."""
    first = next(iter(MODELS.values()), None) if MODELS else None
    base_url = os.environ.get("JUDGE_BASE_URL") or (first["base_url"] if first else None)
    api_key = os.environ.get("JUDGE_API_KEY") or (first["api_key"] if first else None)
    model = os.environ.get("JUDGE_MODEL") or (first["model"] if first else None)
    if not (base_url and api_key and model):
        return None, None
    llm = ChatOpenAI(
        base_url=base_url,
        api_key=api_key,
        model=model,
        temperature=0.0,
    ).with_structured_output(FilterJudgement, method="function_calling")
    return llm, base_url


async def judge_filter(
    query: str,
    generated_filter: str,
    expected_filter: str,
    current_date: str,
    judge_llm: ChatOpenAI,
    judge_base_url: str,
) -> FilterJudgement:
    """Ask the LLM judge whether the generated filter matches the expected filter."""
    messages = [
        {
            "role": "system",
            "content": _JUDGE_SYSTEM_PROMPT.format(current_date=current_date),
        },
        {
            "role": "user",
            "content": (
                f"User query      : {query}\nExpected filter : {expected_filter}\nGenerated filter: {generated_filter}"
            ),
        },
    ]
    return await judge_llm.bind(**_model_kwargs(judge_base_url, 256)).ainvoke(messages)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CaseResult:
    id: int
    category: str  # "filter" | "split"
    n_expected_queries: int
    n_generated_queries: int
    expects_filter: bool
    has_filter: bool  # any generated query has a non-null filter
    filter_pass: bool  # filter presence matches expectation
    judge_correct: bool | None  # judge verdict (None when not evaluated)
    judge_reason: str | None  # judge explanation when judge_correct=False
    count_pass: bool  # query count matches expectation
    pass_: bool  # filter_pass AND (judge_correct is not False) AND count_pass
    generated_queries: list[dict]  # [{"query": str, "filter": str | None}]
    error: str | None = None


@dataclass
class ModelReport:
    model_name: str
    timestamp: str
    prompt_path: str
    dataset_path: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    accuracy: float = 0.0
    by_category: dict = field(default_factory=dict)
    cases: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core evaluation logic
# ---------------------------------------------------------------------------


def build_llm_messages(prompt: str, messages: list[dict]) -> list[dict]:
    """Build the two-message list sent to the LLM, mirroring pipeline.py."""
    chat_history = "".join(f"{m['role']}: {m['content']}\n" for m in messages)
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"Here is the chat history: \n{chat_history}\n"},
    ]


def _is_openai_api(base_url: str) -> bool:
    return "openai.com" in base_url


def _model_kwargs(base_url: str, max_tokens: int) -> dict:
    """Return call-time kwargs; omit vLLM-specific extra_body for OpenAI endpoints."""
    kwargs: dict = {"max_completion_tokens": max_tokens}
    if not _is_openai_api(base_url):
        kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
    return kwargs


def format_prompt(template: str, last_message: str) -> str:
    """Fill {current_date} and {query_language} placeholders."""
    try:
        from langdetect import detect  # type: ignore

        lang = detect(last_message)
    except Exception:
        lang = "en"

    return template.format(
        current_date=datetime.now().strftime("%A, %B %-d, %Y"),
        query_language=lang,
    )


async def run_case(
    case: dict,
    prompt_template: str,
    query_generator: ChatOpenAI,
    model_base_url: str,
    judge_llm: ChatOpenAI | None,
    judge_base_url: str | None,
    current_date: str,
) -> CaseResult:
    """Run a single test case and return its result."""
    messages = case["messages"]
    last_message = messages[-1]["content"]
    prompt = format_prompt(prompt_template, last_message)
    llm_messages = build_llm_messages(prompt, messages)
    filter_spec: dict | None = case.get("filter_spec")

    try:
        output: SearchQueries = await query_generator.bind(**_model_kwargs(model_base_url, 512)).ainvoke(llm_messages)
        n_generated = len(output.query_list)
        generated_queries = [{"query": q.query, "filter": q.filter} for q in output.query_list]
        has_filter = any(q.filter is not None for q in output.query_list)
        filter_pass = has_filter == case["expects_filter"]

        # Call LLM judge when a filter is both expected and generated.
        judge_correct: bool | None = None
        judge_reason: str | None = None
        if case["expects_filter"] and filter_spec and has_filter and judge_llm is not None and judge_base_url:
            # Concatenate all non-null filters (typically one for filter-category cases).
            all_filters = " | ".join(q["filter"] for q in generated_queries if q["filter"])
            try:
                judgement = await judge_filter(
                    query=last_message,
                    generated_filter=all_filters,
                    expected_filter=filter_spec["expected_filter"],
                    current_date=current_date,
                    judge_llm=judge_llm,
                    judge_base_url=judge_base_url,
                )
                judge_correct = judgement.correct
                judge_reason = judgement.reason
            except Exception as judge_exc:
                judge_correct = None
                judge_reason = f"Judge call failed: {judge_exc}"

        count_pass = n_generated == case["n_expected_queries"]
        passed = filter_pass and (judge_correct is not False) and count_pass
        error = None
    except Exception as exc:
        n_generated = 0
        generated_queries = []
        has_filter = False
        filter_pass = False
        judge_correct = None
        judge_reason = None
        count_pass = False
        passed = False
        error = str(exc)

    return CaseResult(
        id=case["id"],
        category=case["category"],
        n_expected_queries=case["n_expected_queries"],
        n_generated_queries=n_generated,
        expects_filter=case["expects_filter"],
        has_filter=has_filter,
        filter_pass=filter_pass,
        judge_correct=judge_correct,
        judge_reason=judge_reason,
        count_pass=count_pass,
        pass_=passed,
        generated_queries=generated_queries,
        error=error,
    )


async def run_eval_for_model(
    model_name: str,
    model_cfg: dict,
    dataset: list[dict],
    prompt_template: str,
    prompt_path: str,
    dataset_path: str,
    judge_llm: ChatOpenAI | None,
    judge_base_url: str | None,
    current_date: str,
) -> ModelReport:
    """Run all dataset cases for one model, with a tqdm progress bar."""
    model_base_url = model_cfg["base_url"]
    query_generator = ChatOpenAI(
        base_url=model_base_url,
        api_key=model_cfg.get("api_key", "EMPTY"),
        model=model_cfg["model"],
        temperature=0.1,
    ).with_structured_output(SearchQueries, method="function_calling")

    tasks = [
        run_case(case, prompt_template, query_generator, model_base_url, judge_llm, judge_base_url, current_date)
        for case in dataset
    ]

    results: list[CaseResult] = []
    for coro in tqdm(
        asyncio.as_completed(tasks),
        total=len(tasks),
        desc=f"{model_name}",
        unit="case",
        leave=True,
    ):
        results.append(await coro)

    return build_model_report(results, model_name, prompt_path, dataset_path)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def build_model_report(
    results: list[CaseResult],
    model_name: str,
    prompt_path: str,
    dataset_path: str,
) -> ModelReport:
    report = ModelReport(
        model_name=model_name,
        timestamp=datetime.now().isoformat(),
        prompt_path=prompt_path,
        dataset_path=dataset_path,
    )

    by_cat: dict[str, dict] = {}
    for r in results:
        report.total += 1
        if r.error:
            report.errors += 1
        elif r.pass_:
            report.passed += 1
        else:
            report.failed += 1

        cat = r.category
        if cat not in by_cat:
            by_cat[cat] = {
                "total": 0,
                "passed": 0,
                "failed": 0,
                "errors": 0,
                "filter_correct": 0,
                "judge_total": 0,  # cases where judge was called
                "judge_correct": 0,
                "count_correct": 0,
            }
        by_cat[cat]["total"] += 1
        if r.error:
            by_cat[cat]["errors"] += 1
        elif r.pass_:
            by_cat[cat]["passed"] += 1
        else:
            by_cat[cat]["failed"] += 1
        if not r.error:
            if r.filter_pass:
                by_cat[cat]["filter_correct"] += 1
            if r.judge_correct is not None:
                by_cat[cat]["judge_total"] += 1
                if r.judge_correct:
                    by_cat[cat]["judge_correct"] += 1
            if r.count_pass:
                by_cat[cat]["count_correct"] += 1

    report.accuracy = report.passed / report.total if report.total else 0.0
    for stats in by_cat.values():
        non_error = stats["total"] - stats["errors"]
        stats["accuracy"] = stats["passed"] / stats["total"] if stats["total"] else 0.0
        stats["filter_accuracy"] = stats["filter_correct"] / non_error if non_error else 0.0
        jt = stats["judge_total"]
        stats["judge_accuracy"] = stats["judge_correct"] / jt if jt else None
        stats["count_accuracy"] = stats["count_correct"] / non_error if non_error else 0.0
    report.by_category = dict(sorted(by_cat.items()))

    for r in results:
        d = asdict(r)
        d["pass"] = d.pop("pass_")
        report.cases.append(d)

    return report


def print_model_summary(report: ModelReport) -> None:
    print()
    print(f"  Model    : {report.model_name}")
    print(
        f"  Total    : {report.total}  |  Passed: {report.passed}  |  Failed: {report.failed}  |  Errors: {report.errors}"
    )
    for cat in ("filter", "no_filter", "split"):
        if cat not in report.by_category:
            continue
        stats = report.by_category[cat]
        err_tag = f"  [{stats['errors']} errors]" if stats["errors"] else ""
        judge_acc = stats["judge_accuracy"]
        judge_tag = f"  (judge: {judge_acc:.1%})" if judge_acc is not None else ""
        print(f"    {cat:<10}: {stats['passed']:>3}/{stats['total']:<3} passed{judge_tag}{err_tag}")
    print(f"  Overall  : {report.accuracy:.1%}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

HERE = Path(__file__).parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate temporal filter generation across all configured models.")
    parser.add_argument(
        "--dataset",
        default=str(HERE / "datasets" / "temporal_filter.json"),
        help="Path to the dataset JSON file",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="Path to a specific prompt template file (default: evaluate all *.txt files in ./prompts/)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write JSON results to this file",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    if not MODELS:
        print("No models configured. Set BASE_URLs, API_KEYs, MODELs in .env.")
        return

    errors = []
    for name, cfg in MODELS.items():
        for key in ("base_url", "model", "api_key"):
            if not cfg.get(key):
                errors.append(f"  [{name}] missing or None: '{key}'")
    if errors:
        print("Invalid model configuration:")
        for e in errors:
            print(e)
        return

    dataset_path = Path(args.dataset).resolve()
    try:
        dataset_path.relative_to(HERE)
    except ValueError:
        print(f"Error: --dataset path must be inside {HERE}")
        return

    if args.prompt:
        prompt_path = Path(args.prompt).resolve()
        try:
            prompt_path.relative_to(HERE)
        except ValueError:
            print(f"Error: --prompt path must be inside {HERE}")
            return
        prompt_paths = [prompt_path]
    else:
        prompt_paths = sorted((HERE / "prompts").glob("*.txt"))
        if not prompt_paths:
            print(f"No prompt files found in {HERE / 'prompts'}")
            return

    with dataset_path.open() as f:
        dataset: list[dict] = json.load(f)

    judge_llm, judge_base_url = _build_judge_llm()
    judge_label = os.environ.get("JUDGE_MODEL") or (next(iter(MODELS)) if MODELS else "none")
    current_date = datetime.now().strftime("%A, %B %-d, %Y")

    print(f"Loaded {len(dataset)} test cases from {dataset_path.name}")
    print(f"Found {len(prompt_paths)} prompt(s): {', '.join(p.name for p in prompt_paths)}")
    print(f"Evaluating {len(MODELS)} model(s): {', '.join(MODELS)}")
    print(f"Judge model : {judge_label}" + (" (not configured — judge skipped)" if judge_llm is None else ""))
    print(f"Current date: {current_date}")

    output_prompts: list[dict] = []
    for prompt_path in prompt_paths:
        prompt_template = prompt_path.read_text()
        prompt_rel = str(prompt_path.relative_to(HERE))
        sep = "-" * 72
        print(f"\n{sep}")
        print(f"PROMPT: {prompt_path.name}")
        print(sep)

        prompt_reports: list[ModelReport] = []
        for model_name, model_cfg in MODELS.items():
            report = await run_eval_for_model(
                model_name=model_name,
                model_cfg=model_cfg,
                dataset=dataset,
                prompt_template=prompt_template,
                prompt_path=prompt_rel,
                dataset_path=str(dataset_path.relative_to(HERE)),
                judge_llm=judge_llm,
                judge_base_url=judge_base_url,
                current_date=current_date,
            )
            print_model_summary(report)
            prompt_reports.append(report)

        output_prompts.append(
            {
                "prompt": prompt_rel,
                "models": [asdict(r) for r in prompt_reports],
            }
        )

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output = {
            "dataset": str(dataset_path.relative_to(HERE)),
            "prompts": output_prompts,
        }
        with output_path.open("w") as f:
            json.dump(output, f, indent=2)
        print(f"\nResults written to {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
