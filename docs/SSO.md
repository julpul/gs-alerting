# Sign-in (password and single sign-on)

The Herald web UI has three sign-in modes, chosen by `AUTH_MODE`:

| Mode | Who you are | Use for |
| --- | --- | --- |
| `demo` (default) | Whoever you pick under "Viewing as" (`X-Herald-User` header). No login page. | Local dev, tests, the hackathon demo |
| `password` | The user you sign in as, with username + password from our `users` table | A server without an identity provider |
| `oidc` | The user the identity provider says you are ("Continue with SSO"); the password form stays as the emergency way in | Production with Entra ID, Keycloak, ... |

In `password` and `oidc` mode identity comes **only** from a server-side session: an `HttpOnly`, `SameSite=Lax`
cookie (`Secure` when the site is served over https) holding a random token; Redis holds the session for 8 hours
(`SESSION_TTL_SECONDS`). `X-Herald-User`, `X-Herald-Role` and Bearer tokens are ignored on `/api/*`
(`401 sign in required`), and `/` redirects to `/login` until you are signed in, then back to the page you asked for
(`next` is a same-origin path only). `/v1` keeps its Bearer-token auth and MCP its personal access tokens.

## Endpoints

| Endpoint | What |
| --- | --- |
| `GET /auth/providers` | Buttons for the sign-in page (404 in demo mode) |
| `GET /auth/login?provider=sso&next=/` | Starts OpenID Connect (code flow + PKCE, state and nonce sealed in a cookie) |
| `GET /auth/callback` | Provider redirect target: verifies the ID token (RS256, JWKS, issuer, audience, nonce), opens the session |
| `POST /auth/password` | `{username, password, next}`; username is the e-mail or its local part (`anna`) |
| `POST /auth/logout` | Ends the session; with SSO the answer's `logout_url` ends the provider session too |
| `GET /api/me` | Who is signed in (`mode` is `demo`, `password` or `oidc`) |

Failed passwords are limited per username + IP (5 in 60 s, then `429`). Every `login`, `logout`, `login_denied`
(signed in at the provider but no `users` row) and `login_locked` is written to the audit log.

## Mapping people

The provider only proves who someone is. We match `email` (then `preferred_username`) to our `users` row; roles,
teams and scopes stay in our database. Someone the provider accepts but who has no `users` row sees "You don't have
access to Herald yet" and the attempt is audited as `login_denied`.

## Run each mode

**Demo** (default): `make up`. Nothing to configure.

**Password**: set in `.env` (or the server's environment) and restart the api:

```
AUTH_MODE=password
APP_SECRET=<random, at least 32 characters>
JWT_SECRET=<random>
DEMO_PASSWORD=<one password for every seeded demo user>   # optional, used by the seed only
```

The api refuses to start in `password`/`oidc` mode with the built-in secrets. Re-run the seed (`make seed`) after
setting `DEMO_PASSWORD`; it then replaces every demo user's password (without it they keep `demo`).

**SSO with the local Keycloak** (the demo "SSO mockup"):

```
AUTH_MODE=oidc APP_SECRET=<32+ chars> JWT_SECRET=<random> \
  docker compose -f infra/docker-compose.yml --profile sso up -d --build
```

Keycloak runs on <http://localhost:8180> (admin console login: `admin`, password in
`infra/docker-compose.yml` as `KEYCLOAK_ADMIN_PASSWORD`). It imports realm `vistula` from
`infra/keycloak/vistula-realm.json`, with client `herald` (redirect `http://localhost:8080/auth/callback`).
Open <http://localhost:8080>, press "Continue with SSO", sign in as one of the users below. The api reaches
Keycloak as `keycloak:8080` (`OIDC_DISCOVERY_URL`) while browsers use `localhost:8180` (`OIDC_ISSUER`).

Demo users (username; e-mail `<username>@demo` = our `users.email`). **Their throwaway passwords are in
`infra/keycloak/vistula-realm.json`** (`<username>-vistula-demo`); they exist only in that local Keycloak.

| Username | Role in Herald |
| --- | --- |
| `master` | Master: every team, every domain |
| `root.payments`, `root.settlements`, `root.security`, `root.markets` | Domain Root (approver) |
| `anna` | Responder, team Payments |
| `piotr` | Responder, team Settlements |
| `manager` | Informed |
| `marta.nowak`, `tomasz.kaminski`, `ewa.wisniewska`, `james.hartley` | Members of seeded teams |

**Entra ID (or any OIDC provider)** is configuration only:

```
AUTH_MODE=oidc
OIDC_ISSUER=https://idp.example.com/realms/corp       # exact `issuer` of the discovery document
OIDC_CLIENT_ID=herald
OIDC_CLIENT_SECRET=...
OIDC_LABEL="Continue with SSO"
OIDC_DISCOVERY_URL=                                    # only if the api reaches the provider by another name
# Entra ID: one tenant (a GUID, never `common`); adds a "Continue with Microsoft" button
ENTRA_TENANT_ID=... ENTRA_CLIENT_ID=... ENTRA_CLIENT_SECRET=...
PUBLIC_BASE_URL=https://herald.example.com             # redirect URI = <PUBLIC_BASE_URL>/auth/callback
TRUST_FORWARDED_FOR=true                               # behind a proxy that sets X-Forwarded-For
```

Register `<PUBLIC_BASE_URL>/auth/callback` as the redirect URI at the provider, make sure it puts `email` (or
`preferred_username`) in the ID token, and make sure each person exists in Herald's `users`.

## Not covered

No TOTP for the password form, no step-up re-authentication for risky actions, no SCIM/just-in-time user creation,
no refresh of roles from the provider (roles live in our database).
