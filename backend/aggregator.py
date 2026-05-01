"""
Self-consistency aggregator.

Vi kalder modellen flere gange og kombinerer svarene for at:
- reducere varians i score
- flagge uenighed mellem kald som usikkerhed
- bruge den bedst formulerede begrundelse pr. kriterium

Aggregering pr. felt:
- score:      median af N kald
- niveau:     mode (mest hyppige) - ved uafgjort foretrækker vi det laveste
- begrundelse: vælg fra det kald hvis niveau matcher det aggregerede
- styrker/svagheder/...: union af alle kald, dedupliceret
"""

from __future__ import annotations

import concurrent.futures
import logging
import statistics
import uuid
from collections import Counter
from typing import Callable

log = logging.getLogger(__name__)

# Rangordning så "uafgjort" lander på laveste niveau (mest konservativt)
_NIVEAU_RANK = {"lav": 0, "middel": 1, "høj": 2, "ukendt": -1}


def run_with_self_consistency(
    *,
    n_runs: int,
    grade_once: Callable[[str], dict],
    parent_request_id: str | None = None,
) -> dict:
    """
    Kør grade_once N gange parallelt og aggregér resultaterne.

    grade_once(request_id) skal returnere et repareret assessment-dict.
    """
    if n_runs < 1:
        raise ValueError("n_runs skal være mindst 1")

    parent_rid = parent_request_id or uuid.uuid4().hex[:8]

    if n_runs == 1:
        # Spring orkestrering over når der kun er ét kald.
        return grade_once(f"{parent_rid}-1")

    results: list[dict] = []
    errors: list[str] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=n_runs) as pool:
        futures = {
            pool.submit(grade_once, f"{parent_rid}-{i+1}"): i + 1
            for i in range(n_runs)
        }
        for fut in concurrent.futures.as_completed(futures):
            run_idx = futures[fut]
            try:
                results.append(fut.result())
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "[%s] kald %d fejlede: %s", parent_rid, run_idx, e
                )
                errors.append(str(e))

    if not results:
        # Alle kald fejlede - giv op.
        raise RuntimeError(
            f"Alle {n_runs} self-consistency-kald fejlede. "
            f"Sidste fejl: {errors[-1] if errors else 'ukendt'}"
        )

    aggregated = _aggregate(results)
    aggregated["_self_consistency"] = {
        "n_runs_requested": n_runs,
        "n_runs_succeeded": len(results),
        "n_runs_failed": len(errors),
        "score_spread": _score_spread(results),
        "criterion_agreement": _criterion_agreement(results),
    }
    return aggregated


# ----------------------------------------------------------------------------
# Aggregering
# ----------------------------------------------------------------------------


def _aggregate(results: list[dict]) -> dict:
    """Kombiner N assessment-dicts til ét."""
    out: dict = {}

    # Samlet vurdering
    scores = [r["samlet_vurdering"]["score"] for r in results]
    overall_niveauer = [r["samlet_vurdering"]["niveau"] for r in results]
    median_score = statistics.median(scores)
    agreed_niveau = _mode_lowest(overall_niveauer)

    # Vælg det resume hvis niveau matcher det aggregerede
    matching = [
        r for r in results if r["samlet_vurdering"]["niveau"] == agreed_niveau
    ]
    chosen = matching[0] if matching else results[0]
    out["samlet_vurdering"] = {
        "niveau": agreed_niveau,
        "score": round(median_score, 1),
        "resume": chosen["samlet_vurdering"]["resume"],
    }

    # Kriterier - aggregér pr. id
    all_ids: list[str] = []
    seen: set[str] = set()
    for r in results:
        for k in r["kriterier"]:
            if k["id"] not in seen:
                seen.add(k["id"])
                all_ids.append(k["id"])

    out["kriterier"] = []
    for cid in all_ids:
        per_run = [
            next((k for k in r["kriterier"] if k["id"] == cid), None)
            for r in results
        ]
        per_run = [k for k in per_run if k is not None]
        if not per_run:
            continue
        niveauer = [k["niveau"] for k in per_run]
        agreed = _mode_lowest(niveauer)
        # Vælg den begrundelse hvis kald landede på det aggregerede niveau
        matching_k = [k for k in per_run if k["niveau"] == agreed]
        chosen_k = matching_k[0] if matching_k else per_run[0]
        # Saml evidens fra alle matchende kald (dedupliceret)
        evidens_pool: list[str] = []
        for k in matching_k or per_run:
            for e in k.get("evidens", []):
                if e not in evidens_pool:
                    evidens_pool.append(e)
        out["kriterier"].append({
            "id": cid,
            "navn": chosen_k["navn"],
            "niveau": agreed,
            "begrundelse": chosen_k["begrundelse"],
            "evidens": evidens_pool[:5],  # cap ved 5
        })

    # Listefelter - union, dedupliceret, capped
    for field, cap in [
        ("styrker", 6),
        ("svagheder", 6),
        ("forbedringsforslag", 6),
        ("dialogspoergsmaal", 7),
        ("forbehold", 4),
    ]:
        out[field] = _union_dedup([r.get(field, []) for r in results], cap=cap)

    return out


def _mode_lowest(values: list[str]) -> str:
    """Mest hyppige værdi - ved uafgjort vælges den laveste rang."""
    if not values:
        return "ukendt"
    counter = Counter(values)
    max_count = max(counter.values())
    tied = [v for v, c in counter.items() if c == max_count]
    if len(tied) == 1:
        return tied[0]
    # Sorter på rang - lavest først (mest konservativ)
    tied.sort(key=lambda v: _NIVEAU_RANK.get(v, 99))
    return tied[0]


def _union_dedup(lists: list[list[str]], cap: int) -> list[str]:
    """Tag union af lister med stabil orden og dedup på case-insensitive match."""
    seen: set[str] = set()
    out: list[str] = []
    # Round-robin: tag #1 fra hver liste, så #2 osv. - giver god diversitet
    max_len = max((len(lst) for lst in lists), default=0)
    for i in range(max_len):
        for lst in lists:
            if i < len(lst):
                item = lst[i]
                key = item.lower().strip()
                if key not in seen:
                    seen.add(key)
                    out.append(item)
                    if len(out) >= cap:
                        return out
    return out


# ----------------------------------------------------------------------------
# Diagnostiske mål for usikkerhed
# ----------------------------------------------------------------------------


def _score_spread(results: list[dict]) -> dict:
    scores = [r["samlet_vurdering"]["score"] for r in results]
    if len(scores) < 2:
        return {"min": scores[0], "max": scores[0], "spread": 0.0}
    return {
        "min": min(scores),
        "max": max(scores),
        "spread": round(max(scores) - min(scores), 1),
    }


def _criterion_agreement(results: list[dict]) -> dict:
    """For hvert kriterium: hvor mange kald var enige om niveau."""
    out: dict = {}
    all_ids: set[str] = set()
    for r in results:
        for k in r["kriterier"]:
            all_ids.add(k["id"])
    for cid in all_ids:
        niveauer = [
            k["niveau"]
            for r in results
            for k in r["kriterier"]
            if k["id"] == cid
        ]
        if not niveauer:
            continue
        most_common, count = Counter(niveauer).most_common(1)[0]
        out[cid] = {
            "agreement": f"{count}/{len(niveauer)}",
            "unanimous": count == len(niveauer),
        }
    return out
