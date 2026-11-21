# Scenariusz nagrania (≤ 5 min): jak przejść i nagrać pokaz

Cel: w 5 minutach pokazać, że Herald **działa end to end** (zdarzenie → alert → właściciel → AI → kod → naprawa)
i że ma **wyróżniki**, których nie ma typowy system alertów. Tekst do powiedzenia jest po angielsku (dla jury GS), a instrukcje po polsku.

## 1. Przygotowanie (15 min przed nagraniem)

**Gdzie:** produkcja https://api-production-d033bedd.up.railway.app (albo lokalnie `make rebuild`, http://localhost:8080).
Loginy i hasło: `LOGINS.local.md`.

1. **Czysta tablica.** Stare otwarte alerty z testów trzeba zamknąć (patrz sekcja „Przed nagraniem” na końcu).
2. **Okno Chrome 1440×900**, zoom 110%. Włącz „Nie przeszkadzać” w macOS. Zakładki ułóż w tej kolejności:
   1. Herald: zaloguj **Annę** przez *Sign in with Vistula ID*.
   2. Okno incognito z Herald: zaloguj **Paulinę** (`root.payments`).
   3. mockgw (telefon i SMS): https://mockgw-production.up.railway.app
   4. Mailpit: https://mailpit-production-69850428.up.railway.app
   5. Symulator Vistula Capital: https://simulator-production-ae5fd2bb.up.railway.app
   6. GitHub: `demo/payments-api` w repo (musisz być zalogowany na GitHubie, bo repo jest prywatne).
3. **Terminal** (duża czcionka, np. 18 pt) z gotowym poleceniem `curl` do wywołania alertu (z `docs/E2E_TEST_GUIDE.md`, sekcja 0).
4. **Fragment z Claude Code nagraj osobno i wcześniej** (trwa 1–3 min, w montażu przyspiesz ×4):
   1. W alercie kliknij *Open in Claude Code* i skopiuj obie komendy.
   2. W terminalu: `git clone …`, potem `claude "…"`. Claude znajduje `payments_api/fraud.py` (brak timeoutu), poprawia go, uruchamia `pytest` (2 passed) i proponuje PR.
5. **Nagrywanie:** `Cmd+Shift+5`, opcja *Nagraj zaznaczoną część* na okno Chrome. W *Opcjach* wybierz mikrofon. Alternatywy: QuickTime albo OBS (OBS pozwala przełączać sceny Chrome i terminal).
6. **Próba:** przejdź scenariusz raz bez nagrywania. Pierwsza sugestia AI dochodzi po około 5–10 s.

## 2. Scenariusz (sceny, czasy, co kliknąć, co powiedzieć)

| Czas | Ekran / akcja | Co powiedzieć | Wyróżnik |
| --- | --- | --- | --- |
| 0:00–0:20 | Herald, tablica Anny | "Vistula Capital runs dozens of systems. When something breaks, Herald decides who must act, tells them on the right channel, and helps them fix it, right in the tools they already use." | — |
| 0:20–0:40 | Wyloguj się, *Sign in with Vistula ID*, wybór konta (Pick an account), powrót do Herald | "Sign-in is the company's single sign-on over OpenID Connect. Here it's our demo identity provider; Entra ID or Okta is a config change." | SSO, sesje, audyt |
| 0:40–1:10 | Terminal: `curl` z CardGatewayTimeouts. Na tablicy karta pojawia się sama. | "An event comes in from Prometheus. No refresh: it's pushed live. Herald already assigned an owner: Anna, because the rule names her. No 'who takes this?' moment." | live feed (SSE), **automatyczny właściciel z powodem** |
| 1:10–1:40 | Otwórz alert: karta **AI proposed solution**, *Fixed before* (A-39) | "AI reads the event, our runbooks and past incidents. It proposes the next runbook step and shows how we fixed this last time. Event content is treated as untrusted; AI never acts on its own." | **AI w kontekście alertu, pamięć organizacji** |
| 1:40–1:55 | Zakładka mockgw: SMS do Anny z krokami | "Anna got a text, because it's daytime; at night her preference switches to email." | kanały według preferencji i pory |
| 1:55–2:40 | Karta **Fix it in the code**: *Open the code*, potem *Open in Claude Code*, potem wstawka z terminalem Claude Code (×4: fix, pytest zielony) | "The platform is linked to its codebase. One click gives Anna a Claude Code session with the alert as context. It finds the missing timeout in the fraud-score client, fixes it and runs the tests. Or she opens a draft pull request straight from the alert." | **alert → kod → poprawka (wyróżnik #1)** |
| 2:40–3:05 | *Logs in Datadog*. Krok runbooka *failover_region* → **Run**, potem potwierdzenie w miejscu (*Confirm*), potem *Acknowledge* | "Observability links are filled with this alert's service and time window. Risky actions run only after a human confirms, and every action is recorded." | bezpieczeństwo akcji, audyt |
| 3:05–3:25 | *Resolve*: szkic notatki AI, popraw jedno zdanie, zatwierdź | "Resolving drafts the incident note. That note becomes 'Fixed before' for the next similar alert, so the team gets smarter with every incident." | **pętla wiedzy** |
| 3:25–3:50 | Symulator: `storm_500`, na tablicy 1 alert ×500. Test lab: SMS outage, potem email fallback w Mailpit | "Noise control: 500 identical errors are one alert and one call. If the SMS provider is down, we retry, then fall back to email. Nothing is lost." | dedup, fallback, 0 strat |
| 3:50–4:20 | Okno Pauliny: **Rules → New rule → AI assistant**: "critical errors from payments-api go to Anna by SMS 8–20", potem dry-run i Save. Potem **Escalation flows** (prawdziwa polityka, kto jest kim). | "Leads configure in plain words. AI drafts the rule, a dry-run shows what it would have caught, and it's live without a restart. Escalation shows real people per level." | AI w konfiguracji, hot reload |
| 4:20–4:40 | **Runbooks → Add from file** (`docs/samples/runbooks/deploy-rollback.yaml`), szkic, potem *Accept and save* | "Runbooks come in any shape, YAML, Markdown or free text; Herald turns them into executable steps, and a human accepts the draft." | runbooki w dowolnym formacie |
| 4:40–5:00 | **Metrics** (events/s, p99, Lost 0) i jeden slajd z liczbami i Terraform | "Measured: alert on screen in 52 ms p99, up to 2,500 events per second on a laptop, zero lost under chaos. Stateless roles scale horizontally on AWS; the Terraform is in the repo." | wydajność, skalowanie |

## 3. Wyróżniki do zapamiętania (na koniec albo na slajd)
1. **Od alertu do kodu:** każda platforma ma podpięte repo. Alert otwiera Claude Code z kontekstem albo roboczy PR. Agenci pracują przez MCP.
2. **AI tam, gdzie pracujesz:** propozycja w alercie, *Fixed before*, „zapytaj o alert”, szkic notatki, reguła z opisu słownego. Zawsze jako szkic do zatwierdzenia przez człowieka.
3. **Zawsze jest właściciel:** automatyczne przypisanie z wyjaśnieniem (osoba z reguły, potem dyżur, potem najmniej obciążony).
4. **Cisza zamiast szumu:** deduplikacja, grupowanie, digest. Przypomnienia najwyżej co minutę.
5. **Niezawodność zmierzona:** p99 52 ms, 1 500–2 500 zdarzeń/s, 0 strat w teście chaosu.
6. **Bezpieczeństwo:** SSO (OIDC), audyt, potwierdzenie człowieka przed ryzykowną akcją, treść zdarzeń traktowana jako niezaufana.

## 4. Przed nagraniem: czysta produkcja
- **Stare otwarte alerty z testów** (A-52…A-128) dalej przypominają się Masterowi. Zamknij je przed nagraniem: daj znać, to zrobię to jednym poleceniem z notatką „demo reset” (historia zostaje).
- **Skrzynki demo:** wyczyść Mailpit (przycisk *Delete all* w jego UI) i mockgw (`curl -X POST https://mockgw-production.up.railway.app/admin/reset`). Wtedy na nagraniu widać tylko nowe wiadomości.
- **PR z alertu:** działa po ustawieniu `GITHUB_TOKEN` (fine-grained, repo `julpul/gs-alerting`, Contents + Pull requests: read and write) w Railway w serwisie `api`. Bez tokenu pokaż samą ścieżkę z Claude Code.
