"""
CLI: Kør AI-grader'en mod et eller flere markdown-filer fra kommandolinjen.

Brug:
    python run_grader.py ../sample-data/student1.md
    python run_grader.py ../sample-data/*.md --out ../output/ --runs 1
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from aggregator import run_with_self_consistency
from app import (
    DEFAULT_MODEL,
    FORMEL_TEGNGRÆNSE,
    SYSTEM_PROMPT_PATH,
    _build_user_prompt,
    _hash,
    _load_rubric,
    _load_text,
)
from llm_client import AnthropicClient, LLMError
from schema_repair import JsonRepairError, extract_json, repair_assessment


def grade_file(path: Path, client: AnthropicClient, model: str, n_runs: int) -> dict:
    report_text = path.read_text(encoding="utf-8")
    rubric = _load_rubric()
    system_prompt = _load_text(SYSTEM_PROMPT_PATH)
    user_prompt = _build_user_prompt(rubric, report_text)
    parent_rid = uuid.uuid4().hex[:8]

    def grade_once(sub_rid: str) -> dict:
        raw = client.complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            request_id=sub_rid,
        )
        parsed = extract_json(raw)
        return repair_assessment(parsed, rubric)

    if n_runs == 1:
        result = grade_once(parent_rid)
    else:
        result = run_with_self_consistency(
            n_runs=n_runs,
            grade_once=grade_once,
            parent_request_id=parent_rid,
        )

    sc = result.pop("_self_consistency", None)
    result["meta"] = {
        "request_id": parent_rid,
        "source_file": str(path),
        "model": model,
        "rubric_name": rubric.get("name"),
        "rubric_version": rubric.get("version"),
        "prompt_hash": _hash(system_prompt + user_prompt),
        "report_chars": len(report_text),
        "report_chars_limit": FORMEL_TEGNGRÆNSE,
        "report_within_limit": len(report_text) <= FORMEL_TEGNGRÆNSE,
        "self_consistency": sc,
        "ai_disclaimer": "Vejledende vurdering. Ikke en formel bedømmelse.",
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Kør AI-grader på markdown-filer.")
    parser.add_argument("files", nargs="+", help="Sti til en eller flere .md-filer.")
    parser.add_argument("--out", default="../output", help="Mappe til JSON-output.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Modelnavn.")
    parser.add_argument("--runs", type=int, default=1,
                        help="Antal LLM-kald til self-consistency (1-5). Default 1.")
    args = parser.parse_args()

    if not 1 <= args.runs <= 5:
        print("--runs skal være mellem 1 og 5", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        client = AnthropicClient()
    except LLMError as e:
        print(f"FEJL: {e}", file=sys.stderr)
        return 1

    exit_code = 0
    for raw_path in args.files:
        path = Path(raw_path)
        if not path.exists():
            print(f"Springer over (mangler): {path}")
            continue

        print(f"Vurderer: {path}  (runs={args.runs})")
        try:
            result = grade_file(path, client, args.model, args.runs)
        except (LLMError, JsonRepairError, ValueError, RuntimeError) as e:
            print(f"  FEJL: {e}", file=sys.stderr)
            exit_code = 1
            continue

        out_path = out_dir / f"{path.stem}.assessment.json"
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        sv = result["samlet_vurdering"]
        sc = result["meta"].get("self_consistency") or {}
        spread = sc.get("score_spread", {}).get("spread", 0)
        print(f"  -> {out_path.name}  niveau={sv['niveau']}  score={sv['score']}  spread=±{spread}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())