# Sign-in (password and single sign-on)

The Herald web UI has three sign-in modes, chosen by `AUTH_MODE`:

| Mode | Who you are | Use for |
| --- | --- | --- |
| `demo` (default) | Whoever you pick under "Viewing as" (`X-Herald-User` header). No login page. | Local dev, tests, the hackathon demo |
| `password` | The user you sign in as, with username + password from our `users` table | A server without an identity provider |
| `oidc` | The user the identity provider says you are ("Continue with SSO"); the password form stays as the emergency way in | Vistula ID (demo); production with Entra ID, Okta, ... |

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

**Demo** (pytest, local api on `make infra-up`): `AUTH_MODE=demo`, nothing else.

**SSO with Vistula ID: the default of the compose stack (`make up` / `make rebuild`) and of the Railway demo.**
Vistula ID (`sso/`) is our own small OpenID Connect provider, standing in for the corporate one. It implements the real
protocol: discovery, authorization code with PKCE (S256), RS256-signed ID tokens with a JWKS, userinfo and
end-session. It also has an account picker ("Pick an account") once you are signed in, which is what makes it single
sign-on. It looks like the company sign-in of our demo organisation, Vistula Capital. Herald does not know it is a demo:
replacing it with Entra ID or Okta is configuration only (below).

| | Local | Railway |
| --- | --- | --- |
| Herald | http://localhost:8080 → "Sign in with Vistula ID" | https://api-production-d033bedd.up.railway.app |
| Vistula ID | http://localhost:8180 (browser), `sso:8180` (api, `OIDC_DISCOVERY_URL`) | https://sso-production-b4223540.up.railway.app |

- **Accounts:** `sso/users.json`, the same people as the seed (`anna`, `piotr`, `master`, `root.*`, `manager`, …), with e-mail `<username>@demo`. Herald matches them by e-mail.
- **Password:** one demo password for every account: `SSO_PASSWORD`, or `DEMO_PASSWORD` when that is unset. It is never in the repo: locally it lives in `.env`, the list of logins is in `LOGINS.local.md` (git-ignored), and on Railway it is a service variable.
- **Shared client secret:** `OIDC_CLIENT_SECRET` on the api is the same value as `SSO_CLIENT_SECRET` on the provider.
- **Provider variables:**
  - `SSO_ISSUER` (public URL)
  - `SSO_CLIENT_ID` / `SSO_CLIENT_SECRET`
  - `SSO_REDIRECT_URIS` (comma list of exact callback URLs)
  - `SSO_PASSWORD`
  - `SSO_SESSION_SECRET`
  - `SSO_PRIVATE_KEY` (PEM; without it a key is generated at start and the api fetches the new JWKS by itself)
- **Tests:** `cd backend && uv run pytest ../sso/tests`.

**Password only** (no SSO button): `AUTH_MODE=password`. In both signed-in modes the username + password form on
`/login` stays as the emergency way in, for when the identity provider is down. It uses the same seed password,
rate limited and audited.

```
AUTH_MODE=oidc | password
APP_SECRET=<random, at least 32 characters>
JWT_SECRET=<random>
DEMO_PASSWORD=<one password for every seeded demo user>   # the seed (make seed / make seed-prod) applies it
```

The api refuses to start in `password`/`oidc` mode with the built-in secrets. In the signed-in modes "Viewing as" is
gone. Admins (`master`) get "Preview as level (testing)" in the avatar menu, and "Sign out" is its last item: it ends
the Herald session and the Vistula ID session, then returns to `/login`.

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
