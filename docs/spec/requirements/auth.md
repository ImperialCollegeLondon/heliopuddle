# Auth requirements (`AUTH`)

Milestone M5A. Design: [auth](../../design/auth.md). Approved plan:
[reviews/m5a-plan.md](../reviews/m5a-plan.md).

Authentication has three modes. In `local` mode hp keeps its own users and
issues tokens (OAuth2 password flow). In `oidc` mode an external provider such
as Keycloak issues the tokens and hp validates them. In `header` mode a trusted
proxy authenticates users and forwards the user id header (the behaviour
before M5A). Authorization is by **realm roles**: global roles, plus optional
roles scoped to one project.

Roles:

| Role | Grants |
|---|---|
| `hp-reader` | read access (except backend connection details), and `POST files/check` |
| `hp-writer` | register, uploads, metadata and rules-ignore changes, accept; plus read |
| `hp-deleter` | `DELETE /files/{id}`; plus read |
| `hp-operator` | `POST runs`, rule enable, disable and trigger; plus read |
| `hp-client-storage-read-write` | backing-storage credentials and direct read/write access; plus API read |
| `hp-admin` | everything, and user management |

`hp-reader`, `hp-writer`, `hp-deleter`, `hp-operator` and
`hp-client-storage-read-write` may be scoped to a project as `<role>:<project>`
(for example `hp-writer:demo`). `hp-admin` is global only. The credential role
does not imply API writer, deleter, operator or user-management permissions;
`hp-admin` implies every role.

**AUTH-01 (M5A)** `HELIOPUDDLE_AUTH_MODE` is `local` (default), `oidc` or
`header`. An invalid value or missing required settings stops startup with a
clear message. In `header` mode, behaviour is as in M1–M4: the user comes from
`HELIOPUDDLE_USER_HEADER` and no roles are enforced. In `local` and `oidc`
modes the header is ignored.
*Verify:* unit tests: each mode, a bad value, a spoofed header.

**AUTH-02 (M5A)** The `user` table holds an id, a case-insensitive unique
`username`, an `argon2` `password_hash`, `roles` (a list of role strings),
`is_active`, `credentials_changed_at`, and created, updated and last-login
timestamps. A username is 1 to 255 ASCII letters, digits and `. _ @ + -`,
starting with a letter or digit: ASCII keeps case-insensitive matching the
same in SQL and Python (SQLite's `lower()` is ASCII-only), and every such name
is one URL path segment, so it is always addressable as `/users/{username}`. The migration runs on SQLite and Postgres. Users are data in the
DB, not config.
*Verify:* migration test on both databases.

**AUTH-03 (M5A)** **First-time admin.** In `local` mode with an empty `user`
table, startup creates one user with the `hp-admin` role. The username is
`HELIOPUDDLE_ADMIN_USERNAME` (default `admin`; the same alphabet as every
username, else startup fails). The password is
`HELIOPUDDLE_ADMIN_PASSWORD`, or a random 24-character password logged once at
WARNING. There is never a fixed default. The env vars apply only while no users
exist, and concurrent starts create exactly one admin, even when each is
configured with a different admin username (the users are counted under the
startup advisory lock, so no unique-name conflict is needed to stop a second
admin). A password shorter than 12 characters stops startup. **Recovery:** with `HELIOPUDDLE_ADMIN_RESET=true`
and the password env var set, startup resets that user's password, re-enables
it and restores `hp-admin`, with a WARNING log, so a lost password never needs
a DB-touching CLI command.
*Verify:* unit tests: env override, generated password, restart, race, short
password, reset.

**AUTH-04 (M5A)** **Password flow.** `POST /auth/token` takes a form with
`grant_type=password`, `username` and `password`. It returns `{access_token,
token_type: "bearer", expires_in}`. Errors use the OAuth format:
`invalid_grant`, `unsupported_grant_type`. Unknown and wrong-password cases are
indistinguishable and take the same time. The endpoint is `404` outside
`local` mode. Passwords and tokens are never logged.
*Verify:* unit tests, including a timing-parity check.

**AUTH-05 (M5A)** **Local tokens.** HS256 JWTs with `iss`, `sub`,
`realm_access: {roles}`, `uid`, `cv`, `iat`, `exp` and `jti`. The lifetime is
`HELIOPUDDLE_AUTH_TOKEN_TTL` (default `8h`). A token is bound to the credentials
it was issued for: `uid` is the user's id and `cv` the user's credentials
version (`credentials_changed_at` in whole microseconds). Each request re-reads
the user by `uid`: effective roles come from the DB, a disabled or deleted user
is rejected, and so is a token whose `cv` is not the user's current version, so
a password change or reset, or a deleted and recreated user, invalidates every
earlier token at once, however close in time (no clock rounding). A login issues
its token only if the row whose password it checked is still current when the
token is minted (same id and version, under a row lock), so a reset or recreate
racing a login yields no token. Expired, tampered, wrong-issuer and `alg=none`
tokens, and tokens missing `uid` or `cv`, return `401`.
*Verify:* unit tests per rejection case.

**AUTH-06 (M5A)** **Signing secret.** `HELIOPUDDLE_AUTH_SECRET_KEY` if set; it
must be at least 32 characters, and an empty or shorter value stops startup
naming the variable (it never falls back to generating a key, and is never used
as it is: an empty deployment variable must not select a publicly known key,
for tokens or for the UI session cookie).
Otherwise a random key is generated into `DATA_DIR/auth_secret` (mode 0600) on
first start and reused. A process with no env var and no readable file fails
fast, for example a separate UI process with no shared data dir.
*Verify:* unit tests: generated, reused, permissions, fail-fast.

**AUTH-07 (M5A)** **Role enforcement.** Every API route declares the role it
needs (any one suffices, and global `hp-admin` always does) or is explicitly
public. A route-table test fails on an undeclared route. No or invalid
credentials return `401` with `WWW-Authenticate: Bearer`. A valid token with
insufficient roles returns `403` with `{detail, required_roles}`. Public
routes: `/health`, `/version`, `/auth/config`, `/auth/token`. Any valid token
can call `/auth/me`. Reader routes are all `GET`s except backend connection
details, plus `POST files/check`.
Writer routes are register, uploads, the metadata and rules-ignore `PATCH`es,
and accept. The deleter route is `DELETE /files/{id}`. Operator routes are
`POST runs` and rule enable, disable and trigger. The `/users` routes need
`hp-admin`. `GET /projects/{p}/storage-locations/{name}` needs
`hp-client-storage-read-write` on that project (or `hp-admin`): it returns
reusable backend credentials that could bypass every
API role. The storage-location list withholds the entire URL (`url: ""`) from
callers without that role, including inactive storage locations, because
arbitrary fsspec URLs may contain credentials or signed query strings.
Delete and upload-publish errors must not return raw backend exception text
that may contain those credentials; they retain the operation, exception type
and settlement outcome instead.
`GET /files/{id}/content` streams binary content with reader access on its
project; `PUT /uploads/{token}/content` accepts binary content with writer
access on its project, under the reservation's ownership guard. Neither route
issues backend credentials.
*Verify:* route-table test plus a role × endpoint matrix.

**AUTH-08 (M5A)** **Project-scoped roles.** A role is `<role>:<project>` for
reader, writer, deleter, operator and client-storage-read-write. On a route
about project `p`, the global role or `<role>:p` suffices. Routes addressed by id (`/files/{id}`,
`/series/{id}/versions`, `/uploads/{token}/...`) resolve the project from the
row first. `GET /projects` returns only projects where the caller holds any
role. A caller with no role at all on a project gets `404` (existence hidden).
A caller with some role but not the needed one gets `403`. Unknown role
strings are rejected on write (`422`). A scoped role for a project that is no
longer configured is accepted but grants nothing: a project removed from the
config keeps its rows in the DB, and routes addressed by id (`/files/{id}`,
`/series/{id}/versions`, `/uploads/{token}/...`) then answer `404` to a caller
whose only roles are scoped, while global roles still reach them.
*Verify:* unit tests: global, scoped, multi-project, the id-scoped routes, the
404/403 split, list filtering.

The storage-credential role scopes credential issuance to the named project;
backend permissions must independently restrict issued credentials to that
project's root. The API cannot revoke or narrow a copied backend credential.

**AUTH-09 (M5A)** **OIDC resource server.** Settings:
`HELIOPUDDLE_OIDC_ISSUER`, `HELIOPUDDLE_OIDC_CLIENT_ID`,
`HELIOPUDDLE_OIDC_CLIENT_SECRET`, `HELIOPUDDLE_OIDC_AUDIENCE` (default: the
client id), `HELIOPUDDLE_OIDC_USERNAME_CLAIM` (default `preferred_username`,
falling back to `sub`) and `HELIOPUDDLE_OIDC_ROLES_CLAIM` (default
`realm_access.roles`). Startup reads the issuer's discovery document and fails
fast if it is unreachable or settings are missing. Bearer JWTs are validated
locally against the JWKS (cached, refreshed on an unknown `kid`). Only
asymmetric algorithms are accepted, and `iss`, `aud` and `exp` are checked. A
token with no hp roles authenticates but gets `403` or `404` on protected
routes.
*Verify:* unit tests against a fake issuer with a generated RSA key and JWKS,
covering every rejection case.

**AUTH-10 (M5A)** **Introspection.** `HELIOPUDDLE_OIDC_INTROSPECT` is `off`,
`sensitive` (default) or `always`. `sensitive` covers routes requiring
`hp-deleter`, `hp-admin` or `hp-client-storage-read-write` (including
storage-location lists that return URLs); `always` covers every authenticated route,
including `/auth/me`. The token is posted to the introspection endpoint
with the client id and secret. An inactive token returns `401` and an
unreachable issuer returns `502`. A value other than `off` requires a client
secret, or startup fails.
*Verify:* unit tests with the issuer mocked; the manual Keycloak test
(AUTH-17).

**AUTH-11 (M5A)** **Discovery and self-service.** Public `GET /auth/config`
returns `{mode, token_url, authorization_url}`. `GET /auth/me` returns
`{username, roles, mode}`. In `local` mode, `POST /auth/me/password
{current_password, new_password}` lets any user change their own password,
which sets `credentials_changed_at`. The user row is locked and re-read before
the current password is checked and stays locked through the update, so an admin
reset that commits meanwhile is never overwritten by the caller's change.
Nothing exposes secrets.
*Verify:* unit tests per mode, and a race against an admin reset on SQLite and
Postgres.

**AUTH-12 (M5A)** **Identity in the data.** `created_by` and `updated_by` hold
the authenticated username in `local` and `oidc` modes, and the header value in
`header` mode. System writers such as `heliopuddle:index` are unchanged.
*Verify:* unit tests per write endpoint.

**AUTH-13 (M5A)** **User CRUD API** (`local` mode only, otherwise `404`; global
`hp-admin` only): `GET /users`, `POST /users {username, password, roles?,
is_active?}` → `201`, `GET /users/{username}`, `PATCH /users/{username}`
(password, roles, is_active), `DELETE /users/{username}` → `204`. The password
hash is never returned. A duplicate username returns `409`. A password under 12
characters, an invalid role, or a username outside the AUTH-02 alphabet returns
`422`. The last active `hp-admin` cannot
be deleted, disabled or demoted (`409`), checked atomically under a row lock.
Created and updated times are set. Checks run in the order 401, 403, then the
mode check (`404` outside `local`). `422` bodies on `/users` and `/auth` do not
echo the rejected input, so a rejected password is never reflected.
*Verify:* unit tests per endpoint and each guard.

**AUTH-14 (M5A)** **User CLI.** `heliopuddle user add|list|show|set-password|
set-roles|disable|enable|delete` calls only the API through
`heliopuddle.client`, with no DB access. Options: `--api-url` (env
`HELIOPUDDLE_API_URL`, default `http://127.0.0.1:8080/api/v1`); credentials
from `--username` (env `HELIOPUDDLE_USERNAME`) with a hidden password prompt,
`--password-stdin` or `HELIOPUDDLE_PASSWORD`, or `--token` (env
`HELIOPUDDLE_TOKEN`). `--username` is the account that runs the command (for
example `admin`), never the user being managed, which is the argument after the
command name. A new user's password comes from a hidden prompt with
confirmation, `--new-password-stdin` or `HELIOPUDDLE_NEW_PASSWORD`. `delete`
asks for confirmation unless `--yes`. Passwords never appear on argv. An import-linter
contract forbids the DB layer in the user CLI module.
*Verify:* unit tests with the Typer runner against a test API; `lint-imports`.

**AUTH-15 (M5A)** **Client auth.** `HelioPuddleClient(..., token=)`, `(...,
username=, password=)` or `(..., client_id=, client_secret=)`.
Username/password fetches a token from `/auth/token` in `local` mode (always on
the origin the client was configured with: the `token_url` the server announces
is ignored, because behind a TLS-terminating proxy that uvicorn does not trust
the server can announce `http://`) or from the provider in `oidc` mode. A
password or secret is never sent over plain `http://` when the client's API URL
is `https://` (an `oidc` token endpoint that is plain http is refused). Client credentials use the provider's
`client_credentials` grant, so the service account needs realm roles. Tokens
are cached and refreshed once on expiry or `401`. A `401` raises
`AuthenticationError` and a `403` raises `PermissionDeniedError` carrying the
required roles. User-management methods mirror AUTH-13, and `me()` returns the
roles. `user=` still works in `header` mode. In `local` and `oidc` modes,
`get_storage_location` requires `hp-client-storage-read-write` (or `hp-admin`).
`add_file`, `open` and `download` use direct fsspec access with that role;
otherwise they transfer bytes through the API under writer/reader access.
Fallback applies only to a credential-role denial, not authentication,
cross-project or unrelated failures. Transfers are chunked with bounded memory,
uploads retain the reservation/renew/complete/abort protocol, and downloads
retain hash verification and atomic replacement.
*Verify:* unit tests per credential type, refresh and errors.

**AUTH-16 (M5A)** **UI login.** `local` mode serves a login form and logout.
`oidc` mode uses an authorization-code flow with `state` and PKCE through the
confidential client. The session is a signed cookie with HttpOnly,
`SameSite=Lax`, and `Secure` on https. Unauthenticated pages redirect to
`/login?next=`, and `next` accepts only local paths. Expiry or a `401`
re-logins. The project picker shows only accessible projects (AUTH-08). "Index
now", "Reconcile now", "Accept", delete and the other write controls are hidden
unless the caller's roles allow them on that project, and the server still enforces it.
The nav shows the user and a logout (the login page shows neither the
Projects nor the Runs link); in `oidc` mode logout also ends the
provider's session (redirect to its `end_session_endpoint`, when it has one), so
the next page asks for credentials again. It works in all three UI serving modes
(UI-03).
*Verify:* unit tests with a stub client; one Playwright login e2e in `local`
mode.

**AUTH-17 (M5A)** **Deployment and docs.** The settings go into the
architecture and deployment docs with defaults and an upgrade note (set
`HELIOPUDDLE_AUTH_MODE=header` behind a trusted proxy). `examples/keycloak/`
has a compose file and an importable realm with the six global roles, example
project-scoped roles, a composite role, a confidential client, a service
account and a test user. A short guide covers setting up Keycloak and the role
names.
*Verify:* one documented manual test against the example Keycloak: UI login, a
bearer call, a project-scoped role, introspection on delete. No automated e2e.

**AUTH-18 (M6A)** **Login throttling.** `POST /auth/token` and the UI's
`POST /login` share an in-process limiter. More than
`HELIOPUDDLE_LOGIN_MAX_FAILURES` (default 10) consecutive failures for one
username (case-insensitive), or `HELIOPUDDLE_LOGIN_MAX_FAILURES_PER_IP`
(default 100) from one client address, within `HELIOPUDDLE_LOGIN_WINDOW`
(default `15m`) get `429` with `Retry-After` (API) or a friendly message (UI)
until the window ends, even with the right password. A success resets the
username's count. The counters are per process.
*Verify:* unit tests with an injectable clock: per-username, per-address,
reset on success, expiry, and the UI message.

**AUTH-19 (M6A)** **Security headers.** Every API and UI response carries
`Content-Security-Policy: frame-ancestors 'none'`, `X-Frame-Options: DENY`,
`X-Content-Type-Options: nosniff` and `Referrer-Policy: same-origin`. A header a
route set is kept; a route's CSP without `frame-ancestors` gets it appended.
*Verify:* unit tests on UI pages, errors, API responses and the image route.

**AUTH-20 (M6A)** **Header mode needs the header.** In `header` mode a request
without the user header (or with an empty one) gets `401`, except on the public
routes. `server start` logs a WARNING when `header` mode binds a non-loopback
host.
*Verify:* unit tests of the `401`s and of the warning.
