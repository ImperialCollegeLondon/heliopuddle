"""``heliopuddle file ...``: add, register, list, show, update, download and remove files.

Requirement CLI-15.

A thin layer over :class:`heliopuddle.client.HelioPuddleClient`, so it works
from any machine that can reach the API and needs only ``heliopuddle[cli]``.

Connection: ``--api-url`` (env ``HELIOPUDDLE_API_URL``) and ``--project``
(env ``HELIOPUDDLE_PROJECT``, default ``default``). Credentials, as for
``heliopuddle user``: ``--token`` (env ``HELIOPUDDLE_TOKEN``), or ``--username``
(env ``HELIOPUDDLE_USERNAME``) with the password from ``--password-stdin``, env
``HELIOPUDDLE_PASSWORD`` or a hidden prompt. With neither, requests carry only
``--user`` (env ``HELIOPUDDLE_USER``) in the user header, for servers in
``header`` auth mode.

Files are chosen by id (the positional arguments) or with ``--path GLOB`` /
``--name GLOB``, which match live files: a digit-only argument is always an
id, so a file whose path is ``123`` is reached with ``--path 123``.

Exit codes: 0 on success, 1 when the API refuses or cannot be reached (or a
file conflicts), 2 for invalid options.
"""

from __future__ import annotations

from heliopuddle.cli_file import commands_read, commands_update, commands_write
from heliopuddle.cli_file.app import file_app

__all__ = ["commands_read", "commands_update", "commands_write", "file_app"]
