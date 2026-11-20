"""Marks tests that run ``add_file`` end to end against the in-process API.

The client follows the upload reservation protocol (CLI-08), so these tests
need the server's ``/projects/{p}/uploads`` and ``/uploads/{token}/...``
endpoints. They are skipped, with that reason, while the API has none, and run
as soon as it does.
"""

from __future__ import annotations

import pytest


def upload_api_available() -> bool:
    """True if the API serves ``/projects/{p}/uploads``."""
    from heliopuddle.server.api import create_api

    return any(path.endswith("/uploads") for path in create_api().openapi()["paths"])


requires_upload_api = pytest.mark.skipif(
    not upload_api_available(),
    reason="needs the upload reservation endpoints (POST /projects/{p}/uploads)",
)
