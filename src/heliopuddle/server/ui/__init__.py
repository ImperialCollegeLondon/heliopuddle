"""Web UI (FastAPI + Jinja2). Reads only via heliopuddle.client (UI-05).

Use :func:`create_ui` to build the app; routes are relative to the UI root.

Layout: ``app`` builds the app; ``pages/<page>`` holds each page's routes,
helpers and templates; ``shared`` holds code used by several pages; ``auth``
holds login and sessions; ``templates`` holds the shared templates.
"""

from heliopuddle.server.ui.app import (
    ClientFactory,
    create_ui,
    default_api_url,
    human_size,
    prefect_ui_url,
)

__all__ = ["ClientFactory", "create_ui", "default_api_url", "human_size", "prefect_ui_url"]
