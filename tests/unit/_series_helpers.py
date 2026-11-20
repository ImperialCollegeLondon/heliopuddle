"""Shared helpers and fixtures for the split series tests."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from heliopuddle.config.models import DEFAULT_VERSION_RULES, VersionRule
from heliopuddle.server import models
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import File, Series

RULES = [
    VersionRule(
        paths=["calibration/layers/**"],
        version_type="text",
        extraction_pattern=r"_v(?P<major>\d{1,3})\.(?P<minor>\d{1,4})\.(?P<ext>[^./]+)$",
        version_text="{major:0>3}.{minor:0>4}",
    ),
    VersionRule(paths=["spice/**"], extraction_pattern=r"_(?P<version>\d{2})\.(?P<ext>[^./]+)$"),
    VersionRule(paths=["dated/**"], extraction_pattern=r"_(?P<date>\d{8})_v(?P<version>\d+)\."),
    *DEFAULT_VERSION_RULES,
]


def reg(
    ctx: AppContext,
    path: str,
    hash: str = "h",
    *,
    location: str = "primary",
    rules: list[VersionRule] | None = None,
    **kwargs: Any,
) -> models.RegisterResult:
    with ctx.write_session() as session:
        return models.register_file(
            session,
            project="demo",
            storage_location=location,
            path=path,
            hash=hash,
            size=1,
            rules=RULES if rules is None else rules,
            **kwargs,
        )


def remove(ctx: AppContext, file_id: int) -> None:
    with ctx.write_session() as session:
        _, copies = models.begin_delete(session, file_id, updated_by="carol")
    with ctx.write_session() as session:
        models.finish_delete(session, file_id, copies, updated_by="carol")


def _stored(key: str) -> tuple[str, str]:
    """The stored forms a displayed series key can have (API-13 prefixes)."""
    return ("v:" + key, "p:" + key)


def latest_paths(ctx: AppContext, series_key: str) -> tuple[list[str], str | None]:
    """Return the live paths with is_latest, and the path series.latest_file_id points at."""
    with ctx.sessionmaker() as session:
        series = session.scalars(
            select(Series).where(Series.series_key.in_(_stored(series_key)))
        ).one()
        flagged = session.scalars(
            select(File.path).where(File.series_id == series.id, File.is_latest.is_(True))
        ).all()
        pointed = (
            session.get_one(File, series.latest_file_id).path
            if series.latest_file_id is not None
            else None
        )
        return sorted(flagged), pointed
