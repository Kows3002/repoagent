# RepoAgent backend

RepoAgent uses a GitHub App to connect an account, lists only repositories authorized for that App where the account can push, and generates focused code changes. Users review the git diff and, for supported projects, real before-and-after UI previews. Explicit approval commits with the editable message (default: `RepoAgent: Apply requested changes`) and pushes directly to the repository's default branch. It does not create a branch or pull request.

## Configure GitHub sign-in

1. Open **GitHub > Settings > Developer settings > GitHub Apps > New GitHub App** ([registration](https://github.com/settings/apps/new)). An existing OAuth App cannot provide selected-repository permissions; create a GitHub App for this version.
2. Set the homepage to your frontend URL. Set **Callback URL** to exactly `https://repoagent.onrender.com/auth/github/callback` in production, or `http://127.0.0.1:8000/auth/github/callback` locally. Set **Setup URL** to your frontend URL so installation can return there.
3. Leave **Request user authorization (OAuth) during installation** unchecked. RepoAgent starts its own browser-bound authorization from **Continue with GitHub**. Leave user-token expiration enabled. Disable webhooks; this integration does not require a webhook or App private key.
4. Under **Repository permissions**, set **Contents: Read and write**. **Metadata: Read-only** is supplied by GitHub. Leave unrelated permissions off. Editing workflow files requires GitHub's additional Workflows write permission; only enable it if your application needs that feature. Existing branch protections still apply.
5. Allow installation on **Any account** if other users should connect repositories; choose **Only on this account** for a private personal tool. Create the App and generate a **client secret**. Copy its **Client ID** (not numeric App ID), secret, and the slug from `https://github.com/apps/YOUR-SLUG` into the backend environment below.
6. Copy `.env.example` to `.env` for local work, or set these variables in Render for production. Keep your Groq key and generate one persistent application secret:

```sh
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

| Variable | Purpose |
| --- | --- |
| `GITHUB_APP_SLUG` | Slug from the GitHub App public URL; required for repository selection links and installation verification. |
| `GITHUB_CLIENT_ID` | GitHub App Client ID, not its numeric App ID and not an OAuth App client ID. |
| `GITHUB_CLIENT_SECRET` | GitHub App client secret; backend only. |
| `GITHUB_CALLBACK_URL` | Exact callback URL registered with GitHub. |
| `SESSION_SECRET` | Persistent random value of at least 32 characters. Used for signed sessions, token encryption, and preview capabilities unless `APP_SECRET` is set. |
| `APP_SECRET` | Optional override for `SESSION_SECRET`; when present it must also contain at least 32 characters. Keep the effective secret identical across API instances. |
| `GROQ_API_KEY` | Key used by the code editing pipeline. |
| `DATABASE_URL` | Optional PostgreSQL or SQLite URL; defaults to `sqlite:///./repoagent.db`. |
| `FRONTEND_URL` | Required absolute destination after login. Also supplies a trusted POST origin and preview frame ancestor. No localhost fallback. |
| `CORS_ORIGINS` | Additional comma-separated frontend origins allowed for credentialed requests and POST origin checks. |
| `SESSION_HTTPS_ONLY` | Optional secure-cookie override; defaults to true for an HTTPS callback and false for local HTTP. |
| `SESSION_SAME_SITE` | Defaults to `lax` for HTTP development and `none` with secure cookies for HTTPS. An explicit value overrides the default. The OAuth flow cookie always uses `lax`. |

Install dependencies and start the API from `backend`:

```sh
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

The API requires an effective secret of at least 32 characters to start. Keep it stable across restarts and deployments. Rotating it invalidates signed sessions and prevents decryption of saved OAuth credentials; users must authorize again. If GitHub App credentials or the App slug are missing, `GET /auth/session` reports `configured: false` and the frontend explains that sign-in is unavailable.

Authorization uses a browser-bound, single-use state and S256 PKCE. It requests no broad OAuth scopes. GitHub App user tokens are restricted by both the user's permissions and the App installation. RepoAgent additionally verifies installation membership before creating a job, before the worker clones, and again before approval pushes. A public repository outside the installation is also rejected. [GitHub documents App user authorization](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app) and [installation repository access](https://docs.github.com/en/rest/apps/installations#list-repositories-accessible-to-the-user-access-token).

### Choose repositories after deploying

1. Deploy the backend first (startup adds the credential-binding columns), then the updated frontend. Configure API and workers with the same `GITHUB_CLIENT_ID`, `GITHUB_APP_SLUG`, and effective encryption secret (`APP_SECRET` or `SESSION_SECRET`). Workers do not need the client secret to use a stored user token.
2. Sign in using **Continue with GitHub**. Existing broad OAuth sessions deliberately return `authenticated: false`; every user authorizes the new App once.
3. Click **Choose repositories on GitHub**, select your account or organization, choose **Only select repositories**, pick the repositories, then **Install** or **Save**. Organization owners may need to approve installation.
4. Return to RepoAgent and refresh repository access. Only App-authorized repositories with both Contents write and user push permission are offered. Use **Manage access** to add or remove repositories later. GitHub also allows an explicit **All repositories** installation; choose **Only select repositories** to restrict it.
5. If migrating from the old OAuth integration, revoke its broad grant at **GitHub > Settings > Applications > Authorized OAuth Apps** after switching credentials. The migration does not revoke GitHub grants remotely. Do not revoke the newly authorized GitHub App.

Tokens and sessions expire after at most eight hours; sign in again when asked. Refresh tokens are not stored. GitHub repository selections are fetched afresh, not persisted as a client-side allowlist. Removing a repository prevents future jobs and pushes, including pending jobs created before removal. Existing job history and diffs remain in the account for review.

Use the matching pair of URLs for each deployment:

| Environment | `GITHUB_CALLBACK_URL` | `FRONTEND_URL` |
| --- | --- | --- |
| Local | `http://127.0.0.1:8000/auth/github/callback` | `http://127.0.0.1:5173` |
| Production | `https://repoagent.onrender.com/auth/github/callback` | `https://repoagent-frontend.vercel.app` |

Set production values in the Render service environment and register the matching
callback with GitHub (use separate GitHub Apps for local and production if needed).
Local `.env` edits do not update Render. Restart or redeploy after environment
changes, then start a fresh login. An authorization URL containing a local callback
means the backend that generated it is configured with the local callback value.
For the production frontend calling Render directly, also set
`SESSION_SAME_SITE=none` and `SESSION_HTTPS_ONLY=true`; the existing origin checks
and credentialed CORS use the configured frontend origin.

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

The signed, HttpOnly `repoagent_session` cookie contains an opaque session identifier and the internal user ID. The hashed session identifier resolves to the authoritative database session with a maximum lifetime of eight hours, capped sooner when GitHub returns a shorter token expiry. HTTP development defaults to `SameSite=Lax`; HTTPS defaults to `SameSite=None; Secure`. Set `SESSION_SAME_SITE=none` in Render if overriding this default. Public user identity, expiry, and CSRF state live on the server. GitHub access tokens are stored as Fernet ciphertext in `users.access_token` and are never returned by the API.

Auth logs use `uvicorn.error.repoagent.auth` and record OAuth start, callback
receipt, token exchange, user fetch, session creation, and the configured redirect
URL. They never record codes, tokens, secrets, cookie values, PKCE verifiers, or
raw exception details. Startup logs show cookie flags and allowed origins.
Keep `SESSION_SECRET` identical across instances and deployments.

After `Session created`, check the next `Session checked` reason:

- `authenticated`: the signed cookie and database session were accepted.
- `cookie_missing`: no cookie arrived; check the browser's cookie rejection reason.
- `cookie_invalid_or_expired`: a cookie arrived but could not be verified; check
  expiry and whether the signing secret changed between instances.
- `github_app_reauthorization_required`: the credential predates selected access, belongs to another configured App, or expired; start a fresh GitHub sign-in.
- `server_session_missing`, `server_session_expired`, or `user_missing`: the
  signed cookie was readable but its database session is no longer valid.

Database lookup failures return 503 instead of falsely reporting a logged-out
user. Callback failures log their stage and exception type. Browsers blocking
third-party cookies may still omit a `SameSite=None` cookie; use a same-origin
proxy or custom domains on the same site if the browser reports this restriction.

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

Register that exact API callback in the GitHub App. The frontend should send credentials with every API request (`credentials: "include"` for Fetch or `withCredentials: true` for Axios), along with the CSRF header on POST requests. Invalid cookie policies and `SameSite=None` without secure cookies prevent startup, rather than silently creating a broken login. The short-lived OAuth flow cookie stays `SameSite=Lax` so the browser can send it on GitHub's top-level callback navigation.

Browsers can still block third-party cookies even with `SameSite=None; Secure`. Prefer a same-origin proxy when that restriction affects your users; CORS and cookie attributes cannot override browser privacy settings. See the [cookie attribute documentation](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Set-Cookie#samesitesamesite-value).

| Endpoint | Contract |
| --- | --- |
| `GET /auth/github/login` | Redirects the browser to GitHub authorization. |
| `GET /auth/github/callback` | Exchanges the code, stores encrypted credentials, creates a database session, and redirects to `FRONTEND_URL`. |
| `GET /auth/session` | `{ authenticated, configured, user, csrf_token?, repository_access }`; user fields are `id` (GitHub ID), `login`, `name`, and `avatar_url`. |
| `GET /auth/me` | Compatibility response with internal `id`, `github_id`, `username`, `avatar_url`, and `csrf_token`; 401 when signed out. |
| `GET /auth/repositories?cursor=...` | `{ repositories, has_more, next_cursor, access }`; omit cursor initially. Entries include `id`, `full_name`, `clone_url`, `default_branch`, `private`, `description`, and `language`. |
| `POST /auth/logout` | Records sign-out, deletes the database session, and clears the cookie. |
| `GET /auth/activity?limit=20` | Newest account activity; `limit` accepts 1-50. Returns only the signed-in user's events. |

Repository pages come from `/user/installations/{id}/repositories`, never the broad `/user/repos` endpoint. One page contains up to 100 repositories from one installation; follow the opaque `next_cursor` until null, including empty pages filtered by permissions. `access` contains `configured`, `installation_url`, `manage_url`, and `installations` (`id`, `account`, `repository_selection`, `manage_url`). Session `repository_access` contains the same configuration links without installation lookups. The API revalidates cursor installation IDs against the signed-in user's App installations. Suspended installations, archived/disabled repositories, read-only App installations, and repositories without user push permission are excluded.

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

`GET /jobs` returns the signed-in user's newest 50 jobs in descending ID order. Each job includes `created_at`, `updated_at`, `pushed_at`, and `commit_message`. Timestamps are UTC Unix seconds; `pushed_at` and `commit_message` are null until a successful approval. Historical jobs have null timestamps where the original time is unknown.

The API responds with a queued job and runs cloning, analysis, and patch generation in a background thread. Poll `GET /jobs/{id}` for progress. `POST /jobs/{id}/approve` requires a completed job with changes and pushes to the current default branch. Protected branches may reject direct pushes. Each user can list, read, preview, and approve only their own jobs.

Approval accepts an optional JSON body with the commit message edited in the review card. Send the session cookie, trusted Origin, and the same CSRF header as job creation:

```http
POST /jobs/42/approve
Content-Type: application/json
X-CSRF-Token: <value from /auth/session>
Origin: http://127.0.0.1:5173
```

```json
{ "commit_message": "Fix login page title" }
```

The message is trimmed and must contain 1-200 characters on a single line, without control characters. Invalid messages return 422 before any commit or push. Omitting the body or the field keeps the default `RepoAgent: Apply requested changes`, so older clients remain compatible. A successful response contains `{ "message": "Changes pushed successfully", "job_id": 42, "commit_message": "Fix login page title" }`.

If an earlier push failed after creating the commit, retrying pushes that existing commit without rewriting it. The response always returns its actual message, even if the retry submits a different message. The API saves the actual commit message and `pushed_at` after a successful push and includes both in the approval response. Repeating an approval returns that saved result without another Git push. Simultaneous requests are serialized within the API process and, on PostgreSQL, with a database row lock across processes. Git and database writes cannot share a transaction: a crash after Git succeeds but before the database commit can still require a retry.

Deploy the backend first, then the frontend; startup automatically applies the additive migrations. Configure the GitHub App credentials and GITHUB_APP_SLUG described above.

## Saved account activity

Successful GitHub logins, sign-outs, job creation, generation completion/failure, and approved pushes are recorded in `activity_events` in the same database transaction as their saved application state. These summaries contain no tokens, request bodies, generated source, or exception details. Each event has `id`, `kind`, `message`, nullable `job_id`, and `created_at` (UTC Unix seconds). Kinds are `login`, `logout`, `job_created`, `job_completed`, `job_failed`, and `job_pushed`.

`GET /auth/activity?limit=20` returns a JSON array, ordered newest first. It requires a signed-in session and returns only that account's activity; signing out does not delete the account's history. Opening the app again restores saved jobs and activity from the database. The activity log starts with this deployment; it does not invent events for earlier jobs or logins.

Persist the configured database across deployments (managed PostgreSQL, or a persistent volume when using SQLite). Repository workspaces and preview artifacts still require their existing persistent storage to support later review and approval.

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

Startup runs an idempotent, additive migration for PostgreSQL or SQLite. It creates missing user/session/OAuth/activity tables, adds `jobs.user_id` and its index plus `created_at`, `updated_at`, `pushed_at`, and `commit_message`, adds nullable App client bindings on users/sessions and a token expiry on users, and preserves existing job IDs, diffs, statuses, and workspaces. New jobs receive timestamps; unknown historical dates and push state remain null. PostgreSQL schema changes are serialized across API instances.

Legacy columns, including an old per-job PAT column if present, remain physically in the database but are no longer mapped by the ORM or returned by the API. This migration does not erase historical credentials; any data cleanup requires a separate deliberate migration.

Historical jobs keep a null owner because ownership cannot be established safely. Authenticated endpoints do not expose those jobs to any user. New jobs belong to the account that created them. Back up an existing database before deployment.

## Verification

```sh
python -m unittest discover -s tests -v
```

The automated suite uses temporary databases/workspaces and mocked GitHub, Groq, and Docker calls where required. It covers OAuth/session/CSRF behavior, owned jobs, migrations, snapshot/build rules, and isolated preview delivery. No test signs into a real GitHub account or pushes a commit.

The Docker build path has command/contract coverage, but a live Docker Vite build was not verified in this workspace because a Docker daemon was unavailable. Verify that path on a host with Docker before relying on Vite previews in deployment.

