# Herald on Atlassian's components

The same service and API as the plain-file UI in `ui/`, rebuilt with Atlassian Design System packages
(`@atlaskit/*`) so the two can be compared side by side.

| | Address | Source |
|---|---|---|
| Plain-file UI | `http://localhost:8080/` | `backend/static/herald/index.html` |
| This one | `http://localhost:8080/ads/` | `ui/ads/src/`, built into `backend/static/herald/ads/` |

The built files are committed and served by the api itself (`backend/app/api/mounts/herald_ui.py`, same origin
as `/v1`, `/api` and `/mcp`), so `docker compose up` needs no Node.

## Change it

```bash
npm install
```

```bash
npm run build
```

`npm run build` type-checks and writes `../../backend/static/herald/ads/`; reload the page. `npm run dev` gives hot reload on
`http://localhost:5173` and forwards API calls to the running service. Under single sign-on the dev
server can only read (the API refuses changes that come from another origin), so use it with
`AUTH_MODE=demo`, or build and use `/ads/`.

## What is used

- Shell: `@atlaskit/navigation-system` (top bar, sidebar) and `@atlaskit/side-nav-items`.
- Screens: `dynamic-table`, `lozenge`, `modal-dialog`, `tabs`, `select`, `textfield`, `textarea`, `toggle`,
  `flag`, `section-message`, `empty-state`, `breadcrumbs`, `avatar`, `badge`, `code`, `dropdown-menu`.
- Board: `@atlaskit/pragmatic-drag-and-drop`.
- Motion: `@atlaskit/motion` (`FadeIn`, `StaggeredEntrance`) plus the motion each component brings
  (modals, flags, tabs, menus). People who ask their system for reduced motion get none.
- Colour, space, radius, shadow, type: design tokens only (`src/styles.css` holds layout and uses `var(--ds-*)`).

## Screens on the public /v1 API

The Herald screens talk to `/api/*` (the backend-for-frontend). The newer screens call the public contract
(`docs/openapi.yaml`) directly through `src/v1.ts`: it gets a short-lived token for the current "Viewing as"
person from `GET /api/session-token` (in demo mode without that endpoint it signs in as `<person>@demo`),
refreshes it on 401, keeps one shared `EventSource` on `/v1/stream` for live updates, and streams the copilot
with `fetch` (an `EventSource` cannot POST).

| Screen | Route | Endpoints |
|---|---|---|
| Alert view additions | `#/alerts?alert=<n>` | `GET /v1/alerts/A-<n>`, `…/suggestion`, `…/suggestion/{id}/decision`, `…/resolution-note-draft`, `POST …/resolve` |
| Approvals | `#/approvals` | `GET /v1/approvals?status=`, `POST /v1/approvals/{id}/decision` |
| Copilot | `#/copilot[/A-<n>]` | `POST /v1/agent/chat` (SSE) |
| AI assistant | `#/assistant/rule\|mapping\|runbook` | `POST /v1/rules/from-text`, `/v1/rules/dry-run`, `/v1/rules`, `/v1/sources/{id}/mapping/suggest`, `PUT …/mapping`, `/v1/runbooks/parse`, `/v1/runbooks` |
| Agents & MCP | `#/agents` | `GET/POST/DELETE /v1/tokens`, `/api/audit` (optional) |
| On-call | `#/oncall` | `GET /v1/teams`, `/v1/teams/{id}/oncall`, `/v1/escalation-policies/effective` |
| Knowledge | `#/knowledge` | `GET/POST /v1/knowledge` |
| Health | `#/health` | `GET /v1/metrics/summary` every 5 s |

Every AI output is framed by `AiDraft` (`src/ai.tsx`): labelled, with Accept / Edit / Reject, never applied
without a click.

## Things that are easy to trip over

- `.npmrc` sets `legacy-peer-deps`: `@compiled/vite-plugin` asks for `@compiled/react` 2, the Atlaskit
  packages bring version 1. The plugin is needed only to put Atlassian's pre-built CSS in the right order.
- `package.json` must not say `"type": "module"`: the icon files are CommonJS and their default export
  breaks under that setting.
- `@atlaskit/dropdown-menu` 22 has no default export; import `DropdownMenu` from `@atlaskit/dropdown-menu/dropdown-menu`.

## Not rebuilt here

Visual rule editor, runbook editor, phone and mailbox stand-ins: open the plain-file UI. The rule list is read-only
here; new rules can be drafted from plain words in the AI assistant.
