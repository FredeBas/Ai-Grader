# AI-grader (vejledende)  — v0.2.0

En lille AI-drevet applikation, som giver en **vejledende** første vurdering af en praktikrapport ud fra en rubric og et kald til Anthropic's Claude API.

> ⚠️ Output er **ikke** en formel bedømmelse. Det er et hjælpeværktøj til underviser og studerende — til at strukturere feedback og åbne for dialog.

## Hvad er nyt i 0.2.0

- **Self-consistency**: 3 parallelle LLM-kald som standard. Median-score, mode-niveau, varians-rapportering.
- **Retry med exponential backoff**: transiente API-fejl behandles automatisk.
- **Schema-reparation**: åbenlyse modelfejl (engelske niveau-navne, score som string, manglende kriterier) repareres frem for at give 502.
- **Prompt-injection forsvar**: rapport-tekst sanitiseres, system-prompt instrueres eksplicit om at ignorere instruktioner *inde i* rapporten.
- **Tegnoptælling i kode**, ikke i model. Sendes til modellen som faktuel kontekst og returneres til klienten i `meta.report_within_limit`.
- **Audit-log**: alle vurderinger logges som JSON-lines med request-id, prompt-hash og resultat.
- **Rubric-validering**: `rubric_override` valideres før det bruges (mangler 'criteria', forkert vægt-sum osv.).

## Projektstruktur

```
ai-grader/
├── rubric/
│   └── rubric.json                 # 7 kriterier udledt af læringsmål, rapportkrav og Dare-Share-Care
├── prompts/
│   ├── system_prompt.txt           # Modellens rolle, regler, JSON-skema, prompt-injection forsvar
│   └── user_prompt_template.txt    # Indeholder placeholders for {rubric_json} og {report_text}
├── backend/
│   ├── app.py                      # FastAPI app: /grade, /rubric, /health
│   ├── llm_client.py               # Anthropic SDK wrapper med retry + timeout
│   ├── schema_repair.py            # JSON-parsning og defensiv schema-reparation
│   ├── aggregator.py               # Self-consistency: median + mode + varians-rapport
│   ├── run_grader.py               # CLI: batch-vurdering af markdown-filer
│   ├── test_app.py                 # 19 tests (mocked LLM)
│   └── requirements.txt
├── frontend/
│   └── index.html                  # Single-file UI som rammer /grade
├── sample-data/
│   └── student1.md, student2.md, student3.md
├── outputs/
│   ├── student1.assessment.json    # Illustrative vurderinger
│   ├── student2.assessment.json
│   └── student3.assessment.json
└── docs/
    └── refleksion.md               # Test- og refleksionsrapport
```

## Sådan kører du backend'en

```bash
cd backend
pip install -r requirements.txt
export ANTHROPIC_API_KEY="sk-ant-..."
python app.py
# Server kører på http://localhost:8000
```

API-dokumentation (Swagger): <http://localhost:8000/docs>

### Konfiguration via environment

| Variabel | Default | Beskrivelse |
|---|---|---|
| `ANTHROPIC_API_KEY` | (kræves) | Din API-nøgle |
| `CLAUDE_MODEL` | `claude-opus-4-7` | Model |
| `SELF_CONSISTENCY_RUNS` | `3` | Antal parallelle kald (1-5) |
| `MAX_REPORT_CHARS` | `60000` | Maks rapportlængde i request |
| `LOG_LEVEL` | `INFO` | Logniveau |
| `AUDIT_LOG_PATH` | `outputs/audit.log.jsonl` | Sti til audit-log |

## Endpoints

| Metode | Sti | Beskrivelse |
|---|---|---|
| `GET` | `/` | Service-info og endpoint-liste |
| `GET` | `/health` | Health check |
| `GET` | `/rubric` | Returnér aktive rubric |
| `POST` | `/grade` | Tag en rapport-tekst, returnér struktureret vurdering |

### Eksempel på request

```bash
curl -X POST http://localhost:8000/grade \
  -H "Content-Type: application/json" \
  -d '{
    "report_text": "## Min praktikrapport\n\nJeg var i praktik hos...",
    "n_runs": 3
  }'
```

### Eksempel på respons (forkortet)

```json
{
  "samlet_vurdering": { "niveau": "middel", "score": 67.0, "resume": "..." },
  "kriterier": [
    {
      "id": "virksomhed_og_drift",
      "navn": "...",
      "niveau": "høj",
      "begrundelse": "...",
      "evidens": ["citat 1", "citat 2"]
    }
  ],
  "styrker": ["..."],
  "svagheder": ["..."],
  "forbedringsforslag": ["..."],
  "dialogspoergsmaal": ["..."],
  "forbehold": ["..."],
  "meta": {
    "request_id": "a1b2c3d4",
    "model": "claude-opus-4-7",
    "rubric_name": "Praktikrapport - Datamatiker",
    "rubric_version": "1.0",
    "prompt_hash": "9d8801bc89eb",
    "report_chars": 11820,
    "report_chars_limit": 12000,
    "report_within_limit": true,
    "self_consistency": {
      "n_runs_requested": 3,
      "n_runs_succeeded": 3,
      "n_runs_failed": 0,
      "score_spread": { "min": 65, "max": 70, "spread": 5.0 },
      "criterion_agreement": {
        "laeringsmaal": { "agreement": "3/3", "unanimous": true },
        "form_og_formidling": { "agreement": "2/3", "unanimous": false }
      }
    },
    "elapsed_seconds": 8.41,
    "ai_disclaimer": "Vejledende vurdering. Skal ikke betragtes som formel bedømmelse."
  }
}
```

## Sådan kører du den på alle tre studerende

```bash
cd backend
python run_grader.py ../sample-data/*.md --out ../outputs/ --runs 3
```

## Sådan kører du tests

```bash
cd backend
python test_app.py
# 19/19 tests passed
```

## Vigtigste designvalg

1. **Rubric som JSON, ikke prosa.** Nem at sende til modellen, validere mod, og bytte ud uden at ændre kode.
2. **System-/user-prompt adskillelse.** Systempromten definerer rolle, etiske rammer og output-format. Userpromten leverer rubric + rapport. De vedligeholdes hver for sig.
3. **Lav temperatur (0.2).** Stabile, struktur-konsistente vurderinger frem for kreativ variation.
4. **Self-consistency som standard.** 3 parallelle kald, median pr. felt, varians rapporteres som usikkerhed. Sløret bag ét felt i `meta.self_consistency`.
5. **Defensiv schema-reparation.** Engelske niveau-navne mappes til danske. Score som string koerces til float. Manglende kriterier udfyldes med "ukendt" frem for at fejle hårdt.
6. **"Ukendt"-niveau er førsteklasses.** Hvis et kriterium ikke kan vurderes ud fra teksten, *skal* modellen sige det.
7. **Evidens som krav.** Hvert kriterium kræver konkrete tekstreferencer eller citater. Reducerer generisk feedback.
8. **Eksplicit forbehold-felt.** Modellen navngiver selv begrænsninger ved sin vurdering.
9. **Tegntælling i kode, ikke i model.** Modellen er dårlig til at tælle. Vi tæller selv og giver tallet til modellen som faktuel kontekst.
10. **Audit-log.** Alle vurderinger logges med prompt-hash, så feedback altid kan spores tilbage til den prompt-version der producerede den.

## Begrænsninger

- AI'en kan ikke se bilag (billeder, diagrammer) — kun den indsendte tekst.
- Vurderingen er ikke deterministisk — derfor self-consistency med varians-rapportering.
- Self-consistency er **ikke gratis**: 3 kald koster 3x. Sættes ned til 1 hvis budgettet er stramt.

Læs videre i [`docs/refleksion.md`](docs/refleksion.md) for kritisk refleksion over løsningens kvalitet.
