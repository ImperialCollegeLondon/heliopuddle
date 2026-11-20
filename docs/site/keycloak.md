# Keycloak (OIDC) deployment

heliopuddle can authenticate users against an external OpenID Connect provider
such as Keycloak (`HELIOPUDDLE_AUTH_MODE=oidc`). The API accepts bearer tokens;
the UI signs users in through the provider (authorization code flow with PKCE).
All values below are placeholders: replace every `change-me`.

## 1. Configure the Keycloak client

In your realm create a client with:

| Setting | Value |
|---|---|
| Client ID | `heliopuddle` |
| Client authentication | on (a confidential client) |
| Standard flow | on |
| Direct access grants | on, only if clients log in with a username and password |
| Service accounts | on, only if machines use the `client_credentials` grant |
| Valid redirect URIs | `https://hp.example.org/*` (the UI's `/auth/callback` must be allowed) |
| Valid post logout redirect URIs | `https://hp.example.org/*` |
| Audience | an *Audience* mapper adding `heliopuddle` to the access token (Keycloak's default `aud` is `account`), or set `HELIOPUDDLE_OIDC_AUDIENCE` to match |

Then:

- Create **realm roles** named `hp-reader`, `hp-writer`, `hp-deleter`,
  `hp-operator`, `hp-client-storage-read-write` and `hp-admin`, and assign them
  to users. A project-scoped role is `<role>:<project>`, for example
  `hp-writer:demo` (`hp-admin` cannot be scoped).
- Make sure the token carries the roles at `realm_access.roles` (the default
  `roles` client scope does). Another location can be set with
  `HELIOPUDDLE_OIDC_ROLES_CLAIM`.
- Copy the client secret from the client's Credentials tab.

A ready-made realm with all of this is in the repository's
[`examples/keycloak`](https://github.com/ImperialCollegeLondon/heliopuddle/tree/main/examples/keycloak)
directory (demo only: fixed passwords, plain http).

## 2. Run heliopuddle

The issuer must equal the token's `iss` exactly (no trailing slash), and both
browsers and heliopuddle must reach Keycloak at that address. heliopuddle reads
the provider's discovery document at startup and stops with a message naming the
setting if it cannot.

```yaml
services:
  heliopuddle:
    image: heliopuddle/heliopuddle:latest
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      HELIOPUDDLE_AUTH_MODE: oidc
      HELIOPUDDLE_OIDC_ISSUER: https://keycloak.example.org/realms/heliopuddle
      HELIOPUDDLE_OIDC_CLIENT_ID: heliopuddle
      HELIOPUDDLE_OIDC_CLIENT_SECRET: change-me
      # Needed when several heliopuddle processes share sessions (at least 32 characters):
      # HELIOPUDDLE_AUTH_SECRET_KEY: change-me-change-me-change-me-change-me
    volumes:
      - hp-state:/var/lib/heliopuddle
      - hp-data:/data
      # - ./projects:/config:ro

volumes:
  hp-state:
  hp-data:
```

The same variables work with `heliopuddle server start` outside Docker, or in an
env file passed with `--env-file`.

`HELIOPUDDLE_OIDC_INTROSPECT` defaults to `sensitive`: for routes needing
`hp-deleter`, `hp-client-storage-read-write` or `hp-admin`, heliopuddle asks
Keycloak whether the token is still active, which needs the client secret. Set it
to `off` to validate tokens locally only, or `always` to check every route.

## 3. Use it

- UI: open the site; you are redirected to Keycloak to sign in.
- Python client: `HelioPuddleClient(url, "demo", client_id="heliopuddle",
  client_secret="change-me")` for a service account, or `username=` and
  `password=`.
- CLI: pass a token with `--token` or `HELIOPUDDLE_TOKEN`.

Behind a TLS-terminating proxy, make sure the proxy forwards the original scheme
and host so the redirect URI heliopuddle builds matches one allowed in Keycloak.
