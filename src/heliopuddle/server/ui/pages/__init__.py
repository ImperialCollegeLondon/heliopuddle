"""One package per page: its routes, helpers and templates together.

Each page package exposes a ``router``; :func:`~heliopuddle.server.ui.app.create_ui`
includes them in route order. A page's templates sit beside its code and are
named by folder (``files/files.html``); templates used by several pages are in
``heliopuddle/server/ui/templates`` and code used by several pages is in
``heliopuddle/server/ui/shared``.
"""
