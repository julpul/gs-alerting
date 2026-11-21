# Przewodnik testów e2e (manualnych)

Stan: 2026-11-21. Jedno UI: klasyczny **The Herald** pod `/`. Stare `/ads/` przekierowuje na `/`. AI jest wbudowane w ekrany. Każdy nowy alert od razu dostaje właściciela.

## Gdzie testować

| | Lokalnie (`make up`) | Produkcja (Railway) |
| --- | --- | --- |
| Herald UI | http://localhost:8080/ | https://api-production-d033bedd.up.railway.app/ |
| Test lab | http://localhost:8080/lab.html | …/lab.html |
| Swagger | http://localhost:8080/docs | …/docs |
| Email (Mailpit) | http://localhost:8025 | https://mailpit-production-69850428.up.railway.app |
| SMS / telefon (mockgw) | http://localhost:8090 | https://mockgw-production.up.railway.app |
| Symulator Vistula Capital | http://localhost:8095 | https://simulator-production-ae5fd2bb.up.railway.app |

Logowanie: na stronie **Sign in** kliknij **Sign in with Vistula ID** (nasze SSO), wpisz login `anna` albo `anna@demo` i hasło demo. Przy kolejnym logowaniu Vistula ID pokaże „Pick an account”, czyli SSO bez ponownego hasła. Pod przyciskiem jest awaryjny formularz z loginem i hasłem.

Hasło nie jest w repo: lokalnie jest w `.env` (`DEMO_PASSWORD`), lista kont w `LOGINS.local.md` (plik spoza gita), na Railway w zmiennych.

Vistula ID: lokalnie http://localhost:8180, produkcja https://sso-production-b4223540.up.railway.app.

Wylogowanie: avatar w prawym górnym rogu → **Sign out**. Kończy sesję w Herald i w Vistula ID. Dla administratora (`master`) jest tam też *Preview as level… (testing)*.

Osoby:
- `anna`: Payments, na dyżurze;
- `piotr`: Settlements, na dyżurze;
- `root.payments`, `root.settlements`: liderzy domen;
- `master`;
- `manager`: tylko informowany.

Pozostałe osoby: `LOGINS.local.md` / `docs/DEMO_ORG.md`. Stary tryb bez logowania („Viewing as”): `AUTH_MODE=demo make up`. SSO z Keycloakiem: `docs/SSO.md`.

Ukryte strony (bez linku w menu): `/#settings` (też zębatka w górnym pasku), `/#report`, `/#deliveries` (skrzynka demo), `/lab.html`.

## A. Główny scenariusz: jedna osoba, od zdarzenia do maila (≈ 5 min)

| # | Krok | Co powinieneś zobaczyć |
| --- | --- | --- |
| 1 | Viewing as **anna**. Otwórz **Alerts** (tablica Open / Acknowledged / Resolved). | Tablica zespołu Payments. |
| 2 | Test lab (lub `make demo-events`): wyślij zdarzenie `payments-api` HighErrorRate. | Karta pojawia się sama, bez odświeżania. Na karcie widać właściciela (imię i znacznik „auto”). |
| 3 | Otwórz alert. | Na górze karta **AI proposed solution**: „Claude · claude-haiku-5-5”, diagnoza i następny krok z runbooka. Przez pierwsze sekundy karta może pokazywać ładowanie. Niżej **Fixed before** (A-n i notatka) i podobne incydenty. |
| 4 | Linia właściciela. | „In charge: Anna Kowalska · assigned automatically (on call for Team Payments)” oraz przycisk **Hand to a colleague**. |
| 5 | **Ask about this alert**: „what should I check first?” | Odpowiedź w drawerze, wyświetlana na bieżąco. |
| 6 | **Mailpit** (`:8025`) i **mockgw** (`:8090`). | Mail lub SMS do Anny z tytułem, polami, krokami runbooka i linkiem. Kanał zależy od jej preferencji i pory dnia (SMS 08–20, poza tym email). |
| 7 | Zaakceptuj sugestię AI (**Accept**). | Decyzja trafia na oś czasu. Jeśli krok ma akcję, wykonuje się. Krok ryzykowny trafia do **Approvals**. |
| 8 | **Resolve**. | Panel „How did you fix it?” z notatką zaproponowaną przez AI, którą można edytować. Zapisz. |
| 9 | Wyślij to samo zdarzenie jeszcze raz. | Nowy alert z panelem „Fixed before”, który pokazuje poprzedni alert i Twoją notatkę. |

## B. Pozostałe scenariusze

| # | Co zrobić | Oczekiwane |
| --- | --- | --- |
| 1 | **Rules → New rule**: w polu **AI assistant** wpisz „critical errors from payments-api go to Anna by SMS between 8 and 20”. | Draft wypełnia normalny formularz (warunki, odbiorcy, kanały, okno czasowe) z wyjaśnieniem i dry-runem. **Save** zapisuje regułę, a **Undo the AI draft** cofa zmiany. |
| 2 | Edytor reguł ręcznie: AND/OR, pole zagnieżdżone, `>`. Zapisz i wyślij zdarzenie. | Alert pojawia się bez restartu. |
| 3 | **Runbooks → Paste any runbook**: wklej wolny tekst i kliknij **Write with AI**. | Kroki z akcjami i znacznikiem „needs approval”, opisane jako „AI draft, needs review”. **Save** zapisuje dokładnie te kroki. **Split into steps** działa na stałych regułach, bez AI. |
| 4 | Alert Settlements jako `piotr`: krok „retry workflow”. | Wykonany od razu (mockgw), wpis na osi czasu. |
| 5 | Krok „escalate” jako `piotr`, potem akceptacja jako `piotr`. | Krok czeka na akceptację. Akceptacja przez Piotra zostaje odrzucona (**four-eyes**). |
| 6 | Viewing as `root.settlements` → **Approvals** → Approve. | Akcja wykonana, a w logu wpis „approved by …”. |
| 7 | mockgw: `ACK <n>` z telefonu on-call. | Alert potwierdzony, eskalacja zatrzymana. |
| 8 | Test lab → Simulate an outage (SMS), potem nowe zdarzenie. | SMS: ponowienia, potem DLQ, potem email fallback w Mailpit. Nic nie ginie. |
| 9 | Symulator → `storm_500`. | Jeden alert ×500 i jedno powiadomienie. |
| 10 | Symulator → `poisoned_log`. | Baner ostrzegający przed podejrzaną treścią. AI nie proponuje akcji. |
| 11 | **Platforms → Connect**: nazwa, `curl` z dialogu, potem **Open**. | Zdarzenie widoczne na platformie jako „unrouted”. Po dodaniu reguły kolejne zdarzenie tworzy alert. |
| 12 | Symulator → `payments_error_spike` (potem automatycznie `resolved`). | Alert dla Anny, który zamyka się sam po „resolved”. |
| 13 | **Teams → Change my notification preferences**. | Następny alert idzie nowym kanałem. |
| 14 | **Agents & MCP**: utwórz token i skopiuj komendę `claude mcp add …`. W Claude Code: „list open alerts”, „request approval for terraform_apply”. | Narzędzia działają, a prośba agenta trafia do **Approvals**. Token można unieważnić. |
| 15 | **Hand to a colleague** na alercie. | Nowy właściciel. Auto-przypisanie już go nie nadpisze. |
| 16 | **Metrics**. | events/s, p50/p99, Lost = 0. |

## C. Znane ograniczenia (świadome)
- Logowanie działa tylko w trybie demo („Viewing as”), bez SSO.
- Auto-przypisanie działa według prostego modelu if/else. Zamiana na inny model: `backend/app/escalation/auto_assign.py::decide`.
- `resolve_when` z reguł Herald jest tylko doradcze. Automatyczne zamykanie działa dla Alertmanagera (`status: resolved`).
- GitHub Actions jest zablokowane przez billing przy prywatnym repo, więc deploy idzie z CLI.
