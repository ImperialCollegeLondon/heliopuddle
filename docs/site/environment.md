# Environment variables

Settings are read from `HELIOPUDDLE_*` environment variables (the server also
reads a few from [Prefect](https://docs.prefect.io/)). Durations are
human-readable (`30d`, `12h`, `45m`, `60s`) and sizes accept units (`1GB`).
Project configuration (storage locations, rules) is YAML, not environment
variables; see the [quick start](quick-start.md).

The tables below are generated from the settings models by
`scripts/env_table.py` and checked by a unit test.

<!-- env-table:start -->
### Server settings

| Variable | Default | Description |
|---|---|---|
| `HELIOPUDDLE_CONFIG_DIR` | `config` | Directory of project YAML files. Docker image: `/config`. |
| `HELIOPUDDLE_DATA_DIR` | `.heliopuddle` | State directory (SQLite database, signing key, staging). Docker image: `/var/lib/heliopuddle`. |
| `HELIOPUDDLE_DATABASE_URL` |  | SQLAlchemy URL; defaults to SQLite under data_dir |
| `HELIOPUDDLE_HOST` | `127.0.0.1` | Interface the server binds (`--host` overrides). |
| `HELIOPUDDLE_PORT` | `8080` | Port the server binds (`--port` overrides). |
| `HELIOPUDDLE_API_ROOT_PATH` |  | URL prefix when the API is served behind a reverse proxy under a sub-path. |
| `HELIOPUDDLE_USER_HEADER` | `X-Forwarded-User` | Header carrying the user id; trusted only when auth_mode is 'header' |
| `HELIOPUDDLE_AUTH_MODE` | `local` | 'local': users and passwords in the DB; 'oidc': tokens from an external provider such as Keycloak; 'header': a trusted proxy sets user_header |
| `HELIOPUDDLE_ADMIN_USERNAME` | `admin` | First admin |
| `HELIOPUDDLE_ADMIN_PASSWORD` |  | First admin's password; generated and logged once when unset |
| `HELIOPUDDLE_ADMIN_RESET` | `false` | Reset the admin's password at startup (needs admin_password) |
| `HELIOPUDDLE_AUTH_SECRET_KEY` |  | Signs local tokens and UI sessions; generated into data_dir |
| `HELIOPUDDLE_AUTH_TOKEN_TTL` | `8h` | Lifetime of a local access token |
| `HELIOPUDDLE_LOGIN_MAX_FAILURES` | `10` | Failed password logins per username before it is blocked for login_window |
| `HELIOPUDDLE_LOGIN_MAX_FAILURES_PER_IP` | `100` | Failed password logins per client address before it is blocked for login_window; higher than the per-username limit because the UI's API calls share one address |
| `HELIOPUDDLE_LOGIN_WINDOW` | `15m` | How long failed logins are counted, and how long a block lasts |
| `HELIOPUDDLE_OIDC_ISSUER` |  | e.g. https://host/realms/&lt;realm&gt; |
| `HELIOPUDDLE_OIDC_DISCOVERY_URL` |  | Where hp reads the provider's discovery document, when hp reaches the provider at another address than browsers do (e.g. on a private network); default: &lt;issuer&gt;/.well-known/openid-configuration |
| `HELIOPUDDLE_OIDC_CLIENT_ID` |  | OIDC client id (required when `auth_mode` is `oidc`). |
| `HELIOPUDDLE_OIDC_CLIENT_SECRET` |  | OIDC client secret (required in `oidc` mode unless `oidc_introspect` is `off`). |
| `HELIOPUDDLE_OIDC_AUDIENCE` |  | Defaults to the client id |
| `HELIOPUDDLE_OIDC_USERNAME_CLAIM` | `preferred_username` | Token claim holding the user name. |
| `HELIOPUDDLE_OIDC_ROLES_CLAIM` | `realm_access.roles` | Dotted path to the roles list in the token |
| `HELIOPUDDLE_OIDC_INTROSPECT` | `sensitive` | Ask the provider whether a token is still active: never, on routes needing hp-deleter, hp-client-storage-read-write or hp-admin, or on every route |
| `HELIOPUDDLE_UI_HOST` |  | Defaults to host |
| `HELIOPUDDLE_UI_PORT` |  | Unset or equal to port: the UI shares the API's port |
| `HELIOPUDDLE_UI_API_URL` |  | API base URL the UI calls; defaults to this process's API |
| `HELIOPUDDLE_UI_ROOT_PATH` |  | URL prefix when the UI is served behind a reverse proxy under a sub-path. |
| `HELIOPUDDLE_PREFECT_UI_URL` |  | Link target for the Prefect UI; derived from PREFECT_API_URL |
| `HELIOPUDDLE_FRAME_ANCESTORS` | `'none'` | Who may show hp pages in a frame: the sources of the CSP frame-ancestors directive, e.g. 'self' for a portal on the same origin |
| `HELIOPUDDLE_OP_LEASE` | `1h` | How long an upload, overwrite or delete owns a path before it expires and may be repaired (renew extends it) |
| `HELIOPUDDLE_PREVIEW_MAX_BYTES` | `1GB` | The largest CDF copied to a temporary file to preview it from a storage location without local file paths |
| `HELIOPUDDLE_MAX_UPLOAD_SIZE` | `1GiB` | The largest file a reservation or registration may declare, e.g. '1GiB'; bigger ones get 413 |
| `HELIOPUDDLE_RCLONE_BINARY` |  | The rclone executable for rclone storage locations; default: rclone on PATH, else the pinned release downloaded into &lt;data dir&gt;/bin |
| `HELIOPUDDLE_RCLONE_AUTO_DOWNLOAD` | `true` | Download the pinned rclone release when no rclone is found |
| `HELIOPUDDLE_RCLONE_API_WAIT` | `30s` | How long an API request waits for a busy rclone config before answering 503 with Retry-After |
| `HELIOPUDDLE_RCLONE_IDLE` | `2s` | How long an rclone session (and its rcd and config lock) stays open after its last use |

### Client and CLI

| Variable | Default | Description |
|---|---|---|
| `HELIOPUDDLE_API_URL` |  | API base URL for `heliopuddle file` and `user`, e.g. `http://localhost:8080/api/v1`. |
| `HELIOPUDDLE_PROJECT` | `default` | Project for `heliopuddle file`. |
| `HELIOPUDDLE_USERNAME` |  | Account the CLI logs in as. |
| `HELIOPUDDLE_PASSWORD` |  | Password for `HELIOPUDDLE_USERNAME` (else a hidden prompt or `--password-stdin`). |
| `HELIOPUDDLE_NEW_PASSWORD` |  | New password for `heliopuddle user` commands that set one (else a prompt). |
| `HELIOPUDDLE_TOKEN` |  | Bearer token for the CLI, instead of a username and password. |
| `HELIOPUDDLE_USER` |  | User id sent in the user header, for servers in `header` auth mode. |
<!-- env-table:end -->

## Prefect

Common Prefect variables:

| Variable | Meaning |
|---|---|
| `PREFECT_API_URL` | Use an external Prefect server (for example `http://prefect:4200/api`) instead of the embedded one. heliopuddle checks it at startup and fails fast if unreachable. |

Other `PREFECT_*` settings are Prefect's own; see the Prefect documentation.
