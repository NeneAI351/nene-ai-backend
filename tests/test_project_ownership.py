"""PostgreSQL-backed project ownership and API boundary tests."""
import os
import uuid

import httpx
import pytest
import pytest_asyncio

import app
import database
from auth import get_authenticated_user_id


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(scope="session")
async def projects_postgres():
    if not os.getenv("DATABASE_URL"):
        pytest.skip("DATABASE_URL is required for project ownership integration tests.")
    await database.start_database()
    await database.initialize_schema()
    yield
    await database.close_database()


async def _create_user():
    user_id = str(uuid.uuid4())
    async with database._pool.connection() as conn:
        await conn.execute("INSERT INTO users(id) VALUES (%s)", (user_id,))
        await conn.commit()
    return user_id


async def _client_for(user_id=None):
    async def authenticated_user():
        if user_id is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="Authentication required.")
        return user_id

    app.app.dependency_overrides[get_authenticated_user_id] = authenticated_user
    transport = httpx.ASGITransport(app=app.app)
    client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    return client


async def test_project_crud_is_owner_scoped(projects_postgres):
    user_a, user_b = await _create_user(), await _create_user()
    async with await _client_for(user_a) as client_a:
        created = await client_a.post(
            "/api/projects",
            json={"name": "  My film  ", "description": "Private project"},
        )
        assert created.status_code == 201, created.text
        project = created.json()
        project_id = project["id"]
        assert project["name"] == "My film"
        assert project["user_id"] == user_a

        listed = await client_a.get("/api/projects")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["items"]] == [project_id]

        updated = await client_a.patch(
            f"/api/projects/{project_id}", json={"description": "Updated"}
        )
        assert updated.status_code == 200
        assert updated.json()["description"] == "Updated"

    async with await _client_for(user_b) as client_b:
        assert (await client_b.get("/api/projects")).json()["items"] == []
        assert (await client_b.get(f"/api/projects/{project_id}")).status_code == 404
        assert (await client_b.patch(
            f"/api/projects/{project_id}", json={"name": "Stolen"}
        )).status_code == 404
        assert (await client_b.delete(f"/api/projects/{project_id}")).status_code == 404

    async with await _client_for(user_a) as client_a:
        still_owned = await client_a.get(f"/api/projects/{project_id}")
        assert still_owned.status_code == 200
        assert still_owned.json()["name"] == "My film"
        deleted = await client_a.delete(f"/api/projects/{project_id}")
        assert deleted.status_code == 204
        assert (await client_a.get(f"/api/projects/{project_id}")).status_code == 404

    app.app.dependency_overrides.clear()


async def test_project_routes_require_authentication(projects_postgres):
    app.app.dependency_overrides.clear()
    async with await _client_for(None) as client:
        assert (await client.get("/api/projects")).status_code == 401
        assert (await client.post("/api/projects", json={"name": "No auth"})).status_code == 401
    app.app.dependency_overrides.clear()


async def test_project_input_validation_and_cannot_choose_owner(projects_postgres):
    user_id = await _create_user()
    async with await _client_for(user_id) as client:
        bad_name = await client.post("/api/projects", json={"name": "   "})
        assert bad_name.status_code == 422
        oversized = await client.post("/api/projects", json={"name": "x" * 121})
        assert oversized.status_code == 422
        # Extra owner fields are ignored by Pydantic; ownership always comes from auth.
        response = await client.post(
            "/api/projects",
            json={"name": "Owner test", "user_id": str(uuid.uuid4())},
        )
        assert response.status_code == 201
        assert response.json()["user_id"] == user_id
    app.app.dependency_overrides.clear()
