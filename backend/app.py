"""
AI-grader backend.

Et lille FastAPI-baseret API som modtager en praktikrapport som tekst,
sender den sammen med en rubric til Anthropic's Claude API, og returnerer
en struktureret, vejledende vurdering.

Reliability-features:
- Prompt-injection forsvar: rapport-tekst sanitiseres før indsættelse.
- Tegnoptælling i kode (ikke i model) - sendes som faktuel kontekst.
- Schema-reparation: åbenlyse modelfejl repareres frem for at fejle hårdt.
- Self-consistency: flere parallelle kald, median-aggregering, varians-rapportering.
- Audit-log: alle vurderinger logges med request-id, prompt-hash og resultat.

Dette er IKKE en automatisk sand bedømmelse - kun et hjælpeværktøj til
underviser og studerende.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from aggregator import run_with_self_consistency
from llm_client import AnthropicClient, LLMError
from schema_repair import JsonRepairError, extract_json, repair_assessment

# ----------------------------------------------------------------------------
# Konfiguration
# ----------------------------------------------------------------------------

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("ai-grader")

BASE_DIR = Path(__file__).resolve().parent.parent
RUBRIC_PATH = BASE_DIR / "rubric" / "rubric.json"
SYSTEM_PROMPT_PATH = BASE_DIR / "prompts" / "system_prompt.txt"
USER_PROMPT_PATH = BASE_DIR / "prompts" / "user_prompt_template.txt"
AUDIT_LOG_PATH = Path(os.getenv("AUDIT_LOG_PATH", str(BASE_DIR / "output" / "audit.log.jsonl")))

DEFAULT_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5")
MAX_REPORT_CHARS = int(os.getenv("MAX_REPORT_CHARS", "60000"))
DEFAULT_N_RUNS = int(os.getenv("SELF_CONSISTENCY_RUNS", "1"))
FORMEL_TEGNGRÆNSE = 12000  # iflg. krav-til-rapport.md

# ----------------------------------------------------------------------------
# Datamodeller
# ----------------------------------------------------------------------------


class GradeRequest(BaseModel):
    report_text: str = Field(..., description="Praktikrapporten som ren tekst.")
    rubric_override: Optional[dict] = Field(
        default=None,
        description="Valgfri: en alternativ rubric (samme JSON-skema som default).",
    )
    model: Optional[str] = Field(default=None, description="Valgfri: override modelnavn.")
    n_runs: int = Field(
        default=DEFAULT_N_RUNS,
        ge=1,
        le=5,
        description="Antal LLM-kald til self-consistency (1-5). Default 1.",
    )

    @field_validator("report_text")
    @classmethod
    def _trim(cls, v: str) -> str:
        return v.strip() if v else v


class CriterionAssessment(BaseModel):
    id: str
    navn: str
    niveau: str
    begrundelse: str
    evidens: list[str] = []


class OverallAssessment(BaseModel):
    niveau: str
    score: float
    resume: str


class GradeResponse(BaseModel):
    samlet_vurdering: OverallAssessment
    kriterier: list[CriterionAssessment]
    styrker: list[str]
    svagheder: list[str]
    forbedringsforslag: list[str]
    dialogspoergsmaal: list[str]
    forbehold: list[str]
    meta: dict


# ----------------------------------------------------------------------------
# Helpers: sanitering og prompt-bygning
# ----------------------------------------------------------------------------


def _load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_rubric() -> dict:
    return json.loads(_load_text(RUBRIC_PATH))


_REPORT_OPEN = "<rapport>"
_REPORT_CLOSE = "</rapport>"


def _sanitize_report(text: str) -> str:
    return (
        text.replace("<rapport>", "<&#8203;rapport>")
            .replace("</rapport>", "</&#8203;rapport>")
    )


def _build_user_prompt(rubric: dict, report_text: str) -> str:
    template = _load_text(USER_PROMPT_PATH)
    rubric_json = json.dumps(rubric, ensure_ascii=False, indent=2)
    safe_report = _sanitize_report(report_text)
    char_count = len(report_text)
    char_context = (
        f"\n\n# FAKTUEL KONTEKST (beregnet i kode, ikke fra modellen)\n"
        f"Rapportens omfang: {char_count} tegn inkl. mellemrum.\n"
        f"Kravet i krav-til-rapport.md er max {FORMEL_TEGNGRÆNSE} tegn ekskl. bilag.\n"
        f"Brug dette tal når du vurderer kriteriet 'form_og_formidling'.\n"
    )
    return (
        template
        .replace("{rubric_json}", rubric_json)
        .replace("{report_text}", safe_report)
    ) + char_context


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


def _audit(entry: dict) -> None:
    try:
        AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("Audit-log skriv fejlede: %s", e)


# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------

app = FastAPI(
    title="AI-grader (vejledende)",
    description="Vejledende AI-vurdering af praktikrapporter ud fra en rubric.",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend fra /frontend-mappen
FRONTEND_DIR = BASE_DIR / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

@app.get("/ui", include_in_schema=False)
def frontend():
    return FileResponse(str(FRONTEND_DIR / "index.html"))

_llm_client: Optional[AnthropicClient] = None


def get_llm_client() -> AnthropicClient:
    global _llm_client
    if _llm_client is None:
        _llm_client = AnthropicClient()
    return _llm_client


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    rid = uuid.uuid4().hex[:8]
    request.state.request_id = rid
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response


@app.get("/")
def root() -> dict:
    return {
        "service": "ai-grader",
        "version": "0.2.0",
        "status": "ok",
        "endpoints": ["/health", "/rubric", "/grade"],
        "note": "Vejledende AI-vurdering. Ikke en formel bedømmelse.",
    }


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "model": DEFAULT_MODEL,
        "self_consistency_runs": DEFAULT_N_RUNS,
    }


@app.get("/rubric")
def get_rubric() -> dict:
    return _load_rubric()


@app.post("/grade", response_model=GradeResponse)
def grade(req: GradeRequest, request: Request) -> GradeResponse:
    rid: str = getattr(request.state, "request_id", uuid.uuid4().hex[:8])
    t0 = time.monotonic()

    if not req.report_text or not req.report_text.strip():
        raise HTTPException(status_code=400, detail="report_text er tom.")

    if len(req.report_text) > MAX_REPORT_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"Rapporten overstiger {MAX_REPORT_CHARS} tegn.",
        )

    rubric = req.rubric_override or _load_rubric()
    if not _validate_rubric_shape(rubric):
        raise HTTPException(
            status_code=400,
            detail="rubric_override har ikke det forventede skema.",
        )

    system_prompt = _load_text(SYSTEM_PROMPT_PATH)
    user_prompt = _build_user_prompt(rubric, req.report_text)
    model = req.model or DEFAULT_MODEL

    prompt_hash = _hash(system_prompt + user_prompt)
    log.info(
        "[%s] /grade model=%s n_runs=%d tegn=%d prompt=%s",
        rid, model, req.n_runs, len(req.report_text), prompt_hash,
    )

    client = get_llm_client()

    def grade_once(sub_rid: str) -> dict:
        raw = client.complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            request_id=sub_rid,
        )
        parsed = extract_json(raw)
        return repair_assessment(parsed, rubric)

    try:
        if req.n_runs == 1:
            aggregated = grade_once(rid)
        else:
            aggregated = run_with_self_consistency(
                n_runs=req.n_runs,
                grade_once=grade_once,
                parent_request_id=rid,
            )
    except LLMError as e:
        log.exception("[%s] LLM-kald fejlede", rid)
        _audit({"rid": rid, "outcome": "llm_error", "error": str(e), "model": model})
        raise HTTPException(status_code=502, detail=f"LLM-fejl: {e}") from e
    except JsonRepairError as e:
        log.error("[%s] JSON-reparation fejlede: %s", rid, e)
        _audit({"rid": rid, "outcome": "schema_error", "error": str(e), "model": model})
        raise HTTPException(status_code=502, detail=f"Modellens svar matchede ikke skemaet: {e}") from e
    except RuntimeError as e:
        log.error("[%s] aggregering fejlede: %s", rid, e)
        raise HTTPException(status_code=502, detail=str(e)) from e

    elapsed = time.monotonic() - t0
    sc_info = aggregated.pop("_self_consistency", None)
    aggregated["meta"] = {
        "request_id": rid,
        "model": model,
        "rubric_name": rubric.get("name"),
        "rubric_version": rubric.get("version"),
        "prompt_hash": prompt_hash,
        "report_chars": len(req.report_text),
        "report_chars_limit": FORMEL_TEGNGRÆNSE,
        "report_within_limit": len(req.report_text) <= FORMEL_TEGNGRÆNSE,
        "self_consistency": sc_info,
        "elapsed_seconds": round(elapsed, 2),
        "ai_disclaimer": "Vejledende vurdering. Skal ikke betragtes som formel bedømmelse.",
    }

    _audit({
        "rid": rid, "outcome": "ok", "model": model,
        "report_chars": len(req.report_text),
        "score": aggregated["samlet_vurdering"]["score"],
        "niveau": aggregated["samlet_vurdering"]["niveau"],
        "elapsed_seconds": round(elapsed, 2),
    })

    log.info("[%s] /grade OK score=%.1f niveau=%s elapsed=%.2fs",
             rid, aggregated["samlet_vurdering"]["score"],
             aggregated["samlet_vurdering"]["niveau"], elapsed)

    try:
        return GradeResponse(**aggregated)
    except Exception as e:
        log.error("[%s] pydantic-skema fejl: %s", rid, e)
        raise HTTPException(status_code=502, detail=f"Internt skemafejl: {e}") from e


# ----------------------------------------------------------------------------
# Rubric-validering
# ----------------------------------------------------------------------------


def _validate_rubric_shape(rubric: dict) -> bool:
    if not isinstance(rubric, dict):
        return False
    crits = rubric.get("criteria")
    if not isinstance(crits, list) or not crits:
        return False
    for c in crits:
        if not isinstance(c, dict):
            return False
        if not all(k in c for k in ("id", "name", "weight", "levels")):
            return False
    total = sum(c.get("weight", 0) for c in crits)
    if not (0.95 <= total <= 1.05):
        return False
    return True


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)