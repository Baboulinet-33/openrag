"""
Prompt evaluation script for query decomposition prompts.

Loads the dataset, runs each test case through one or more prompt templates
against every model defined in the `MODELS` dict, and scores whether the
number of generated sub-queries matches n_expected_queries.

Usage:
    uv run python eval_query_decomposition.py [OPTIONS]

Options:
    --dataset PATH   Path to the dataset JSON file
                     (default: datasets/query_decomposition.json)
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
from typing import Literal

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
# Pydantic models — mirrors openrag/components/pipeline.py
# ---------------------------------------------------------------------------


class TemporalPredicate(BaseModel):
    field: Literal["created_at"] = Field(default="created_at")
    operator: Literal["==", "!=", ">", "<", ">=", "<="]
    value: str = Field(description='ISO 8601 datetime with timezone, e.g. "2026-03-15T00:00:00+00:00".')


class Query(BaseModel):
    query: str = Field(description="A semantically enriched, descriptive query for vector similarity search.")
    temporal_filters: list[TemporalPredicate] | None = Field(
        default=None,
        description="Date predicates on created_at, AND-combined. Null when no creation-date restriction.",
    )


class SearchQueries(BaseModel):
    """Search queries for semantic retrieval."""

    query_list: list[Query] = Field(..., description="Search sub-queries to retrieve relevant documents.")


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CaseResult:
    id: int
    difficulty: int
    domain: str
    n_expected_queries: int
    n_generated_queries: int
    pass_: bool
    generated_queries: list[str]
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
    by_difficulty: dict = field(default_factory=dict)
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


def _model_kwargs(base_url: str, max_tokens: int) -> dict:
    """Return call-time kwargs; omit vLLM-specific extra_body for OpenAI endpoints."""
    kwargs: dict = {"max_completion_tokens": max_tokens}
    if "openai.com" not in base_url:
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
        current_date=datetime.now().strftime("%Y-%m-%d"),
        query_language=lang,
    )


async def run_case(
    case: dict,
    prompt_template: str,
    query_generator: ChatOpenAI,
    model_base_url: str,
) -> CaseResult:
    """Run a single test case and return its result."""
    messages = case["messages"]
    last_message = messages[-1]["content"]
    prompt = format_prompt(prompt_template, last_message)
    llm_messages = build_llm_messages(prompt, messages)

    try:
        output: SearchQueries = await query_generator.bind(**_model_kwargs(model_base_url, 512)).ainvoke(llm_messages)
        n_generated = len(output.query_list)
        generated_queries = [q.query for q in output.query_list]
        passed = n_generated == case["n_expected_queries"]
        error = None
    except Exception as exc:
        n_generated = 0
        generated_queries = []
        passed = False
        error = str(exc)

    return CaseResult(
        id=case["id"],
        difficulty=case["difficulty"],
        domain=case["domain"],
        n_expected_queries=case["n_expected_queries"],
        n_generated_queries=n_generated,
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
) -> ModelReport:
    """Run all dataset cases for one model, with a tqdm progress bar."""
    model_base_url = model_cfg["base_url"]
    query_generator = ChatOpenAI(
        base_url=model_base_url,
        api_key=model_cfg.get("api_key", "EMPTY"),
        model=model_cfg["model"],
        temperature=0.1,
    ).with_structured_output(SearchQueries, method="json_mode")

    tasks = [run_case(case, prompt_template, query_generator, model_base_url) for case in dataset]

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

    by_diff: dict[int, dict] = {}
    for r in results:
        report.total += 1
        if r.error:
            report.errors += 1
        elif r.pass_:
            report.passed += 1
        else:
            report.failed += 1

        d = r.difficulty
        if d not in by_diff:
            by_diff[d] = {"total": 0, "passed": 0, "failed": 0, "errors": 0}
        by_diff[d]["total"] += 1
        if r.error:
            by_diff[d]["errors"] += 1
        elif r.pass_:
            by_diff[d]["passed"] += 1
        else:
            by_diff[d]["failed"] += 1

    report.accuracy = report.passed / report.total if report.total else 0.0
    for stats in by_diff.values():
        stats["accuracy"] = stats["passed"] / stats["total"] if stats["total"] else 0.0
    report.by_difficulty = {str(k): v for k, v in sorted(by_diff.items())}

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
    for diff, stats in report.by_difficulty.items():
        err_tag = f"  [{stats['errors']} errors]" if stats["errors"] else ""
        print(f"    Level {diff}: {stats['passed']:>3}/{stats['total']:<3} passed  ({stats['accuracy']:.1%}){err_tag}")
    print(f"  Overall  : {report.accuracy:.1%}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

HERE = Path(__file__).parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate the query_decomposition prompt across all configured models."
    )
    parser.add_argument(
        "--dataset",
        default=str(HERE / "datasets" / "query_decomposition.json"),
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
        print("No models configured. Edit the MODELS dict at the top of main.py.")
        return

    # Validate model configs before doing any work
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

    # Resolve dataset path and enforce it stays within the benchmark directory
    dataset_path = Path(args.dataset).resolve()
    try:
        dataset_path.relative_to(HERE)
    except ValueError:
        print(f"Error: --dataset path must be inside {HERE}")
        return

    # Resolve prompt path(s)
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
    print(f"Loaded {len(dataset)} test cases from {dataset_path.name}")
    print(f"Found {len(prompt_paths)} prompt(s): {', '.join(p.name for p in prompt_paths)}")
    print(f"Evaluating {len(MODELS)} model(s): {', '.join(MODELS)}")

    # Run each prompt × each model
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
            )
            print_model_summary(report)
            prompt_reports.append(report)

        output_prompts.append(
            {
                "prompt": prompt_rel,
                "models": [asdict(r) for r in prompt_reports],
            }
        )

    # Optionally persist results
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
