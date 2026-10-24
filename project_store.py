"""Owner-scoped project persistence.

Every lookup and mutation includes the authenticated user ID in SQL. Callers must
pass the user ID returned by the verified bearer-token dependency; never accept
an owner ID from a request body or query parameter.
"""
from __future__ import annotations

from typing import Any, Optional

import database


PROJECT_COLUMNS = "id, user_id, name, description, status, created_at, updated_at"


async def list_projects(user_id: str, limit: int = 50) -> list[dict[str, Any]]:
    return await database.fetch_all(
        f"""SELECT {PROJECT_COLUMNS} FROM projects
            WHERE user_id = %s AND status = 'active'
            ORDER BY updated_at DESC, id DESC
            LIMIT %s""",
        (user_id, limit),
    )


async def get_project(user_id: str, project_id: str) -> Optional[dict[str, Any]]:
    return await database.fetch_one(
        f"""SELECT {PROJECT_COLUMNS} FROM projects
            WHERE id = %s AND user_id = %s AND status = 'active'""",
        (project_id, user_id),
    )


async def create_project(
    user_id: str, project_id: str, name: str, description: Optional[str]
) -> dict[str, Any]:
    return await database.fetch_one(
        f"""INSERT INTO projects (id, user_id, name, description)
            VALUES (%s, %s, %s, %s)
            RETURNING {PROJECT_COLUMNS}""",
        (project_id, user_id, name, description),
    )


async def update_project(
    user_id: str,
    project_id: str,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    update_name: bool = False,
    update_description: bool = False,
) -> Optional[dict[str, Any]]:
    # Fixed SQL fragments only; user-provided values are always bound parameters.
    assignments: list[str] = []
    params: list[Any] = []
    if update_name:
        assignments.append("name = %s")
        params.append(name)
    if update_description:
        assignments.append("description = %s")
        params.append(description)
    if not assignments:
        return await get_project(user_id, project_id)
    assignments.append("updated_at = NOW()")
    params.extend((project_id, user_id))
    return await database.fetch_one(
        f"""UPDATE projects SET {", ".join(assignments)}
            WHERE id = %s AND user_id = %s AND status = 'active'
            RETURNING {PROJECT_COLUMNS}""",
        tuple(params),
    )


async def delete_project(user_id: str, project_id: str) -> bool:
    row = await database.fetch_one(
        """DELETE FROM projects
           WHERE id = %s AND user_id = %s
           RETURNING id""",
        (project_id, user_id),
    )
    return row is not None
