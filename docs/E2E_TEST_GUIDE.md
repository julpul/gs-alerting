# Przewodnik testów e2e (manualnych) — po integracji z UI

Stan: 2026-11-21 ~05:35. Backend + UI zintegrowane, 1017 testów zielonych, QA gate przeszedł (patrz `docs/CHECKLIST.md`).

## Gdzie testować

| | Lokalnie (`make up`, baza świeżo zresetowana + seed) | Produkcja (Railway) |
| --- | --- | --- |
| UI klasyczne | http://localhost:8080/ | https://api-production-d033bedd.up.railway.app/ |
| UI Atlaskit (+ nowe ekrany) | http://localhost:8080/ads/ | https://api-production-d033bedd.up.railway.app/ads/ |
| Test lab | http://localhost:8080/lab.html | …/lab.html |
| Swagger | http://localhost:8080/docs | …/docs |
| Email (Mailpit) | http://localhost:8025 | https://mailpit-production-69850428.up.railway.app |
| SMS / telefon (mockgw) | http://localhost:8090 | https://mockgw-production.up.railway.app |
| Symulator Vistula Capital | http://localhost:8095 | https://simulator-production-ae5fd2bb.up.railway.app |

Logowanie: brak — **Viewing as** w menu profilu (prawy górny róg). Osoby: `master`, `anna` (Payments on-call), `piotr` (Settlements), `root.settlements`, `root.payments`, `manager` (informed) + reszta Vistula Capital (`docs/DEMO_ORG.md`).

## 0. Przed testami AI (Twój krok)
Klucz LLM: `docs/MANUAL_STEPS.md` §2 (lokalnie `.env` + `make up`; Railway: zmienne w `api` **i** `workers`). Bez klucza wszystko działa, a AI pokazuje „Heuristic draft” / „AI unavailable”.

## 1. Scenariusze (oczekiwany wynik)

| # | Co zrobić | Oczekiwane |
| --- | --- | --- |
| 1 | `make demo-events` albo Test lab → wyślij 3 zdarzenia z Appendix A | 5 alertów (Settlements, Rates „Rejected EUR 25000000 — LIMIT_BREACH”, Anna, Pricing per instrument, Security); feed odświeża się sam |
| 2 | Otwórz alert Settlements | Runbook `settlement-timeout` z wartościami (workflowId), kroki: check → retry → escalate |
| 3 | Uruchom krok „retry workflow” | wykonany od razu (mockgw), wpis w timeline |
| 4 | Uruchom krok „escalate” jako `piotr` | czeka na akceptację; SMS z „Reply APPROVE n” w mockgw |
| 5 | Zaakceptuj jako `piotr` | odmowa: **four-eyes** („Waiting for another approver”) |
| 6 | Przełącz na `root.settlements` → Approvals → Approve | akcja wykonana, audit „approved by …” |
| 7 | mockgw: odpowiedz `ACK <n>` z telefonu on-call | alert acknowledged, eskalacja zatrzymana |
| 8 | Test lab → Simulate an outage (SMS) → wyślij nowe zdarzenie Settlements | SMS retry → DLQ → email fallback w Mailpit, nic nie zginęło |
| 9 | Symulator → `storm_500` | 1 alert ×500, jedno powiadomienie |
| 10 | Symulator → `poisoned_log` | baner „text that looks like instructions”, AI nie proponuje akcji |
| 11 | Symulator → `new_schema` (albo wyślij dowolny JSON/tekst na `demo-key-custody`) | przyjęte; Platforms → pola widoczne; zrób regułę w edytorze (AND/OR, pole zagnieżdżone, `>`), wyślij ponownie → alert bez restartu |
| 12 | Symulator → `payments_error_spike` (potem auto `resolved`) | alert Anna; SMS 08–20 / email poza; po „resolved” zamknięty automatycznie |
| 13 | Preferencje: `/ads/#/preferences` → zmień kanały | następny alert idzie nowym kanałem |
| 14 | Resolve z notatką → wyślij podobne zdarzenie | panel „Fixed before” pokazuje A-n + notatkę |
| 15 | `/ads/#/agents` → utwórz PAT → `claude mcp add --transport http alerting <origin>/mcp --header "Authorization: Bearer <PAT>"` → w Claude Code: „list open alerts”, „raise a security alert”, „request approval for terraform_apply” | narzędzia działają; prośba agenta trafia do Approvals |
| 16 | `/ads/#/health` | events/s, p50/p99, Lost = 0 |

## 2. Testy AI (z kluczem)
- Alert → panel **AI suggestion**: diagnoza, next step (tylko istniejące kroki runbooka), Accept/Reject.
- **Copilot** (`/ads/#/copilot`): „why is payments-api alerting?”, „what fixed this last time?”, „create a rule: critical from payments-api to Anna by SMS 8–20” (draft, zapis kliknięciem).
- **AI assistant**: rule from text (+ dry-run), mapping dla `custody` po wysłaniu nieznanego schematu, runbook z wolnego tekstu (pricing cache).
- Resolve → **AI draft** notatki; postmortem/status update w alercie.
- Poisoned log (#10) z kluczem — AI nadal bez akcji.

## 3. Znane ograniczenia (świadome)
- Logowanie = tryb demo („Viewing as”), bez SSO.
- `resolve_when` z reguł Herald jest przechowywane, ale doradcze (auto-close działa dla Alertmanager `status: resolved`).
- „Rule from text” bez LLM zwraca „AI unavailable” (brak heurystyki).
- Repo prywatne + GitHub Actions zablokowane billingiem → CI/CD nie startuje (deploy robiony z CLI).
