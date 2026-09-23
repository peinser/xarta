"""Safe filesystem cleanup helpers for scheduled jobs."""

from __future__ import annotations

import asyncio
import datetime

from pathlib import Path


def _remove_expired_files(root: Path, older_than_days: int) -> int:
    root = root.expanduser().resolve(strict=True)
    if root == Path(root.anchor):
        raise ValueError("Refusing to clean a filesystem root")
    if not root.is_dir():
        raise ValueError(f"Cleanup path is not a directory: {root}")

    cutoff = datetime.datetime.now(tz=datetime.UTC).timestamp() - (
        older_than_days * 24 * 60 * 60
    )
    removed = 0
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink() and path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1
    return removed


async def remove_expired_files(root: str, older_than_days: int) -> int:
    """Remove old regular files without blocking the application event loop."""

    if older_than_days < 1:
        raise ValueError("older_than_days must be at least one")
    return await asyncio.to_thread(_remove_expired_files, Path(root), older_than_days)
