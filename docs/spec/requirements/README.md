# Requirements

Each component has a file of requirements with stable IDs. A requirement has a
milestone; once that milestone is being built, the requirement must have at
least one passing test tagged with its ID:

```python
@pytest.mark.req("API-01")
def test_register_and_list(...): ...
```

`uv run python scripts/req_matrix.py` prints the requirement → test → result
matrix. CI fails if any requirement due in the current milestone (or earlier)
has no passing test. The current milestone is set in `pyproject.toml`
(`[tool.heliopuddle] milestone = "M1"`). A milestone may carry a letter
(`M5A` sorts between `M5` and `M6`).

| File | Prefix | Component |
|---|---|---|
| [client.md](client.md) | `CLI` | `heliopuddle.client` |
| [api.md](api.md) | `API` | `heliopuddle.server.api` |
| [db.md](db.md) | `DB` | `heliopuddle.server.database`, `models` |
| [storage.md](storage.md) | `STO` | `heliopuddle.storage`, copy/move/delete operations |
| [services.md](services.md) | `SVC` | `heliopuddle.server.services` (Prefect flows) |
| [ui.md](ui.md) | `UI` | `heliopuddle.server.ui` |
| [deploy.md](deploy.md) | `DEP` | CLI, Docker, settings |
| [auth.md](auth.md) | `AUTH` | authentication and authorization (M5A) |
| [../reviews/](../reviews) | — | approved milestone plans |
| [later.md](later.md) | `ANA`, `ING` | M8–M9 (draft) |

Format: `ID (milestone)` then the requirement, then how it is verified.
