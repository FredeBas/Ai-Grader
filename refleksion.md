# Test og refleksion

## 1. Hvordan vi udledte rubricen

Rubricen er bygget fra tre kilder:

- **Læringsmål** (studieordning): viden om daglig drift; færdigheder i tekniske/analytiske metoder, problemstillinger, planlægning og formidling; kompetencer i udviklingsorienterede situationer, ny viden og samarbejde.
- **Krav til rapport**: virksomhedsbeskrivelse, læringsmål, opgaver+teori, personlige udviklingsmål, udbytte, kvittering, omfang.
- **Dare-Share-Care**: indgår eksplicit i bedømmelsen iflg. EK's koncept.

De endte som **7 kriterier** med vægte der summerer til 1.00:

| ID | Kriterium | Vægt |
|---|---|---|
| `virksomhed_og_drift` | Virksomhedsbeskrivelse og daglig drift | 0.10 |
| `laeringsmaal` | Opfyldelse af læringsmål | 0.25 |
| `opgaver_og_teori` | Opgaver og refleksion ift. teori | 0.20 |
| `personlig_udvikling` | Refleksion over personlige udviklingsmål | 0.15 |
| `udbytte_for_virksomhed_og_studerende` | Udbytte | 0.10 |
| `dare_share_care` | Dare, Share, Care | 0.10 |
| `form_og_formidling` | Form, struktur og formidling | 0.10 |

Læringsmål vejer mest fordi det er det formelle bedømmelsesgrundlag. Opgaver-og-teori-koblingen er næsthøjest fordi det er rapportens primære faglige tyngde.

## 2. Resultater på de tre studerende

| Studerende | Niveau | Score | Hovedindtryk |
|---|---|---|---|
| Student 1 (V1, Cloud Operations) | høj | 81 | Stærk teorikobling, god personlig refleksion, rigt underbygget udbytte. Svaghed: omfang og virksomheds-driftens bredde. |
| Student 2 (S1, ski-rejse startup) | middel | 67 | Bred portefølje af tekniske opgaver, ærlig personlig refleksion. Svaghed: tynd teorikobling, Dare-Share-Care implicit. |
| Student 3 (V1, AI research) | middel | 56 | Konkret teknisk bidrag og ydre evidens (TV2, jobtilbud). Svaghed: form og formidling, læringsmål håndteret via "Se Bilag". |

De tre vurderinger illustrerer at rubricen kan differentiere — de tre rapporter er faktisk forskellige i karakter, og scorerne afspejler det.

## 3. Hvilke reliability-forbedringer vi tilføjede i v0.2

Den første version producerede gyldige resultater, men var skrøbelig på de måder LLM-applikationer ofte er det:

| Problem | Løsning i v0.2 |
|---|---|
| Et enkelt kald gav svingende score (±5 points) | **Self-consistency**: 3 parallelle kald, median pr. felt, varians-rapport |
| Transiente API-fejl gav 502 til klienten | **Retry med exponential backoff** (3 forsøg, 1s/2s/4s + jitter) |
| Engelsk niveau ("medium") gav 502 | **Schema-reparation**: alias-map til danske niveauer |
| Score som string ("65") gav 502 | **Type-koercion** i schema_repair |
| Manglende kriterier gav 502 | **Auto-fyld** til "ukendt" frem for at fejle |
| Modellen tællede tegn upålideligt | **Tegntælling i kode**, sendt som faktuel kontekst |
| Bruger kunne potentielt prompt-injecte via rapport | **Sanitering** + eksplicit instruks i system-prompt |
| Ingen sporbarhed bagudtil | **Audit-log** med prompt-hash + request-id |
| Hængende API hang backend | **HTTP-timeout** på 60s |
| Rubric-override blev accepteret blindt | **Form-validering** af rubric-shape og vægt-sum |

Tests gik fra 6 til 19, og dækker nu retry, partial-failure, schema-repair-edge-cases, prompt-injection, og rubric-validering.

## 4. Refleksion over løsningens kvalitet

### Hvor er vurderingen stærk?

- **Når kriterier kan kobles til konkrete tekststeder.** Modellen citerer det den læser, og refleksioner som "X mangler kobling til teori" bliver underbygget.
- **Når rubricen er præcis.** Niveaudefinitionerne (lav/middel/høj) gør det nemmere for modellen at lande på samme sted to gange — særligt med self-consistency oveni.
- **Til at åbne dialog.** De foreslåede mundtlige spørgsmål er reelt brugbare til en eksamen — åbne, knyttet til indholdet, og tester forståelse.
- **Self-consistency afslører usikkerhed.** Når tre kald uenes, ser brugeren det i `criterion_agreement`-feltet i stedet for at få et falsk præcist tal.

### Hvor er vurderingen usikker eller misvisende?

- **Bilag.** Mange rapporter henviser til billeder, diagrammer og feedback. AI'en ser ingen af dem.
- **"Selvbiografisk" indhold.** Når en rapport hævder "min mentor sagde X", kan AI'en ikke verificere det. Den må antage det er sandt, hvilket favoriserer rapporter med detaljerede selvbeskrivelser.
- **Implicit bias mod struktur.** Rapporter med klar overskriftsstruktur scorer formentlig højere på "form" — hvilket kan være rimeligt, men kan også overskygge indhold.
- **Self-consistency reducerer varians men eliminerer den ikke.** Tre kald enige om "middel" giver stadig kun en *vejledende* indikation. Hvis 2/3 siger "middel" og 1/3 siger "høj", er det reel usikkerhed der ikke skal skjules.

### Stabilitet i v0.2

I tests så vi at:
- Self-consistency håndterer at 1 ud af 3 kald fejler — vi får stadig et resultat med flag.
- Schema-reparation håndterer 5 forskellige typer modelfejl uden at fejle.
- Score-varians vurderet på simulerede kald er typisk under ±5 points på samme rapport. Median-aggregering halverer den effektive varians.

## 5. Begrænsninger ved AI til denne type vurdering

Disse er strukturelle og ændres ikke af bedre kode:

1. **Ingen formel autoritet.** En karakter er en menneskelig dom underlagt regler om habilitet, klage, censor osv. AI kan ikke erstatte det.
2. **Hallucineret evidens.** Modeller kan i princippet citere noget der ikke står i teksten. Vores prompt kræver evidens-uddrag, men det kræver manuel verifikation.
3. **Følger rubricen, men kan ikke korrigere den.** Hvis rubricen er skæv, vil vurderingen også blive skæv.
4. **Risiko for "automatisk sandhed"-effekt.** Vores response indeholder eksplicit `ai_disclaimer` og et `forbehold`-felt for at modvirke det.
5. **Modellen er ikke kalibreret til denne specifikke uddannelse.** Den ved ikke hvad "datamatiker" på EK forventes at kunne på 5. semester ud over hvad rubricen og rapporten siger.

## 6. Hvad en v0.3 ville tilføje

- **Tool use / strict response_format**: tving JSON-skemaet på SDK-niveau frem for at bede pænt i prompten.
- **Calibration set**: 5-10 historisk vurderede rapporter med "facit-niveau" som regression-test. Nye prompt-versioner må ikke afvige uforventet.
- **Caching på prompt-hash**: samme rapport+rubric+prompt giver samme svar uden ekstra kald.
- **Compare-mode**: to rapporter side om side med begrundelser for forskelle — nyttigt til censur-arbejde.
- **Frontend viser uenighed visuelt**: når `criterion_agreement` ikke er unanimous, marker det med en gul advarselsfarve.
- **Mulighed for redaktør-loop**: brugeren kan markere en vurdering som "uenig — her er hvorfor", og det bliver feedback til prompt-iteration.

## 7. Hvad vi ville sige til en studerende, der bruger værktøjet

- Brug det som første feedback, ikke som dom.
- Læs `meta.self_consistency.criterion_agreement` — hvis kaldene er uenige, er AI'en i tvivl, og du skal være ekstra kritisk.
- Læs `forbehold`-feltet.
- Tag dialogspørgsmålene seriøst — de er ofte de mest nyttige output.
- Hvis du er uenig, så er du sandsynligvis ikke forkert. AI'en har set rapporten i 30 sekunder; du har levet praktikken i 12 uger.
