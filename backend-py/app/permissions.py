"""Permission registry — single source of truth for all feature gates.

Frontend reads this via ``GET /api/permissions/registry`` to render
the per-user permissions matrix; backend uses ``ALL_KEYS`` to validate
incoming permission updates and ``require_permission(key)`` to gate
sensitive routes.

To add a new gated feature:

    1. Add a tuple to the relevant group below.
    2. Wrap the frontend control with ``<Can permission="your.key">``.
    3. (Optional) Gate the backend route via ``Depends(require_permission("your.key"))``.

Super admins implicitly hold every permission and bypass all checks.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status

from app.dependencies import get_current_user


# (key, friendly label). Order within a group is rendering order in the UI.
PERMISSION_GROUPS: dict[str, list[tuple[str, str]]] = {
    "Dashboard": [
        ("dashboard.view", "View dashboard"),
    ],
    "Course Search": [
        ("search.view", "Use course search"),
    ],
    "Universities": [
        ("universities.view", "View universities list and detail"),
        ("universities.create", "Create new universities"),
        ("universities.edit", "Edit university details"),
        ("universities.delete", "Delete universities"),
        ("universities.bulk_import", "Bulk import universities (CSV)"),
    ],
    "Courses": [
        ("courses.view", "View course details"),
        ("courses.create", "Create new courses"),
        ("courses.edit", "Edit existing courses"),
        ("courses.delete", "Delete courses"),
    ],
    "Raw / Staged Data": [
        ("staged.view", "View raw scraped data tab"),
        ("staged.approve", "Approve or reject scraped courses"),
        ("staged.edit", "Edit staged courses before approval"),
        ("staged.delete", "Delete staged courses"),
    ],
    "Scraping": [
        ("scraping.view", "View scraping jobs and history"),
        ("scraping.trigger", "Trigger new scrape / repair jobs"),
    ],
    "Bulk Upload": [
        ("bulk.view", "View bulk upload page"),
        ("bulk.import", "Import bulk Excel files"),
    ],
    "Data Backup": [
        ("backup.view", "View backups"),
        ("backup.create", "Create or restore backups"),
    ],
    "Settings": [
        ("settings.view", "View settings"),
        ("settings.edit", "Edit academic levels / acronyms"),
    ],
    "Users & Permissions": [
        ("users.manage", "Create, edit, delete users and toggle their permissions"),
    ],
}


ALL_KEYS: frozenset[str] = frozenset(
    key for group in PERMISSION_GROUPS.values() for (key, _) in group
)


def registry_payload() -> list[dict]:
    """Shape returned by GET /api/permissions/registry."""
    return [
        {
            "group": group,
            "permissions": [{"key": k, "label": label} for (k, label) in items],
        }
        for group, items in PERMISSION_GROUPS.items()
    ]


def user_has_permission(user_payload: dict, key: str) -> bool:
    """True for super admins, true if `key` is in the user's granted set."""
    if user_payload.get("is_super_admin"):
        return True
    perms = user_payload.get("permissions") or []
    return key in perms


def require_permission(key: str):
    """FastAPI dependency factory: 403 unless the current user holds `key`."""

    async def _dep(user: Annotated[dict, Depends(get_current_user)]) -> dict:
        if not user_has_permission(user, key):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing permission: {key}",
            )
        return user

    return _dep
