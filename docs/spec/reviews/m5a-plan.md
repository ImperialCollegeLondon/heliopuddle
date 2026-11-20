# M5A spec review and build plan (approved)

## Context

Until now hp trusts a user id header set by a proxy (`HELIOPUDDLE_USER_HEADER`);
anything that can reach the API can claim to be any user, and there are no
permissions. M5A adds authentication and authorization:

- hp works on its own: users with passwords, the OAuth2 **password flow**;
- hp works with an external **Keycloak** (full OAuth): bearer tokens are
  validated against the issuer's JWKS, following the
  [Skycloak FastAPI guide](https://skycloak.io/blog/keycloak-fastapi-python-api-authentication/);
- the first start creates one admin user, securely (generated password, or env
  vars);
- authorization is by Keycloak **realm roles**, global or scoped to a project.

M5A is built between M5 (happening elsewhere) and M6. The earlier "M9: Auth"
row is replaced. Nothing is renumbered, and requirements use `AUTH-xx` only so
they cannot collide with M5's.

## Decisions

See "M5A review decisions" in [features.md](../features.md) (A1–A9).

You (the owner) made these calls during the review:

| Topic | Decision |
|---|---|
| Name | M5A |
| Default mode | `local`; existing proxy deployments set `HELIOPUDDLE_AUTH_MODE=header` after upgrading (upgrade note) |
| User management | Through the API (CRUD), and the CLI only calls the API |
| Roles | Global by default, plus optional roles scoped to one or more projects |
| Authorization source | Realm roles, not OAuth scopes |
| DEP-14 (now AUTH-17) | `examples/keycloak` and one manual test; no automated e2e |

## Requirements

Every in-scope requirement, with its full description, is in
[requirements/auth.md](../requirements/auth.md): AUTH-01 to AUTH-17. The
design is in [design/auth.md](../../design/auth.md).

## Test strategy

- **Unit:** a fake OIDC issuer (generated RSA key and JWKS), the role ×
  endpoint and project-scope matrices, the user CRUD API and the user CLI
  (Typer runner against a test API).
- **Integration:** the existing suite runs with `auth_mode="header"`, plus
  Postgres coverage of the `user` table and the first-admin race.
- **E2E:** the existing Docker and Playwright suite runs with
  `HELIOPUDDLE_AUTH_MODE=header`, plus one Playwright login test in `local`
  mode.
- **Manual:** one test against `examples/keycloak` (AUTH-17).

## Tooling changes

- `scripts/req_matrix.py` accepts milestones with a letter suffix (`M5A`
  sorts after `M5` and before `M6`); `pyproject.toml` sets `milestone = "M5A"`.
- New dependencies: `PyJWT[crypto]`, `pwdlib[argon2]`, `python-multipart`,
  `itsdangerous`.
- Import-linter: the user CLI module may not import the DB layer.
- Migration `0006` follows M4's `0005`; M5 added no migration, so the rebase
  keeps `0006` unchanged.
