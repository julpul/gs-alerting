# Co musisz zrobić ręcznie (dev checklist)

Wszystko, czego agenci nie mogą / nie powinni zrobić sami: klucze, sekrety, decyzje kontowe. Odhaczaj `[x]`.
Status kroków, które orkiestrator już wykonał, jest oznaczony **(zrobione przez Claude)**.

---

## 1. Klucze LLM (wymagane dla funkcji AI)

Produkt używa prawdziwych modeli (bez mocków) przez LiteLLM. Wystarczy **jeden** dostawca, dwa dają fallback.

| Zmienna | Skąd | Uwagi |
| --- | --- | --- |
| `OPENAI_API_KEY` | https://platform.openai.com/api-keys | |
| `ANTHROPIC_API_KEY` | https://console.anthropic.com/settings/keys | |
| `OPENROUTER_API_KEY` | https://openrouter.ai/keys | opcjonalny trzeci fallback |
| `LLM_MODEL_FAST` | np. `anthropic/claude-haiku-5-5` lub `openai/<mały model>` | sugestie, mapowanie schematów |
| `LLM_MODEL_SMART` | np. `anthropic/claude-sonnet-5-5` lub `openai/<flagowy model>` | reguła z tekstu, parsowanie runbooków |
| `LLM_FALLBACKS` | np. `openai/<model>,openrouter/<vendor>/<model>` | kolejność fallbacku |

- [ ] Lokalnie: `cp .env.example .env` i wpisz klucze (plik jest w `.gitignore`).
- [ ] Railway: wklej te same zmienne do serwisów `api` i `workers` (Variables → Raw editor). Komenda, jeśli wolisz CLI (wartości wpisujesz sam, nie wklejaj kluczy do czatu z AI):
  ```bash
  railway variables --service api --set "ANTHROPIC_API_KEY=..." --set "OPENAI_API_KEY=..."
  railway variables --service workers --set "ANTHROPIC_API_KEY=..." --set "OPENAI_API_KEY=..."
  ```
- [ ] Ustaw limit wydatków u dostawcy (np. 20 USD) — load testy nie wołają LLM, ale demo tak.

Bez kluczy wszystko działa poza draftami AI (UI pokaże „AI unavailable”, fixed-before i runbook dalej działają).

## 2. Sekrety aplikacji

| Zmienna | Jak wygenerować |
| --- | --- |
| `JWT_SECRET` | `openssl rand -hex 32` |
| `APP_SECRET` (Fernet) | `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |

Lokalnie `.env.example` ma wartości dev; na Railway orkiestrator ustawia losowe wartości przy deployu **(zrobione przez Claude, sprawdź w Railway → Variables)**.

## 3. GitHub

- [x] Repo `julpul/gs-alerting` (prywatne) **(zrobione przez Claude)**
- [ ] **Przed oddaniem:** zdecyduj czy repo ma być publiczne dla jury: `gh repo edit julpul/gs-alerting --visibility public --accept-visibility-change-consequences` (albo dodaj jurorów jako collaborators).
- [ ] Secret `RAILWAY_TOKEN` dla CD (jeśli Claude nie zdołał go utworzyć sam — patrz §4).
- [ ] (opcjonalnie) Branch protection na `prod`: Settings → Branches → require status check `ci`.

## 4. Railway

- [x] Projekt `gs-alerting`, środowisko `production`, serwisy **(robi orkiestrator w fazie P5)**
- [ ] **Project token dla GitHub Actions** (jeśli nie ma go jeszcze w secrets): Railway → projekt `gs-alerting` → Settings → Tokens → *Create token* (environment: `production`) → potem:
  ```bash
  gh secret set RAILWAY_TOKEN --repo julpul/gs-alerting   # wklej token w prompt
  ```
- [ ] Sprawdź plan/limity Railway (8 serwisów: api, workers, mockgw, mailpit, simulator, postgres, redis, rabbitmq). Hobby plan wystarczy na demo; load testy robimy lokalnie, nie na Railway.
- [ ] Deploy na prod = `git push origin main:prod` (CD robi resztę). Ręczny trigger: Actions → *cd* → Run workflow.

## 5. Lokalne narzędzia

| Narzędzie | Po co | Instalacja |
| --- | --- | --- |
| Docker Desktop (≥ 8 GB RAM dla Dockera) | cały stack | już jest |
| `uv` | backend | już jest |
| `k6` | load testy | `brew install k6` **(Claude instaluje przy P3-LOAD)** |
| `gh`, `railway` CLI | repo, deploy | już zalogowane |

## 6. Demo / zgłoszenie

- [ ] Uruchom raz pełny scenariusz demo przed nagraniem (rozgrzewa LLM i embeddingi): `make up && make demo-events`.
- [ ] Podłączenie Claude Code jako agenta MCP (pokazujemy w wideo):
  ```bash
  # PAT generujesz w UI (albo POST /v1/tokens), wartości nie wklejaj do repo
  claude mcp add --transport http alerting http://localhost:8080/mcp --header "Authorization: Bearer <PAT>"
  ```
- [ ] Nagranie ≤ 5 min, upload do Lockbox w folderze z nazwą zespołu + plik tekstowy z linkiem do repo i URL demo na Railway.
- [ ] Sekcja „AI usage & security” w README jest gotowa — przeczytaj i potwierdź, że zgadza się z tym, jak pracowaliście.

## 7. Front (później)

- [ ] UI w `ui/` (osobny agent/designer) koduje pod `docs/openapi.yaml`. Gdy będzie gotowe: `VITE_API_URL` = URL serwisu `api` na Railway, dodaj origin UI do `CORS_ORIGINS` w serwisie `api`.
