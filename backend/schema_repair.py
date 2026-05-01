"""
Robust JSON-parsning og schema-reparation.

LLM'er er gode til at følge skemaer, men ikke perfekte. De kan:
- pakke JSON i markdown-fences
- skrive prosa før eller efter JSON-blokken
- glemme et obligatorisk felt
- give et tal som string ("65" i stedet for 65)
- bruge engelske niveau-navne ("low") i stedet for danske ("lav")

I stedet for at fejle hårdt på alle disse ting, prøver vi at reparere
det der kan repareres - og giver en tydelig fejl hvis det ikke kan.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Map af kendte engelske/forkerte niveau-navne til vores danske skema
_NIVEAU_ALIASES = {
    "low": "lav",
    "lavt": "lav",
    "medium": "middel",
    "mid": "middel",
    "middle": "middel",
    "high": "høj",
    "hojt": "høj",
    "hoejt": "høj",
    "højt": "høj",
    "unknown": "ukendt",
}

_VALID_NIVEAUER_OVERALL = {"lav", "middel", "høj"}
_VALID_NIVEAUER_CRIT = {"lav", "middel", "høj", "ukendt"}


class JsonRepairError(ValueError):
    """JSON kunne ikke parses eller repareres til skemaet."""


# ----------------------------------------------------------------------------
# Trin 1: Få et JSON-objekt ud af modellens råtekst
# ----------------------------------------------------------------------------


def extract_json(text: str) -> dict:
    """
    Defensiv JSON-parsning. Prøv flere strategier i rækkefølge.
    """
    if not text or not text.strip():
        raise JsonRepairError("Modellen returnerede tom tekst.")

    candidates = list(_json_candidates(text))
    last_err: Exception | None = None
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as e:
            last_err = e
            continue

    raise JsonRepairError(
        f"Kunne ikke parse JSON fra modellens svar. Sidste fejl: {last_err}"
    )


def _json_candidates(text: str):
    """
    Yield mulige JSON-strenge fra rå tekst, fra mest til mindst præcis.
    """
    cleaned = text.strip()

    # 1. Hele teksten som-er.
    yield cleaned

    # 2. Strip markdown-fences hvis de findes.
    if cleaned.startswith("```"):
        without_fences = re.sub(r"^```(?:json)?\s*", "", cleaned)
        without_fences = re.sub(r"\s*```\s*$", "", without_fences)
        yield without_fences.strip()

    # 3. Find første { ... sidste } - tager højde for prosa rundt om.
    first_brace = cleaned.find("{")
    last_brace = cleaned.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        yield cleaned[first_brace : last_brace + 1]


# ----------------------------------------------------------------------------
# Trin 2: Reparér åbenlyse skemafejl
# ----------------------------------------------------------------------------


def repair_assessment(data: dict, rubric: dict) -> dict:
    """
    Tag en parsed dict og forsøg at gøre den skemamatchende.

    Vi laver kun reparationer som er trygge: type-koercion (string -> float),
    alias-mapping for niveau, default-værdier for tomme list-felter.
    Hvis et obligatorisk felt mangler helt, kaster vi.
    """
    if not isinstance(data, dict):
        raise JsonRepairError("Modellens svar var ikke et JSON-objekt.")

    out: dict[str, Any] = {}

    # samlet_vurdering
    sv = data.get("samlet_vurdering")
    if not isinstance(sv, dict):
        raise JsonRepairError("Mangler 'samlet_vurdering' eller forkert type.")
    out["samlet_vurdering"] = {
        "niveau": _normalize_niveau(sv.get("niveau"), allow_unknown=False),
        "score": _coerce_float(sv.get("score"), field="samlet_vurdering.score"),
        "resume": _coerce_str(sv.get("resume"), field="samlet_vurdering.resume"),
    }

    # kriterier - skal indeholde alle rubric-id'er
    kriterier_raw = data.get("kriterier")
    if not isinstance(kriterier_raw, list):
        raise JsonRepairError("Mangler 'kriterier'-listen.")
    expected_ids = {c["id"] for c in rubric.get("criteria", [])}
    seen_ids: set[str] = set()
    repaired_kriterier = []
    for k in kriterier_raw:
        if not isinstance(k, dict):
            continue
        cid = k.get("id")
        if not cid:
            continue
        seen_ids.add(cid)
        repaired_kriterier.append({
            "id": cid,
            "navn": _coerce_str(k.get("navn"), field=f"kriterier[{cid}].navn"),
            "niveau": _normalize_niveau(k.get("niveau"), allow_unknown=True),
            "begrundelse": _coerce_str(
                k.get("begrundelse"), field=f"kriterier[{cid}].begrundelse"
            ),
            "evidens": _coerce_str_list(k.get("evidens", [])),
        })

    missing = expected_ids - seen_ids
    if missing:
        # Hellere flagge end at gætte - tilføj som "ukendt" så feedback'en er ærlig.
        for cid in missing:
            crit_def = next((c for c in rubric["criteria"] if c["id"] == cid), None)
            if crit_def:
                repaired_kriterier.append({
                    "id": cid,
                    "navn": crit_def.get("name", cid),
                    "niveau": "ukendt",
                    "begrundelse": (
                        "AI-grader: modellen returnerede ikke vurdering for dette "
                        "kriterium. Det skal vurderes manuelt."
                    ),
                    "evidens": [],
                })
    out["kriterier"] = repaired_kriterier

    # Listefelter - tom liste er bedre end fejl
    out["styrker"] = _coerce_str_list(data.get("styrker", []))
    out["svagheder"] = _coerce_str_list(data.get("svagheder", []))
    out["forbedringsforslag"] = _coerce_str_list(data.get("forbedringsforslag", []))
    out["dialogspoergsmaal"] = _coerce_str_list(
        data.get("dialogspoergsmaal", data.get("dialogspoergmaal", []))
    )
    out["forbehold"] = _coerce_str_list(data.get("forbehold", []))

    return out


# ----------------------------------------------------------------------------
# Hjælpere
# ----------------------------------------------------------------------------


def _normalize_niveau(value: Any, *, allow_unknown: bool) -> str:
    if not isinstance(value, str):
        return "ukendt" if allow_unknown else "middel"
    v = value.strip().lower()
    v = _NIVEAU_ALIASES.get(v, v)
    valid = _VALID_NIVEAUER_CRIT if allow_unknown else _VALID_NIVEAUER_OVERALL
    if v not in valid:
        return "ukendt" if allow_unknown else "middel"
    return v


def _coerce_float(value: Any, *, field: str) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", ".").strip())
        except ValueError as e:
            raise JsonRepairError(
                f"Feltet '{field}' kunne ikke koerces til tal: {value!r}"
            ) from e
    raise JsonRepairError(f"Feltet '{field}' mangler eller har forkert type.")


def _coerce_str(value: Any, *, field: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise JsonRepairError(f"Feltet '{field}' mangler eller er tom.")


def _coerce_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, (int, float)):
            out.append(str(item))
    return out
