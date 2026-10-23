"""Owner-scoped character persistence and project association checks."""
from __future__ import annotations

from typing import Any, Optional

import database

CHARACTER_COLUMNS = (
    "id, user_id, project_id, name, profile, reference_asset_url, created_at, updated_at"
)


async def list_characters(
    user_id: str, project_id: Optional[str] = None, limit: int = 50
) -> list[dict[str, Any]]:
    if project_id is None:
        return await database.fetch_all(
            f"""SELECT {CHARACTER_COLUMNS} FROM characters
                WHERE user_id = %s
                ORDER BY updated_at DESC, id DESC LIMIT %s""",
            (user_id, limit),
        )
    return await database.fetch_all(
        f"""SELECT {CHARACTER_COLUMNS} FROM characters
            WHERE user_id = %s AND project_id = %s
            ORDER BY updated_at DESC, id DESC LIMIT %s""",
        (user_id, project_id, limit),
    )


async def get_character(user_id: str, character_id: str) -> Optional[dict[str, Any]]:
    return await database.fetch_one(
        f"""SELECT {CHARACTER_COLUMNS} FROM characters
            WHERE id = %s AND user_id = %s""",
        (character_id, user_id),
    )


async def _owned_active_project(conn, user_id: str, project_id: str) -> bool:
    result = await (await conn.execute(
        """SELECT id FROM projects
           WHERE id = %s AND user_id = %s AND status = 'active'
           FOR KEY SHARE""",
        (project_id, user_id),
    )).fetchone()
    return result is not None


async def create_character(
    user_id: str,
    character_id: str,
    project_id: Optional[str],
    name: str,
    profile: dict[str, Any],
) -> Optional[dict[str, Any]]:
    async with database._pool.connection() as conn:
        async with conn.transaction():
            if project_id is not None and not await _owned_active_project(
                conn, user_id, project_id
            ):
                return None
            row = await (await conn.execute(
                f"""INSERT INTO characters (id, user_id, project_id, name, profile)
                    VALUES (%s, %s, %s, %s, %s::jsonb)
                    RETURNING {CHARACTER_COLUMNS}""",
                (character_id, user_id, project_id, name,
                 __import__("json").dumps(profile)),
            )).fetchone()
            return row


async def update_character(
    user_id: str,
    character_id: str,
    *,
    name: Optional[str] = None,
    profile: Optional[dict[str, Any]] = None,
    project_id: Optional[str] = None,
    update_name: bool = False,
    update_profile: bool = False,
    update_project: bool = False,
) -> Optional[dict[str, Any]]:
    assignments: list[str] = []
    params: list[Any] = []
    if update_name:
        assignments.append("name = %s")
        params.append(name)
    if update_profile:
        assignments.append("profile = %s::jsonb")
        params.append(__import__("json").dumps(profile))
    if update_project:
        assignments.append("project_id = %s")
        params.append(project_id)
    if not assignments:
        return await get_character(user_id, character_id)

    async with database._pool.connection() as conn:
        async with conn.transaction():
            if update_project and project_id is not None and not await _owned_active_project(
                conn, user_id, project_id
            ):
                return None
            assignments.append("updated_at = NOW()")
            params.extend((character_id, user_id))
            return await (await conn.execute(
                f"""UPDATE characters SET {", ".join(assignments)}
                    WHERE id = %s AND user_id = %s
                    RETURNING {CHARACTER_COLUMNS}""",
                tuple(params),
            )).fetchone()


async def delete_character(user_id: str, character_id: str) -> bool:
    row = await database.fetch_one(
        """DELETE FROM characters
           WHERE id = %s AND user_id = %s
           RETURNING id""",
        (character_id, user_id),
    )
    return row is not None
