# Authentication and authorization (M5A)

Requirements: [AUTH-01 to AUTH-17](../spec/requirements/auth.md).

## Modes

`HELIOPUDDLE_AUTH_MODE` selects who issues and checks identities:

| Mode | Who authenticates | Roles come from |
|---|---|---|
| `local` (default) | hp: users and passwords in the DB, `POST /auth/token` (OAuth2 password flow) | the user's `roles` in the DB |
| `oidc` | an external provider (Keycloak): hp validates its bearer tokens | the token's `realm_access.roles` claim |
| `header` | a trusted proxy, which forwards `HELIOPUDDLE_USER_HEADER` | not enforced (the pre-M5A behaviour); a request without the header is `401` (AUTH-20) |

In `local` and `oidc` modes the user header is **ignored**, so a client cannot
claim another identity. `header` mode exists only for deployments behind a
proxy that authenticates users; it is opt-in. A request **without** the user
header is refused with `401` (AUTH-20): it did not come through the proxy, so
it must not be treated as an administrator. The public routes (`/health`,
`/version`, `/auth/config`, `/auth/token`) need no header. Nothing inside the
server relies on a header-less caller: flows and tasks call the database layer
directly, and the UI and CLI clients forward the header they were given (the UI
forwards the proxy's header from the browser request). Anyone who can reach the
port can send the header themselves, so in this mode the server must listen on
loopback or a private network only reachable by the proxy; `server start` logs a
WARNING when `header` mode binds a non-loopback host.

**Upgrade note.** The default changed from "trust the header" to `local`. A
deployment behind an authenticating proxy must set
`HELIOPUDDLE_AUTH_MODE=header`; otherwise its callers get `401`.

## Code layout

```
server/auth/        roles.py (role grammar and checks, pure)
                    passwords.py (argon2)   tokens.py (local JWTs)
                    secret.py (signing key) oidc.py (discovery, JWKS, introspection)
                    bootstrap.py (first admin, recovery)
server/models/users.py     user DB access functions
server/schemas/auth.py     token, me, user types (shared with the client)
server/api/auth/            /auth/* (one module per endpoint)
server/api/users/           /users* (one module per endpoint)
server/api/deps.py         authenticate and authorize dependencies
server/ui/auth/              login pages (routes.py), session and guard (session.py), OIDC helpers (oidc.py)
client/auth.py             token handling for HelioPuddleClient
cli_user.py                `heliopuddle user ...` (API only)
```

`heliopuddle.client`, `heliopuddle.server.ui` and `heliopuddle.cli_user` may not
import the DB layer (import-linter).

## Roles

| Role | Grants |
|---|---|
| `hp-reader` | `GET` routes except backend connection details, and `POST files/check` |
| `hp-writer` | register, uploads, metadata, content-date and rules-ignore `PATCH`, accept; plus read |
| `hp-deleter` | `DELETE /files/{id}`; plus read |
| `hp-operator` | `POST runs`, rule enable, disable and trigger; plus read |
| `hp-client-storage-read-write` | reusable backing-storage credentials and direct fsspec access; plus read, not API write or user management |
| `hp-admin` | everything; user management |

Grammar: global-only `hp-admin`, or
`(hp-reader|hp-writer|hp-deleter|hp-operator|hp-client-storage-read-write)` with an
optional `:<project>` suffix. A role without a suffix is **global**; with a
suffix it applies to that project only. In Keycloak a scoped role is simply a
realm role named `hp-writer:demo`. Bundles are Keycloak composite roles (their
members are expanded into `realm_access.roles` in the token). Role strings
outside this grammar in a token are ignored; in the user API they are `422`.

### Authorization

`get_principal` returns a `Principal(username, roles, mode, token)`. A route
declares `requires(role)`: the project is the `{project}` path parameter when
there is one, and for routes addressed by id it is resolved from the row by
`requires(role, project_of=file_project | series_project | upload_project)`.
`requires(None)` means "any valid principal" (`GET /projects`, filtered, and
`GET /auth/me`); it still runs introspection when `HELIOPUDDLE_OIDC_INTROSPECT`
is `always`, so no authenticated route skips it. When the row does not exist, a
caller who lacks the role globally gets the same hidden
`404 {"detail": "not found"}` as for a project they cannot see.

A project that is not in the config (a typo in the URL, or a project removed
from the YAML whose rows remain in the DB) is reachable only with a global role:
scoped roles grant nothing on it, so a removed project's files, series and
uploads answer `404` to a caller whose roles are all scoped, instead of staying
readable through the id-addressed routes.

```
allowed(roles, needed, project):
    "hp-admin" in roles                                  -> allow
    for r in roles:
        base, _, scope = r.partition(":")
        if scope and scope != project: continue          # scoped to another project
        if base == needed or (needed == "hp-reader" and base != "hp-admin"):
            return allow                                 # any role implies read
    return deny
```

Implication: every project role also has read, so a read route accepts any
project role, global or `:project`.

Failures: no or invalid credentials `401` (`WWW-Authenticate: Bearer`). On a
project route, if the principal holds no role at all on the project (global or
scoped), `404` (existence hidden); with some role but not the needed one,
`403 {detail, required_roles}`. `GET /projects` filters to projects with any
role. Project-agnostic routes (`/users`) answer `403`.

A route-table test walks the FastAPI router and fails if a route has neither a
`requires` dependency nor an entry in the public list, so a new endpoint cannot
ship unprotected.

### Route table

| Route | Required |
|---|---|
| `GET /health`, `GET /version`, `GET /auth/config`, `POST /auth/token` | public |
| `GET /auth/me`, `POST /auth/me/password` | any valid token (`/auth/me` is introspected in `always` mode; the password route is `local` only) |
| `GET /projects` | any role (filtered) |
| `GET /projects/{p}`, `.../storage-locations` (list), `.../rules*` (GET), `.../files` (GET, `by-path`), `POST .../files/check`, `GET /files/{id}`, `GET /series/{id}/versions` | reader on `p` |
| `GET /projects/{p}/storage-locations/{name}` (backend connection details) | `hp-client-storage-read-write` on `p` (or `hp-admin`) |
| `GET /files/{id}/content` (binary file content) | reader on the file's project |
| `PUT /uploads/{token}/content` (binary upload content) | writer on the upload's project |
| `POST .../files` (register), uploads (`POST .../uploads`, renew, complete, abort), `PATCH /files/{id}/metadata`, `PATCH /files/{id}/content-date`, `PATCH /files/{id}/rules-ignore`, `POST /files/{id}/accept` | writer on `p` |
| `DELETE /files/{id}` | deleter on `p` |
| `POST .../runs`, `POST .../rules/{name}/enable`, `disable`, `trigger` | operator on `p` |
| `/users*` | global `hp-admin` |

### Backend credentials and direct file access

The generic fsspec client uses the server's URL and resolved storage options
for direct file access. Those can contain reusable write/delete credentials;
handing them to a reader or scoped writer would bypass API permissions and
project boundaries. Connection details therefore require
`hp-client-storage-read-write` on the project (or `hp-admin`) in `local` and
`oidc` modes. The reader-level storage-location list keeps names, state and
index times but returns `url: ""` without that role on the project, displayed
as "Restricted" in the UI. The whole URL is withheld, not just known secret
keys: fsspec supports embedded credentials, query tokens and chained URLs.
This applies to inactive storage locations too.

The credential role can be global or project-scoped, for example
`hp-client-storage-read-write:demo`. Scope controls which project's credentials
the API issues; configure backend credentials restricted to that project's root,
since hp cannot constrain them once issued. The role implies reader access but
not writer, deleter, operator or admin API permissions. Give a direct-upload
client `hp-writer:demo` and `hp-client-storage-read-write:demo`.
The latter authorizes credential issuance, not just read/write operations:
the backend itself determines what those credentials can do.

Client `get_storage_location` requires the credential role. `add_file`, `open`
and `download` prefer direct fsspec access when connection details are allowed;
when that specific permission is denied they transfer bytes through the API
instead. A reader can stream `GET /files/{id}/content`; a writer can reserve,
`PUT /uploads/{token}/content`, and complete an upload. Both content routes
resolve project ownership and use existing role checks; neither returns backend
credentials. Cross-project, authentication and unrelated permission errors are
not hidden by a fallback. Trusted-proxy `header` mode preserves direct access.
Credential issuance is sensitive for OIDC introspection, like admin and delete.
This includes list responses containing URLs, not only the detail endpoint.
There are no existing deployments requiring credential migration.

Delete and upload-publish failures report the operation, exception type and
settled state without raw backend exception text, which may contain a
credential-bearing URL.

## Local mode

**Users.** Table `user` (see [data model](data-model.md)). Passwords are hashed
with argon2 (`pwdlib`); the minimum length is 12. For an unknown username the
password check still verifies against a dummy hash, so timing does not reveal
which usernames exist.

**Token.** `POST /auth/token` (form: `grant_type=password`, `username`,
`password`; other `grant_type` → `unsupported_grant_type`) returns
`{access_token, token_type: "bearer", expires_in}`. The access token is an HS256
JWT: `iss=heliopuddle`, `sub` (username), `realm_access: {roles}` (the same
shape Keycloak uses, so one role check serves both modes), `uid` (the user's
id), `cv` (the credentials version, below), `iat`, `exp`
(`HELIOPUDDLE_AUTH_TOKEN_TTL`, default `8h`) and `jti`. The `scope` form field
is ignored (hp uses roles). There are no refresh tokens: clients get a new
token with their password or re-login.

**Credentials version.** `credentials_version(user)` is `credentials_changed_at` in
whole microseconds since the epoch. It changes whenever the password is
changed, reset or set by an admin, and a recreated user starts a new row with a
new id and a new timestamp.

**Per-request check.** The token is verified (signature, `alg` pinned to
HS256, `iss`, `exp`, and integer `uid` and `cv`), then the user is re-read by
`uid`: it must exist, be active, and still have the credentials version `cv`,
and the principal's roles are the DB roles, not the token's. So role changes,
disabling and password changes take effect on the next request, without a
revocation list, and a token can never outlive the credentials it was issued
for (an earlier design compared `iat`, which has one-second resolution, and left
a window). The read is one indexed lookup by primary key.

**Login without a race.** `POST /auth/token` reads the user and verifies the
password outside any lock (argon2 is slow), remembering the row's id and
version. It then takes the row lock (`FOR UPDATE` on Postgres, the write lock on
SQLite), requires the same id, `is_active` and the same version, records the
login, and mints the token with that id and version. A password reset, a
disable or a delete-and-recreate that commits in between makes the login fail
with `invalid_grant`; one that commits after it invalidates the token at once.

**Own password change.** `POST /auth/me/password` locks the caller's user row
(`FOR UPDATE` on Postgres) before checking the current password and keeps it
through the update. A reset that commits first makes the supplied (old) password
wrong, and one that arrives meanwhile waits and is applied afterwards, so it is
never overwritten by a stale read.

**Signing key.** `HELIOPUDDLE_AUTH_SECRET_KEY` (at least 32 characters: an empty or
short value stops startup, because falling back to it would make tokens and the UI
session cookie forgeable), else `DATA_DIR/auth_secret`
(random 32 bytes, created `0600` on first start, with the create done
atomically so concurrent starts agree). A process that has neither (typically a
UI-only process on another host) refuses to start. The key is read once per process (`get_secret`). Set the env var when several
hp processes must accept the same tokens or cookies.

### First-time admin (AUTH-03)

At startup in `local` mode (after migrations, in one transaction that takes
the user table's write lock), if the `user` table is empty hp creates one
active user with `hp-admin`:

- username `HELIOPUDDLE_ADMIN_USERNAME` (default `admin`);
- password `HELIOPUDDLE_ADMIN_PASSWORD` if set (minimum 12 characters, else
  startup fails), otherwise 24 random URL-safe characters, logged **once** at
  WARNING with the username. It is not stored anywhere in clear.

The env vars matter only while there are no users, so restarting never resets a
password. Concurrent first starts (several replicas) create exactly one admin,
even with different `HELIOPUDDLE_ADMIN_USERNAME` values: the users are counted
under the startup advisory lock (`lock_schema`, Postgres; SQLite's write
transaction already serialises), so a second start sees the first one's admin.
A unique-username conflict on a loser is still treated as "already created".

**Recovery.** If the admin password is lost, start once with
`HELIOPUDDLE_ADMIN_RESET=true` and `HELIOPUDDLE_ADMIN_PASSWORD` set: hp sets
that user's password (creating the user as `hp-admin` if it does not exist),
re-enables it, restores `hp-admin`, sets `credentials_changed_at`, and logs a
WARNING. Remove the flag afterwards. No CLI command touches the DB.

### User management

`/users` CRUD (AUTH-13) is implemented by `server/api/users/` over
`server/models/users.py`. The `heliopuddle user` CLI is a thin Typer layer over
`HelioPuddleClient` and so over that API. Guards: duplicate username `409`;
the last active `hp-admin` cannot be deleted, disabled or demoted (`409`, checked
under `FOR UPDATE` row locks on Postgres and `BEGIN IMMEDIATE` on SQLite); invalid roles
and short passwords `422`. Scoped roles are validated against the
grammar, not against the configured projects (a stale scoped role is harmless).

## OIDC mode (Keycloak)

Follows the guide: the API is an OAuth resource server that validates bearer
JWTs locally with the provider's public keys, and only asks the provider
about a token when introspection applies.

**Settings.** `HELIOPUDDLE_OIDC_ISSUER` (for Keycloak
`https://host/realms/<realm>`), `HELIOPUDDLE_OIDC_DISCOVERY_URL` (optional, see
below), `HELIOPUDDLE_OIDC_CLIENT_ID`,
`HELIOPUDDLE_OIDC_CLIENT_SECRET`, `HELIOPUDDLE_OIDC_AUDIENCE` (default: the
client id), `HELIOPUDDLE_OIDC_USERNAME_CLAIM`, `HELIOPUDDLE_OIDC_ROLES_CLAIM`
(a dotted path, default `realm_access.roles`), `HELIOPUDDLE_OIDC_INTROSPECT`.

**Startup (fail fast).** Required settings are checked, then
`{issuer}/.well-known/openid-configuration` is fetched (short timeout); the
`jwks_uri` and `token_endpoint` (both required), `authorization_endpoint` and
`introspection_endpoint` (required unless introspection is `off`) are taken from it. An unreachable issuer, a missing
setting, or a discovery document whose `issuer` differs from the setting stops
startup with a message naming the setting.

**Provider on another address (AUTH-21).** Behind a reverse proxy the issuer
is usually the provider's public URL, which hp itself may not be able to reach
(no public DNS inside a private network, or a certificate it does not trust).
`HELIOPUDDLE_OIDC_DISCOVERY_URL` points hp at the discovery document on the
address it can reach, e.g. `http://keycloak:8080/realms/<realm>/.well-known/openid-configuration`.
hp uses the endpoints in the document as they are, so the provider must answer
with its server-to-server endpoints (JWKS, token, introspection) on the address
it was asked on and its browser endpoints (authorization, end session) on the
public one. Keycloak does this with `KC_HOSTNAME=<public URL>` and
`KC_HOSTNAME_BACKCHANNEL_DYNAMIC=true`. The document's `issuer` must still equal
`HELIOPUDDLE_OIDC_ISSUER`, which tokens carry as `iss`. `GET /auth/config`
announces the document's token endpoint, so clients on the same network get
tokens there; a client elsewhere passes a token it got itself (`token=`,
`--token`). hp does not rewrite any URL.

**Validation.** `PyJWT`, with a `PyJWKClient` (as in the guide) fetching the keys
from the provider's JWKS. The client caches the key set for an hour and, on an
unknown `kid`, refetches it at most once per cooldown (10 seconds, so a flood of
bad tokens cannot hammer the provider; a key rotation is picked up within that
time) and the refetch replaces the cache, so a retired key stops working. Only
`RSA` and `EC` keys with `use=sig` and a `kid` are used, and a token without a
`kid` is rejected. The JWKS is fetched once at startup to prove it works; a
refetch that fails during a request is a `502`. Algorithms are restricted to
asymmetric ones (`RS*`, `ES*`, `PS*`); `HS*` and `none` are refused even if the
header or the JWKS claims them. `iss` must equal the issuer, `aud` must include
the audience (a string or a list), `exp` is required. Any `jwt` error is a `401`
with a generic message. Unit tests mock only the network (the fake issuer in
`tests/fake_oidc.py` answers `urllib`'s JWKS request), so `PyJWKClient`'s own
caching, cooldown and error handling run for real.
The keycloak default audience is `account`; to use the client id, add an
"Audience" mapper to the client (the example realm does), or set
`HELIOPUDDLE_OIDC_AUDIENCE=account`.

**Identity and roles.** Username is the username claim, else `sub`. Roles are
read from the roles claim by dotted path; non-hp roles are ignored. A token
with no hp roles authenticates but is denied (`403`, or `404` on project
routes).

**Introspection.** `off`, `sensitive` (default) or `always`. `sensitive`
applies to routes requiring `hp-deleter`, `hp-client-storage-read-write` or
`hp-admin` (a revoked token must
not be able to delete); `always` applies to every route. hp posts
`token`, `token_type_hint=access_token` with the client id and secret to the
introspection endpoint; `active: false` is a `401`; a network error, an unreadable body or any non-200 answer
is a `502`. Only a literal `active: true` passes. Results are not cached.

## Client

`HelioPuddleClient(api_url, project, *, token=None, username=None,
password=None, client_id=None, client_secret=None, user=None, ...)`. At most
one credential kind. `username`/`password` and `client_id`/`client_secret`
fetch a token lazily on the first request: the client calls `GET /auth/config`
to learn the mode; `local` posts the password form to the API's own
`/auth/token` on the origin the client was configured with, ignoring the
`token_url` the server announces (a TLS-terminating proxy that uvicorn does not
trust for forwarded headers makes the server announce `http://`, and a password
must not follow that); `oidc` posts to the announced provider `token_url` a `password` (direct access grant) or `client_credentials` grant
to the provider with the client credentials the caller gave. The token and its
expiry are cached; on a `401` the client fetches a new token once and retries.
A password or secret is never sent over `http://` by a client whose API URL is
`https://` (it refuses an `oidc` token endpoint that is plain http).
`user=` is sent only in `header` mode. Errors: `AuthenticationError` (401),
`PermissionDeniedError` (403, with `required_roles`). `me()`, and the user
methods that mirror `/users`, are plain API calls.

Role helpers (`Me.can(role, project)`) use the same `allowed()` rule as the
server, so the UI can hide controls consistently.

## CLI

`heliopuddle user add|list|show|set-password|set-roles|disable|enable|delete`
(module `heliopuddle.cli_user`) builds a `HelioPuddleClient` and calls the API;
there is no other path. `--api-url` (env `HELIOPUDDLE_API_URL`) defaults to
`http://127.0.0.1:8080/api/v1`. Credentials: `--token`/`HELIOPUDDLE_TOKEN`, or
`--username`/`HELIOPUDDLE_USERNAME` with the password from a hidden prompt,
`--password-stdin` or `HELIOPUDDLE_PASSWORD`. A new user's password uses a hidden
prompt with confirmation, `--new-password-stdin` or `HELIOPUDDLE_NEW_PASSWORD`;
`delete` asks for confirmation unless `--yes`. A password is never accepted on
argv. Only `local` mode has user commands; in other modes the API answers `404`
and the CLI says users are managed elsewhere.

## UI

The UI never validates tokens; it holds one for the user and passes it to the
API through `heliopuddle.client`. The session is a Starlette
`SessionMiddleware` cookie (`hp_session`), signed with the auth secret
(`itsdangerous`), `HttpOnly`, `SameSite=Lax`, `Secure` when the request is
https, scoped to the UI root path. It holds the access token, the username, the
expiry and the roles from `/auth/me`. Because the session is signed, not
encrypted, it is treated as readable by the browser's owner (it is their token).
`SameSite=Lax` is the CSRF defence for the UI's `POST` forms.

- **`local`:** `GET /login` shows a form; `POST /login` calls
  `client.login(username, password)`; on success the token is stored and the
  user is redirected to `next` (a local path only: it must start with a single
  `/` and not `//`; anything else becomes `/`). `POST /logout` clears the
  session.
- **`oidc`:** `GET /login` generates `state` and a PKCE verifier (stored in the
  session), and redirects to the provider's `authorization_endpoint` with
  `response_type=code`, `client_id`, `redirect_uri`, `scope=openid`,
  `state`, `code_challenge`. `GET /auth/callback` checks `state`, exchanges the
  code at the `token_endpoint` with the client secret and the verifier, stores
  the access token, and redirects to `next`. The redirect URI is the UI's
  `/auth/callback` URL built from the request (including the root path), so a
  reverse proxy must forward `Host` and `X-Forwarded-Proto` and the provider
  client must allow that redirect URI. Keycloak access tokens last 5 minutes by
  default; on expiry the UI redirects through the provider, which is silent
  while the provider's own session lives.
- **`header`:** no login; as before.

Every UI page except `/login`, `/auth/callback` and `/static` requires a
session; a missing or expired one redirects to `/login?next=...`, and a `401`
from the API clears the session the same way. A `403`/`404` from the API shows
the existing error page. The project picker lists whatever `GET /projects`
returns (already filtered). A user who is signed in but holds no hp role at all (e.g.
through a portal's single sign-on) sees "No access yet" and the role to ask for,
not the "no projects configured" message. Templates ask the session's roles (`can('hp-operator',
project)`) before showing "Index now", "Reconcile now", "Accept", delete and the
rule controls; the API remains the enforcer.

A UI-only process (`--no-api --no-services`) signs its own cookie: it uses
`HELIOPUDDLE_AUTH_SECRET_KEY` or the key file in its data dir, and generates a key
there if the directory is writable (it fails fast, exit code 2, when it cannot).
It does not need the API's key, because it never validates tokens, but several UI
processes behind one load balancer must share `HELIOPUDDLE_AUTH_SECRET_KEY` so any
of them accepts the cookie. In `oidc` mode it needs the OIDC settings for the code
flow and discovers the provider itself.

Details: the cookie has no `Max-Age`, the token's expiry governs; roles are read
once at login, so a role change shows in the UI only after logging in again (the
API enforces every request regardless); `redirect_uri` honours
`X-Forwarded-Proto` and `X-Forwarded-Host`, and the cookie is `Secure` when the
request is https or that header says so; in `oidc` mode, logout clears the session and, if the discovery document has an
`end_session_endpoint`, redirects the browser there with `client_id` and
`post_logout_redirect_uri` (the UI's `/login?logged_out=1` URL), so the provider's
SSO session ends too and the next page asks for credentials again (no
`id_token_hint` is sent, because the id token is not stored, so Keycloak asks
the user to confirm; the provider must allow that post-logout redirect URI).
Keycloak access tokens carry every realm role, so a user with very many roles can
push the signed cookie towards the browsers' 4 KB limit (the example realm's
admin session measured 1.8 KB); keep the hp role set small or trim the token's
roles with a client scope in Keycloak. The UI has no delete control, so none is
hidden.

## Security notes

- Passwords, tokens, the auth secret and the OIDC client secret are never
  logged, never returned by `/auth/config`, `/users` or `/auth/me`, and are
  redacted from startup logs. The generated admin password is the one
  deliberate exception, logged once.
- Use https in front of hp for anything but local use: the password flow and
  bearer tokens are only as private as the transport.
- **Login throttling (AUTH-18).** `POST /auth/token` and the UI's `POST /login`
  share one in-process counter (`server/auth/throttle.py`). After
  `HELIOPUDDLE_LOGIN_MAX_FAILURES` (default 10) failures for a username (case
  insensitive), or `HELIOPUDDLE_LOGIN_MAX_FAILURES_PER_IP` (default 100) from a
  client address, within `HELIOPUDDLE_LOGIN_WINDOW` (default `15m`), further
  attempts get `429` with `Retry-After` (the UI shows a friendly message) until
  the window since the first failure ends, even with the right password. A
  successful login clears the username's count. The address limit is higher
  because the UI calls the API from one address; the UI also counts the real
  browser address itself. The counters are **per process**: with several server
  processes each keeps its own, and a restart clears them. A rate-limiting
  proxy remains a good idea for an exposed server. Tests drive an injectable
  clock.
- **Security headers (AUTH-19).** Every API and UI response carries
  `Content-Security-Policy: frame-ancestors 'none'`, `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff` and `Referrer-Policy: same-origin`
  (`server/security_headers.py`). A header a route set itself is kept; a CSP
  without `frame-ancestors` gets it appended (the image route's sandbox CSP).
  The UI never frames itself. A portal that shows the UI in a frame sets
  `HELIOPUDDLE_FRAME_ANCESTORS` (e.g. `'self'` when it is on the same origin):
  the CSP then names those sources, `X-Frame-Options` is `SAMEORIGIN` for
  `'self'` and left out for any other value (it cannot name other origins, and
  browsers use the CSP when it has `frame-ancestors`). The value must be plain
  source expressions: `;`, commas, control characters and unquoted `self` or
  `none` stop startup, so the setting cannot add other directives. Only
  allow origins you trust: framing enables clickjacking by those pages.
- Local tokens cannot be revoked individually; effective roles, `is_active`
  and `credentials_changed_at` are re-read from the DB per request, and the
  lifetime is bounded.
- Prefect's own server and UI are not authenticated by hp (out of scope). The
  embedded Prefect API on port 4200 must never be exposed publicly: publish it on
  loopback only (see deployment.md).
- `GET /health` is unauthenticated and returns only `ok` or `error` per part;
  details are logged on the server.

### Known limitations

Not fixed, by decision:

- Logout does not revoke the bearer token held in the session cookie; the token
  stays valid until it expires (`HELIOPUDDLE_AUTH_TOKEN_TTL`).
- The generated first-admin password is logged once at WARNING. Set
  `HELIOPUDDLE_ADMIN_PASSWORD` to avoid it.
- Preview parsers (CDF, tables, markdown) parse file content in the server
  process, within `preview_max_bytes`; a hostile file can cost CPU and memory.
- GitHub Actions in `.github/workflows` are pinned by tag, not by commit SHA.

## Keycloak setup

`examples/keycloak/` contains a compose file and `realm-heliopuddle.json`
(imported at startup). The realm defines: the six global realm roles, example
scoped roles (`hp-reader:demo`, `hp-writer:demo`), a composite role
`hp-contributor` (reader + writer), a confidential client `heliopuddle` (standard
flow and direct access grants, with its secret in the import, an Audience
mapper, redirect URIs for `http://localhost:8080/*`, and a service account with
realm roles), and users `admin-demo`, `reader-demo`, `writer-demo` and `demo-only` (only `hp-writer:demo`). `examples/keycloak/projects/` has the two projects (`demo`, `other`) the manual test needs. hp is
started with
`HELIOPUDDLE_AUTH_MODE=oidc HELIOPUDDLE_OIDC_ISSUER=http://localhost:8180/realms/heliopuddle
HELIOPUDDLE_OIDC_CLIENT_ID=heliopuddle HELIOPUDDLE_OIDC_CLIENT_SECRET=...`.
The guide's role setup (client roles `api-read`, `api-admin`) maps to these
realm roles; hp reads realm roles, not client roles.
