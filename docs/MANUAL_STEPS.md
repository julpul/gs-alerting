# Co musisz zrobić ręcznie (dev checklist)

Wszystko, czego agenci nie mogą / nie powinni zrobić sami: klucze, sekrety, decyzje kontowe. Odhaczaj `[x]`.
**(Claude)** = już zrobione przez orkiestratora.

---

## 0. TL;DR — co zostało po Twojej stronie

| # | Co | Gdzie | Pilne? |
| --- | --- | --- | --- |
| 1 | Ponowić płatność GitHub **albo** upublicznić repo | https://github.com/settings/billing | tak — bez tego CI/CD nie startuje |
| 2 | Klucz LLM (min. 1 dostawca) | `.env` lokalnie + Railway `api` i `workers` | tak — inaczej AI = „unavailable” |
| 3 | Limit wydatków u dostawcy LLM | panel dostawcy | tak |
| 4 | Zdecydować o widoczności repo dla jury | GitHub | przed oddaniem |
| 5 | Front + nagranie wideo + upload do Lockbox | — | na koniec |

---

## 1. GitHub — płatność / Actions

Objaw: każdy workflow kończy się `startup_failure` po 0 s (sprawdzone na minimalnym `echo hi` — to konto, nie nasze pliki).

**Ponowienie płatności:**
1. https://github.com/settings/billing → u góry czerwony baner → **Retry payment** / **Update payment method**.
2. Payment information → dodaj/zmień kartę. Jeśli bank blokuje — w aplikacji banku zezwól na płatności internetowe/zagraniczne (GitHub obciąża z USA), potem Retry.
3. https://github.com/settings/billing/spending_limit (Actions) — limit > 0 USD albo „unlimited”, jeśli darmowe 2000 min/mies. się skończyły.
4. Daj znać → Claude odpali `gh workflow run ci.yml` i sprawdzi.

**Alternatywa (darmowo, od razu):** upublicznić repo — Actions na publicznych repo są bez limitu, jury i tak potrzebuje dostępu. W repo nie ma sekretów.
```bash
gh repo edit julpul/gs-alerting --visibility public --accept-visibility-change-consequences
```

- [x] Repo `julpul/gs-alerting` (prywatne) **(Claude)**
- [x] Secret `RAILWAY_TOKEN` (CD) — ustawiony przez dewelopera 2026-11-21 01:05
- [x] Repo variable `RAILWAY_API_URL` (smoke po deployu) **(Claude)**
- [ ] Płatność / widoczność (wyżej)
- [ ] (opcjonalnie) Branch protection `prod`: Settings → Branches → require check `ci`

## 2. Klucze LLM

Produkt używa prawdziwych modeli przez LiteLLM (bez mocków). Wystarczy **jeden** dostawca, dwa = fallback. Load testy nie wołają LLM.

| Zmienna | Skąd | Przykład wartości |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | https://console.anthropic.com/settings/keys | `sk-ant-...` |
| `OPENAI_API_KEY` | https://platform.openai.com/api-keys | `sk-...` |
| `OPENROUTER_API_KEY` (opcjonalnie) | https://openrouter.ai/keys | `sk-or-...` |
| `LLM_MODEL_FAST` | sugestie, mapowanie schematów | `anthropic/claude-haiku-5-5` |
| `LLM_MODEL_SMART` | reguła z tekstu, parsowanie runbooków, copilot | `anthropic/claude-sonnet-5-5` |
| `LLM_FALLBACKS` (opcjonalnie) | kolejność fallbacku | `openai/gpt-5-mini` |

**Lokalnie** (plik w `.gitignore`):
```bash
cp .env.example .env      # potem wpisz klucze w edytorze
make up                   # kontenery czytają ../.env
```

**Railway** — te same zmienne w serwisach **`api`** i **`workers`** (AI worker działa w `workers`). Najprościej w panelu: Railway → projekt `gs-alerting` → serwis → *Variables* → *Raw Editor*, wklej:
```
ANTHROPIC_API_KEY=sk-ant-...
LLM_MODEL_FAST=anthropic/claude-haiku-5-5
LLM_MODEL_SMART=anthropic/claude-sonnet-5-5
```
Albo CLI w **zwykłym terminalu** (nie wklejaj kluczy do czatu z AI):
```bash
railway variables --service api     --set "ANTHROPIC_API_KEY=sk-ant-..." --set "LLM_MODEL_FAST=anthropic/claude-haiku-5-5" --set "LLM_MODEL_SMART=anthropic/claude-sonnet-5-5"
railway variables --service workers --set "ANTHROPIC_API_KEY=sk-ant-..." --set "LLM_MODEL_FAST=anthropic/claude-haiku-5-5" --set "LLM_MODEL_SMART=anthropic/claude-sonnet-5-5"
```
Railway sam zrobi redeploy obu serwisów.

- [ ] Klucz lokalnie w `.env`
- [ ] Klucz w Railway `api` + `workers`
- [ ] Limit wydatków u dostawcy (np. 20 USD)

Bez kluczy działa wszystko poza draftami AI — sugestie pokazują „AI unavailable” + deterministyczną część (fixed-before, runbook).

## 3. Sekrety aplikacji
`JWT_SECRET` i `APP_SECRET` (Fernet) na Railway wygenerowane losowo przy setupie **(Claude)**. Lokalnie `.env.example` ma wartości dev. Nic do zrobienia.

## 4. Railway
- [x] Projekt `gs-alerting` / `production`: api, workers, mockgw, simulator, mailpit, postgres (pgvector + wolumen), Redis, rabbitmq (+ wolumen), zmienne, domeny **(Claude, `infra/RAILWAY.md`)**
- [x] Deploy z lokalnego CLI (`railway up`) **(Claude)** — działa bez GitHub Actions
- [ ] Po naprawie Actions: deploy = `git push origin main:prod` (CD: CI → `railway up` × 4 → smoke `/readyz`)
- [ ] Sprawdź plan Railway (8 serwisów; Hobby wystarczy na demo, load testy robimy lokalnie)

## 5. Demo / zgłoszenie
- [ ] Przed nagraniem raz przejść demo (rozgrzewa LLM i embeddingi): `make up && make demo-events`; symulator: http://localhost:8095
- [ ] Na czas nagrania wyłącz losowe incydenty symulatora: `SIM_AUTO_INCIDENTS=false make up` (lokalnie) / `AUTO_INCIDENTS=false` w serwisie `simulator` (Railway)
- [ ] Claude Code jako agent MCP (w wideo):
  ```bash
  # PAT: POST /v1/tokens (albo ekran tokenów w UI) — wartości nie commituj
  claude mcp add --transport http alerting https://api-production-d033bedd.up.railway.app/mcp --header "Authorization: Bearer <PAT>"
  ```
- [ ] Wideo ≤ 5 min → Lockbox, folder = nazwa zespołu + plik tekstowy z linkiem do repo i URL demo
- [ ] Przeczytaj sekcję „AI usage & security” w README i potwierdź, że zgadza się z tym, jak pracowaliście

## 6. Front (później)
- [ ] UI w `ui/` koduje pod `docs/openapi.yaml`. Potem: `VITE_API_URL=https://api-production-d033bedd.up.railway.app`, a origin UI dopisz do `CORS_ORIGINS` w serwisie `api` na Railway.
