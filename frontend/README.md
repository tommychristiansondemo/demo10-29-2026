# PharmaPulse Front-End

React single-page application (TypeScript + Vite) providing the PharmaPulse
chat experience (Requirement 13). This task (17.1) delivers:

- A **streaming chat panel** that renders agent responses as streaming output
  by consuming SSE tokens from the backend `POST /chat` endpoint
  (Requirements 13.1, 13.5).
- **Cognito hosted-UI login/logout** controls: the app redirects to the
  Cognito hosted UI authorize/logout endpoints, stores/clears the JWT, and
  attaches it as a bearer token on `/chat` requests (Requirement 13.3).

The tool-call trace panel and order approval dialog (task 17.2) and the
component tests (task 17.3) are **not** implemented here, but the app is
structured so they drop in without reshaping the streaming layer:

- `useChat` already collects `toolTrace` and `pendingApproval` from the stream.
- `ChatPanel` exposes a `traceSlot` prop for the trace panel.
- `types.ts` defines `ToolTraceEntry` and `PendingApproval`.

## Project layout

```
frontend/
  index.html
  package.json
  tsconfig.json
  vite.config.ts
  .env.example            # configuration template (no secrets)
  src/
    main.tsx              # app entry
    App.tsx               # layout: header + chat panel
    config.ts             # reads VITE_* env vars
    types.ts              # chat / stream / auth types
    styles.css
    api/
      chatStream.ts       # fetch + ReadableStream SSE client
    auth/
      cognito.ts          # hosted-UI login/logout + token storage
      useAuth.ts          # React auth hook
    hooks/
      useChat.ts          # conversation + streaming state
    components/
      ChatPanel.tsx
      ChatMessage.tsx
      LoginControls.tsx
```

## Configuration

All configuration comes from `VITE_*` environment variables — **no secrets in
source**. The Cognito hosted UI uses a public OAuth client, so only public
values (domain, client id, redirect URIs) are referenced.

Copy `.env.example` to `.env.local` and fill in your deployment values:

| Variable | Purpose |
|---|---|
| `VITE_CHAT_API_URL` | Base URL of the streaming backend (`/chat` is appended) |
| `VITE_COGNITO_DOMAIN` | Cognito hosted-UI domain |
| `VITE_COGNITO_CLIENT_ID` | Cognito app client id (public) |
| `VITE_COGNITO_REDIRECT_URI` | Post-login callback URL (defaults to window origin) |
| `VITE_COGNITO_LOGOUT_URI` | Post-logout URL (defaults to window origin) |
| `VITE_COGNITO_SCOPE` | OAuth scopes (default `openid email profile`) |

## Scripts

```bash
npm install        # install dependencies
npm run dev        # start the Vite dev server (http://localhost:5173)
npm run typecheck  # tsc --noEmit
npm run build      # type-check then produce a production build in dist/
npm run preview    # preview the production build
```

## Backend SSE contract

The chat client (`src/api/chatStream.ts`) consumes Server-Sent Events from
`POST /chat`. It is tolerant of two encodings so it works with the backend as
built in task 15:

- **Typed JSON per `data:` frame** (preferred): `{"type":"token","text":"..."}`,
  `{"type":"tool", ...}`, `{"type":"approval", ...}`, `{"type":"error", ...}`,
  `{"type":"done"}`.
- **Named SSE events**: `event: token` / `event: tool` / `event: approval` /
  `event: error` / `event: done`, plus the `[DONE]` sentinel.

Unlabeled `data:` frames are treated as raw assistant tokens.
