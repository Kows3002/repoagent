# RepoAgent backend

RepoAgent uses GitHub OAuth to connect an account, lists repositories the account can push to, and generates focused code changes. Users review the git diff and, for supported projects, real before-and-after UI previews. Explicit approval commits with `RepoAgent: Apply requested changes` and pushes directly to the repository's default branch. It does not create a branch or pull request.

## Configure GitHub sign-in

1. Register a [GitHub OAuth App](https://github.com/settings/developers).
2. Set the homepage URL to the browser-visible workspace origin. Register that origin followed by `/auth/github/callback` as the authorization callback. Locally, use `http://127.0.0.1:5173/auth/github/callback`; Vite proxies the callback to the API.
3. Copy `.env.example` to `.env`, enter the OAuth App credentials and Groq key, and generate one persistent application secret.

```sh
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

| Variable | Purpose |
| --- | --- |
| `GITHUB_CLIENT_ID` | GitHub OAuth App client ID. |
| `GITHUB_CLIENT_SECRET` | OAuth client secret; backend only. |
| `GITHUB_CALLBACK_URL` | Exact callback URL registered with GitHub. |
| `SESSION_SECRET` | Persistent random value of at least 32 characters. Used for signed sessions, token encryption, and preview capabilities unless `APP_SECRET` is set. |
| `APP_SECRET` | Optional override for `SESSION_SECRET`; when present it must also contain at least 32 characters. Keep the effective secret identical across API instances. |
| `GROQ_API_KEY` | Key used by the code editing pipeline. |
| `DATABASE_URL` | Optional PostgreSQL or SQLite URL; defaults to `sqlite:///./repoagent.db`. |
| `FRONTEND_URL` | Absolute destination after login; defaults to `http://127.0.0.1:5173`. Also supplies a trusted POST origin and preview frame ancestor. |
| `CORS_ORIGINS` | Additional comma-separated frontend origins allowed for credentialed requests and POST origin checks. |
| `SESSION_HTTPS_ONLY` | Optional secure-cookie override; defaults to true for an HTTPS callback and false for local HTTP. |
| `SESSION_SAME_SITE` | Session cookie policy: `lax` (default) or `none`. `none` requires secure cookies and an HTTPS API. The OAuth flow cookie always uses `lax`. |

Install dependencies and start the API from `backend`:

```sh
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

The API requires an effective secret of at least 32 characters to start. Keep it stable across restarts and deployments. Rotating it invalidates signed sessions and prevents decryption of saved OAuth credentials; users must authorize again. If OAuth credentials are missing, `GET /auth/session` reports `configured: false` and the frontend explains that sign-in is unavailable.

Authorization uses a browser-bound, single-use state and S256 PKCE. It requests GitHub's `repo` and `read:user` scopes. Organization OAuth restrictions and branch protections still apply.

## PostgreSQL from a local computer

When running Uvicorn on your computer with a Render database, set `DATABASE_URL`
in `backend/.env` to the **External Database URL** from the database's **Connect
> External** menu. Use `sslmode=require` in the URL's query parameters. Keep the
URL private because it contains the database password.

A hostname such as `dpg-...-a` without a domain is Render's internal address. It
is for Render services on the same private network and does not resolve from
your Windows computer. A startup error saying `could not translate host name`
for this address requires the external connection URL, not a different password
or a change to the OAuth code. Use the exact external address from your database
dashboard; its region cannot be inferred from the internal hostname.

Keep the internal connection URL configured on the deployed Render service.
After changing your local `.env`, stop and restart Uvicorn; Python's file reloader
does not reload environment-file changes. If PowerShell also has a
`DATABASE_URL` environment variable, update or remove that override because it
takes precedence over `.env`.

See [Render's database connection documentation](https://render.com/docs/postgresql-creating-connecting#connect-to-your-database).

## Sessions and frontend integration

The signed, HttpOnly cookie contains only an opaque random session identifier and defaults to `SameSite=Lax`. Its hashed identifier resolves to a database session with a maximum lifetime of seven days, capped sooner when GitHub returns a shorter token expiry. Public user identity, expiry, and CSRF state live on the server. GitHub access tokens are stored as Fernet ciphertext in `users.access_token` and are never returned by the API.

Every POST requires both a valid `X-CSRF-Token` header and a trusted `Origin` (or a trusted `Referer` origin when Origin is absent). Fetch the token from `GET /auth/session`; keep it in memory and send requests with `credentials: "include"`. The token is an anti-forgery value, not a GitHub credential.

Use same-origin `/auth` and `/jobs` routes through a reverse proxy in production. The local Vite proxy provides both routes during development. The browser-visible login and callback hosts must match; do not mix `localhost` and `127.0.0.1`. A direct API origin needs the matching registered callback, exact credentialed CORS origins, and browser-compatible cookie settings. A same-origin proxy avoids cross-site cookie restrictions.

For a frontend on a different site calling `https://repoagent.onrender.com` directly, configure the backend with your actual frontend origin:

```dotenv
GITHUB_CALLBACK_URL=https://repoagent.onrender.com/auth/github/callback
FRONTEND_URL=https://your-frontend.example
CORS_ORIGINS=https://your-frontend.example
SESSION_SAME_SITE=none
SESSION_HTTPS_ONLY=true
```

Register that exact API callback in the GitHub OAuth App. The frontend should send credentials with every API request (`credentials: "include"` for Fetch or `withCredentials: true` for Axios), along with the CSRF header on POST requests. Invalid cookie policies and `SameSite=None` without secure cookies prevent startup, rather than silently creating a broken login. The short-lived OAuth flow cookie stays `SameSite=Lax` so the browser can send it on GitHub's top-level callback navigation.

Browsers can still block third-party cookies even with `SameSite=None; Secure`. Prefer a same-origin proxy when that restriction affects your users; CORS and cookie attributes cannot override browser privacy settings. See the [cookie attribute documentation](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Set-Cookie#samesitesamesite-value).

| Endpoint | Contract |
| --- | --- |
| `GET /auth/github/login` | Redirects the browser to GitHub authorization. |
| `GET /auth/github/callback` | Exchanges the code, stores encrypted credentials, creates a database session, and redirects to `FRONTEND_URL`. |
| `GET /auth/session` | `{ authenticated, configured, user, csrf_token? }`; user fields are `id` (GitHub ID), `login`, `name`, and `avatar_url`. |
| `GET /auth/me` | Compatibility response with internal `id`, `github_id`, `username`, `avatar_url`, and `csrf_token`; 401 when signed out. |
| `GET /auth/repositories?page=1` | `{ repositories, has_more, next_page }`; entries include `id`, `full_name`, `clone_url`, `default_branch`, `private`, `description`, and `language`. |
| `POST /auth/logout` | Deletes the database session and clears the cookie. |

Repository pages include accessible repositories with push permission, excluding archived and disabled repositories. The frontend searches the loaded list and requests more pages as needed. Users no longer submit PATs.

Create a job with the authenticated session and CSRF header:

```http
POST /jobs/
Content-Type: application/json
X-CSRF-Token: <value from /auth/session>
Origin: http://127.0.0.1:5173
```

```json
{
  "repo_url": "https://github.com/your-account/your-repository.git",
  "task": "Modify ONLY src/pages/Login.jsx. Replace the page title."
}
```

The API responds with a queued job and runs cloning, analysis, and patch generation in a background thread. Poll `GET /jobs/{id}` for progress. `POST /jobs/{id}/approve` requires a completed job with changes and pushes to the current default branch. Protected branches may reject direct pushes. Each user can list, read, preview, and approve only their own jobs.

## Real UI previews

The worker captures immutable, filtered snapshots immediately after cloning and after applying the patch. Preview builds use those snapshots, so the Current frame represents the original code even after approval. Preview preparation never commits or pushes.

- `POST /jobs/{id}/preview` starts a build for a completed, owned job and returns 202.
- `GET /jobs/{id}/preview` reports `idle`, `building`, `ready`, `unsupported`, or `failed`.
- A ready response includes short-lived `before_url`, `after_url`, and `expires_at`.
- The frontend displays rendered UI in sandboxed frames with comparison, desktop/mobile, and matching page-path controls.
- Old jobs without both snapshots need a new generation to produce this comparison. Preview failures do not remove the generated diff or prevent its review.

Supported projects are static HTML sites with `index.html`, and standalone Vite applications such as React + Vite. Detection checks the repository root and common frontend folders. Static assets are copied without executing repository code.

Vite builds require the Docker CLI and a running Linux container engine on the backend host. Start Docker Desktop on Windows, or the Docker service on a Linux deployment, before requesting a Vite preview. The default builder image is `node:22-bookworm-slim`; the host must be able to pull it and reach the public npm registry.

A dependency-install container receives only a generated manifest. It installs registry dependencies with lifecycle scripts disabled, without repository credentials, npm configuration, or lockfiles. Repository build code then runs in a separate non-root container with network access disabled, resource limits, a read-only source snapshot, and disposable output. The API host does not execute the repository's build commands.

Automatic previews do not support server-rendered frameworks such as Next.js, workspace/local/git dependencies, private packages, custom installation scripts, or arbitrary application servers. Dependency versions are resolved from the manifest rather than the repository lockfile. These projects may require a custom isolated builder.

Repository `.env` files, hidden files, symlinks, and known secret/key files are excluded from snapshots. Preview pages cannot use backend APIs, external services/assets, app authentication, workers, forms, or browser storage. Apps depending on those features may render partially or fail. These are isolated UI artifacts, not a full staging environment.

## Visual preview deployment

Local defaults use a different capability hostname for each preview:

```dotenv
PREVIEW_DOMAIN=localhost
PREVIEW_SCHEME=http
PREVIEW_PORT=8000
PREVIEW_BUILDER_IMAGE=node:22-bookworm-slim
```

The frontend must use `VITE_PREVIEW_ORIGIN=http://*.localhost:8000`. Local preview frames connect directly to the API listener through `<capability>.localhost`; the application itself may remain at `127.0.0.1:5173`. If you change the preview listener's port, change both settings.

For production, use a dedicated wildcard hostname separate from the workspace/API origin, with wildcard DNS and HTTPS certificates:

```dotenv
# backend
PREVIEW_DOMAIN=preview.example.net
PREVIEW_SCHEME=https
PREVIEW_PORT=
PREVIEW_STORAGE_PATH=/var/lib/repoagent/previews

# frontend build environment
VITE_PREVIEW_ORIGIN=https://*.preview.example.net
```

Route `*.preview.example.net` to the backend while preserving the original Host header. Preview middleware intercepts that hostname before application sessions and routes; those hosts expose only read-only preview assets, never API endpoints. Do not serve repository HTML directly on the main application origin. Configure application cookies as host-only, and do not widen their domain to the preview hosts.

Preview URLs act as short-lived capabilities and remain bound to a live database session and its owned job. Signing out revokes that session's preview links. The frames use a restrictive content policy that blocks network connections and prevents access to the parent application.

`PREVIEW_STORAGE_PATH` defaults to `backend/preview_data`. Persist it together with repository workspaces when jobs must remain reviewable across restarts. Preview artifacts, snapshots, and capability metadata are stored there; provide an operator-managed retention policy. `PREVIEW_BUILDER_IMAGE` can select a compatible Node image, including a pinned digest.

The frontend embeds its allowed wildcard frame origin in a build-time Content Security Policy. Rebuild it when the preview hostname, scheme, or port changes, and align any reverse-proxy CSP. Production preview domains require HTTPS.

Job and preview background work is process-local. A restart can interrupt processing, and scaling requires shared database/workspace/preview storage plus compatible routing or a durable worker design.

## Existing databases

Startup runs an idempotent, additive migration for PostgreSQL or SQLite. It creates missing user/session/OAuth tables, adds `jobs.user_id` and its index, and preserves existing job IDs, diffs, statuses, and workspaces. PostgreSQL schema changes are serialized across API instances.

Legacy columns, including an old per-job PAT column if present, remain physically in the database but are no longer mapped by the ORM or returned by the API. This migration does not erase historical credentials; any data cleanup requires a separate deliberate migration.

Historical jobs keep a null owner because ownership cannot be established safely. Authenticated endpoints do not expose those jobs to any user. New jobs belong to the account that created them. Back up an existing database before deployment.

## Verification

```sh
python -m unittest discover -s tests -v
```

The automated suite uses temporary databases/workspaces and mocked GitHub, Groq, and Docker calls where required. It covers OAuth/session/CSRF behavior, owned jobs, migrations, snapshot/build rules, and isolated preview delivery. No test signs into a real GitHub account or pushes a commit.

The Docker build path has command/contract coverage, but a live Docker Vite build was not verified in this workspace because a Docker daemon was unavailable. Verify that path on a host with Docker before relying on Vite previews in deployment.

