# Example Keycloak for heliopuddle (`oidc` mode)

A throwaway Keycloak with a ready-made realm, for trying heliopuddle's `oidc`
authentication mode and for the one manual test of requirement AUTH-17. Design:
[docs/design/auth.md](../../docs/design/auth.md). **Demo only**: dev mode, plain
http, fixed passwords and a published client secret. Never use these values in
production.

| File | What it is |
|---|---|
| `docker-compose.yml` | Keycloak 26 on host port `8180` (and an optional hp container) |
| `realm-heliopuddle.json` | the realm imported at startup |
| `projects/demo.yaml`, `projects/other.yaml` | two hp projects, for the project-scoped role test |

## What the realm contains

- **Realm `heliopuddle`.** Admin console: <http://localhost:8180/admin/>
  (user `admin`, password `admin`).
- **Realm roles.** The six global roles, three example project-scoped roles, and
  one bundle:

  | Role | Grants |
  |---|---|
  | `hp-reader` | `GET` routes except backend credentials, and `POST files/check` |
  | `hp-writer` | register, uploads, metadata and rules-ignore `PATCH`, accept; plus read |
  | `hp-deleter` | `DELETE /files/{id}`; plus read |
  | `hp-operator` | `POST runs`, rule enable, disable and trigger; plus read |
  | `hp-client-storage-read-write` | reusable backing-storage URL/options and API read; no API write, delete, operator or user management |
  | `hp-admin` | everything; user management (global only) |
  | `hp-reader:demo`, `hp-writer:demo` | the same, for project `demo` only (`<role>:<project>`) |
  | `hp-client-storage-read-write:demo` | backend credentials and API read for project `demo` only |
  | `hp-contributor` | a Keycloak composite role: `hp-reader` + `hp-writer` (the token lists both) |

  Roles are read from the access token's `realm_access.roles`. A role without a
  `:<project>` suffix is global. Create more scoped roles by naming a realm role
  `hp-writer:<project>`; `hp-admin` cannot be scoped.

  The credential role controls which project's reusable backing-storage
  URL/options the API issues. The backend credentials must independently be
  restricted to that project's root: hp cannot narrow or revoke credentials
  once copied, and their actual capabilities are set by the backend. The role
  implies API reader only, not writer, deleter, operator or admin. Give a direct
  upload client both `hp-writer:demo` and `hp-client-storage-read-write:demo`;
  ordinary readers and writers transfer file content through the API instead,
  without receiving backend credentials. The storage-location list withholds
  its entire URL (`url: ""`) without the credential role on that project.
  In sensitive introspection mode, issuing either the detail's URL/options or
  the list's credential-bearing URL checks that the token is still active;
  redacted lists do not need that sensitive check.
- **Client `heliopuddle`** (confidential, secret `demo-only-client-secret-change-me`):
  standard flow (PKCE allowed; hp always sends it), direct access grants (the
  password grant, used by the manual test and by `HelioPuddleClient(username=...)`),
  and a service account holding `hp-reader`. Redirect URIs
  `http://localhost:8080/*` and `http://127.0.0.1:8080/*`. An *Audience* mapper
  adds `heliopuddle` to the access token's `aud` (Keycloak's default `aud` is
  `account`), and the default `roles` client scope puts `realm_access.roles`
  in the token.
- **Users** (all with password `demo-password`):

  | User | Roles | Meant for |
  |---|---|---|
  | `admin-demo` | `hp-admin` | everything, including delete |
  | `reader-demo` | `hp-reader` | read only: writes are `403` |
  | `writer-demo` | `hp-contributor` (`hp-reader` + `hp-writer`) | register files; delete is `403` |
  | `demo-only` | `hp-writer:demo` | project `demo` only; project `other` is invisible (`404`) |
  | `service-account-heliopuddle` | `hp-reader` | the `client_credentials` grant |

## Start Keycloak

```bash
cd examples/keycloak
docker compose up -d
docker compose logs -f keycloak     # wait for "Listening on: http://0.0.0.0:8080"
```

Keycloak is then at <http://localhost:8180>. Check the discovery document and
note the `issuer`:

```bash
curl -s http://localhost:8180/realms/heliopuddle/.well-known/openid-configuration | jq .issuer
# "http://localhost:8180/realms/heliopuddle"
```

### Host names and the token issuer

A token's `iss` must equal `HELIOPUDDLE_OIDC_ISSUER` exactly, and the browser,
`curl` and hp must all reach Keycloak at the address in it. The compose file
pins `KC_HOSTNAME=http://localhost:8180`, so Keycloak always issues tokens with
`iss=http://localhost:8180/realms/heliopuddle`, whatever name it was reached
by. The simplest setup is therefore **hp on the host**, with that issuer.

A container cannot reach the host's `localhost:8180` (inside it, `localhost` is
the container). Do not set the issuer to `http://keycloak:8080/...`: hp would
then fetch discovery successfully, but the browser would be redirected to
`keycloak:8080`, which does not resolve on the host, and the `iss` would not
match. In a real deployment, give Keycloak one public https URL (`KC_HOSTNAME`)
that resolves from browsers and from hp, and use it as the issuer.

The optional `hp` compose profile runs hp in a container with the host network,
so `localhost:8180` is right inside it too (Linux only):

```bash
export HP_DEMO_DATA=/tmp/hp-keycloak-demo
mkdir -p "$HP_DEMO_DATA" && chmod 777 "$HP_DEMO_DATA"   # the image runs as uid 10001
docker compose --profile hp up -d --build
```

## Start hp in `oidc` mode

These are the settings from
[docs/design/deployment.md](../../docs/design/deployment.md#authentication-m5a).
From the repository root, with hp on the host:

```bash
export HELIOPUDDLE_DEMO_DATA=/tmp/hp-keycloak-demo     # the stores of both projects
mkdir -p "$HELIOPUDDLE_DEMO_DATA/demo" "$HELIOPUDDLE_DEMO_DATA/other"

HELIOPUDDLE_CONFIG_DIR=examples/keycloak/projects \
HELIOPUDDLE_AUTH_MODE=oidc \
HELIOPUDDLE_OIDC_ISSUER=http://localhost:8180/realms/heliopuddle \
HELIOPUDDLE_OIDC_CLIENT_ID=heliopuddle \
HELIOPUDDLE_OIDC_CLIENT_SECRET=demo-only-client-secret-change-me \
uv run heliopuddle server start
```

`HELIOPUDDLE_OIDC_AUDIENCE` defaults to the client id (`heliopuddle`), which the
realm's Audience mapper provides; `HELIOPUDDLE_OIDC_INTROSPECT` defaults to
`sensitive` (routes needing `hp-deleter`, `hp-admin` or
`hp-client-storage-read-write`). With Keycloak
unreachable, or the issuer or secret missing, hp stops at startup with a message
naming the setting.

## The manual test (AUTH-17)

Run it once against a fresh Keycloak and hp, and paste the results table into
the PR. Needs `curl`, `jq` and `md5sum`. Keycloak access tokens last 5
minutes; if a step starts to answer `401` unexpectedly, fetch a new token.

### 0. Setup (once per shell)

```bash
export HP=http://127.0.0.1:8080/api/v1
export KC=http://localhost:8180/realms/heliopuddle
export HELIOPUDDLE_DEMO_DATA=/tmp/hp-keycloak-demo

# Password grant: prints an access token for a demo user (all use demo-password).
tok() {
  curl -s "$KC/protocol/openid-connect/token" \
    -d grant_type=password -d client_id=heliopuddle \
    -d client_secret=demo-only-client-secret-change-me \
    -d username="$1" -d password=demo-password | jq -r .access_token
}
# Call the API:  api TOKEN METHOD PATH [curl args...]  (prints the body, then "HTTP <status>")
api() {
  local t=$1 m=$2 p=$3; shift 3
  curl -s -w '\nHTTP %{http_code}\n' -X "$m" -H "Authorization: Bearer $t" "$@" "$HP$p"
}
# The id of the live file at a path in project demo:  fid TOKEN NAME
fid() { curl -s -H "Authorization: Bearer $1" "$HP/projects/demo/files/by-path?path=$2.txt" | jq .id; }

# Two files already written to the demo store, as another tool would.
for f in hello second; do
  echo "$f keycloak" > "$HELIOPUDDLE_DEMO_DATA/demo/$f.txt"
done
reg() {   # reg TOKEN NAME -> POST /projects/demo/files for $NAME.txt
  local f="$HELIOPUDDLE_DEMO_DATA/demo/$2.txt"
  api "$1" POST /projects/demo/files -H 'Content-Type: application/json' \
    -d "{\"path\":\"$2.txt\",\"hash\":\"$(md5sum <"$f" | cut -d' ' -f1)\",\"size\":$(wc -c <"$f")}"
}
```

Start hp as above, in another terminal.

### 1. Steps

| # | Step | Command | Expected |
|---|---|---|---|
| 1 | Keycloak is up, issuer is pinned | `curl -s $KC/.well-known/openid-configuration \| jq .issuer` | `"http://localhost:8180/realms/heliopuddle"` |
| 2 | hp starts in `oidc` mode | (the `server start` command above) | starts; log shows no error. With Keycloak stopped it exits non-zero naming `HELIOPUDDLE_OIDC_ISSUER` |
| 3 | Public discovery | `curl -s $HP/auth/config \| jq` | `mode` is `oidc`; `token_url` and `authorization_url` point at `localhost:8180`; no secrets |
| 4 | No token | `curl -si $HP/projects \| head -5` | `401`, `WWW-Authenticate: Bearer` |
| 5 | Bad token | `api garbage GET /projects` | `401` |
| 6 | Bearer call as admin | `A=$(tok admin-demo); api $A GET /auth/me; api $A GET /projects` | `200`, `/auth/me` shows `admin-demo`, `hp-admin`, mode `oidc`; `/projects` lists `demo` and `other` |
| 7 | Reader reads | `R=$(tok reader-demo); api $R GET /projects/demo/files` | `200` |
| 8 | Reader cannot write | `reg $R hello` | `403`, body `required_roles` includes `hp-writer` |
| 9 | Composite role | `W=$(tok writer-demo); api $W GET /auth/me` | roles include both `hp-reader` and `hp-writer` (the composite was expanded by Keycloak) |
| 10 | Writer registers | `reg $W hello; reg $W second` | `HTTP 201` for each. Run `reg $W hello` again: `HTTP 200` (same hash). Then `ID=$(fid $W second)` and `HID=$(fid $W hello)` (numbers; later steps use `$ID` and `$HID`) |
| 11 | Writer cannot delete | `api $W DELETE /files/$ID` | `403`, `required_roles` includes `hp-deleter` |
| 12 | Project-scoped user, list | `S=$(tok demo-only); api $S GET /projects` | `200`, only `demo` |
| 13 | Project-scoped user, other project | `api $S GET /projects/other` | `404` (existence hidden, not `403`) |
| 14 | Project-scoped user, own project | `api $S GET /projects/demo/files; reg $S hello` | `200`; then `200` (the file exists; `hp-writer:demo` implies read and write on `demo`) |
| 15 | Scoped user, missing role | `api $S POST /projects/demo/runs -H 'Content-Type: application/json' -d '{"kind":"index"}'` | `403` (writer is not an operator) |
| 16 | Service account | `C=$(curl -s $KC/protocol/openid-connect/token -d grant_type=client_credentials -d client_id=heliopuddle -d client_secret=demo-only-client-secret-change-me \| jq -r .access_token); api $C GET /auth/me; api $C GET /projects; reg $C hello` | `/auth/me` shows `service-account-heliopuddle` with `hp-reader`; `/projects` `200` (both); `reg` is `403` |
| 17 | Introspection: setup | `D=$(curl -s $KC/protocol/openid-connect/token -d grant_type=password -d client_id=heliopuddle -d client_secret=demo-only-client-secret-change-me -d username=admin-demo -d password=demo-password); AT=$(echo $D \| jq -r .access_token); RT=$(echo $D \| jq -r .refresh_token); api $AT GET /auth/me` | `200`, token valid |
| 18 | Introspection: end the Keycloak session | `curl -s -o /dev/null -w '%{http_code}\n' $KC/protocol/openid-connect/logout -d client_id=heliopuddle -d client_secret=demo-only-client-secret-change-me -d refresh_token=$RT` | `204`. (Alternative: in the admin console, realm `heliopuddle` > Users > `admin-demo` > Sessions > Sign out, or disable the user) |
| 19 | The JWT is still valid locally | `api $AT GET /auth/me` | `200` (hp validates the signature and `exp` locally; `/auth/me` is not a sensitive route) |
| 20 | Sensitive route rejects it | `api $AT DELETE /files/$ID` | `401` (introspection says `active: false`). The file is untouched: `api $(tok admin-demo) GET /files/$ID` is `HTTP 200` with `deleted_at` null |
| 21 | A fresh login deletes | `A=$(tok admin-demo); api $A DELETE /files/$ID` | `200`, the body has `deleted_at` set. `api $A DELETE /files/$ID` again: `404` |
| 22 | Introspection outage | `A=$(tok admin-demo); docker compose stop keycloak; api $A DELETE /files/$HID; api $A GET /projects; docker compose start keycloak` | the `DELETE` is `502` (hp cannot ask the issuer); the `GET` is `200` (local validation needs no issuer call once the JWKS is cached). Wait for Keycloak to come back before step 23 |
| 23 | UI login (admin) | Open <http://localhost:8080/> in a private window | redirected to Keycloak's login page; sign in as `admin-demo` / `demo-password`; land on the hp UI with both projects, and the nav shows `admin-demo` and a logout. Write controls are visible: "Index now" on a project page, enable/disable/run on the rules page |
| 24 | UI, scoped user | Keycloak keeps its own SSO session, so first use a **new private window** (or sign out at <http://localhost:8180/realms/heliopuddle/account>), then open <http://localhost:8080/> and sign in as `demo-only` | the project picker lists only `demo`; <http://localhost:8080/projects/other> shows the not-found error page; "Index now", rule controls and delete are hidden (no `hp-operator` or `hp-deleter`) |
| 25 | UI, reader | Same, as `reader-demo` | no write controls at all (no Accept, rule controls, "Index now", delete) |
| 26 | UI logout | Use the nav logout | hp ends the Keycloak session too (Keycloak may ask you to confirm), then returns to the hp login page; opening a UI page afterwards asks for Keycloak credentials again instead of signing you back in silently |

Notes:

- Statuses are printed by `api` as `HTTP <code>`; the "Expected" column gives
  the code first.
- To see what hp sees, decode a token's payload:
  `echo $A | cut -d. -f2 | tr '_-' '/+' | base64 -d 2>/dev/null | jq '{iss,aud,preferred_username,realm_access}'`.
- The Python client does the same with
  `HelioPuddleClient("http://127.0.0.1:8080/api/v1", "demo", client_id="heliopuddle", client_secret="demo-only-client-secret-change-me")`
  (service account) or `username="writer-demo", password="demo-password"`.

### 2. Results (copy into the PR)

Date: ____  Keycloak image: `quay.io/keycloak/keycloak:26.4.0`  hp commit: ____

| Step(s) | What | Result (pass/fail, note) |
|---|---|---|
| 1-3 | Startup in `oidc` mode, fail-fast, `/auth/config` | |
| 4-6 | `401` without or with a bad token; bearer call as admin | |
| 7-11 | Reader read and `403` on write; composite role; writer register; `403` on delete | |
| 12-15 | Project-scoped role: list, `404` on other project, own project, `403` on missing role | |
| 16 | Service account (`client_credentials`) | |
| 17-21 | Introspection on delete: `401` after session end, `200` after a fresh login | |
| 22 | Introspection outage: `502` on a sensitive route, `200` on reads | |
| 23-26 | UI login through Keycloak, scoped project picker, hidden controls, logout | |

## Troubleshooting

| Symptom | Cause |
|---|---|
| hp exits at startup, naming `HELIOPUDDLE_OIDC_ISSUER` | Keycloak is not up yet, or the issuer is not exactly `http://localhost:8180/realms/heliopuddle` (no trailing slash) |
| every token gets `401` | `iss` differs from the configured issuer (decode the token, see above), or `aud` lacks `heliopuddle` (the Audience mapper is missing) |
| token endpoint answers `invalid_grant: Account is not fully set up` | a user lacks first name, last name or email (Keycloak's user profile requires them); the demo users have them |
| `200` on `/auth/me` but `403`/`404` everywhere else | the token has no hp roles: check `realm_access.roles` |
| the realm was changed in the import file but not in Keycloak | the import runs only when the realm does not exist yet: `docker compose down -v` (or delete the realm in the admin console), then `up` again |
| the UI login redirects back with an `invalid redirect_uri` error | hp is served on a port or host other than `localhost:8080` / `127.0.0.1:8080`: add the redirect URI to the client |

Stop and clean up: `docker compose down -v` (and `rm -rf /tmp/hp-keycloak-demo`).
