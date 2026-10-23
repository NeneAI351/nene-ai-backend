"""PostgreSQL-backed character ownership and project association tests."""
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
async def characters_postgres():
    if not os.getenv("DATABASE_URL"):
        import pytest
        pytest.skip("DATABASE_URL is required for character ownership integration tests.")
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
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app.app), base_url="http://testserver"
    )


async def _create_project(client, name="Project"):
    response = await client.post("/api/projects", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_character_crud_is_owner_scoped_and_project_bound(characters_postgres):
    user_a, user_b = await _create_user(), await _create_user()
    async with await _client_for(user_a) as client_a:
        project_a = await _create_project(client_a)
        created = await client_a.post("/api/characters", json={
            "name": "  Main hero ", "project_id": project_a,
            "profile": {"appearance": "blue coat", "voice": "warm"}
        })
        assert created.status_code == 201, created.text
        character = created.json()
        character_id = character["id"]
        assert character["name"] == "Main hero"
        assert character["user_id"] == user_a
        assert character["project_id"] == project_a

        # A lost response can retry the same UUID without creating another row.
        repeated = await client_a.post('/api/characters', json={
            'id':character_id, 'name':'Main hero', 'project_id':project_a,
            'profile':{'appearance':'blue coat', 'voice':'warm'},
        })
        assert repeated.status_code == 201
        assert repeated.json()['id'] == character_id
        conflict = await client_a.post('/api/characters', json={
            'id':character_id, 'name':'Different draft',
        })
        assert conflict.status_code == 409

        listed = await client_a.get("/api/characters")
        assert [item["id"] for item in listed.json()["items"]] == [character_id]
        by_project = await client_a.get("/api/characters", params={"project_id": project_a})
        assert [item["id"] for item in by_project.json()["items"]] == [character_id]
        updated = await client_a.patch(f"/api/characters/{character_id}", json={
            "name": "Main hero v2", "profile": {"appearance": "green coat"}
        })
        assert updated.status_code == 200, updated.text
        assert updated.json()["profile"] == {"appearance": "green coat"}

    async with await _client_for(user_b) as client_b:
        project_b = await _create_project(client_b, "B's project")
        assert (await client_b.get("/api/characters")).json()["items"] == []
        collision = await client_b.post('/api/characters', json={
            'id':character_id, 'name':'Main hero', 'project_id':None,
        })
        assert collision.status_code == 409
        assert (await client_b.get(f"/api/characters/{character_id}")).status_code == 404
        assert (await client_b.patch(f"/api/characters/{character_id}", json={
            "name": "Takeover"
        })).status_code == 404
        assert (await client_b.delete(f"/api/characters/{character_id}")).status_code == 404
        assert (await client_b.get("/api/characters", params={"project_id": project_a})).status_code == 404
        # User B cannot attach a character to User A's project.
        foreign_project = await client_b.post("/api/characters", json={
            "name": "Bad link", "project_id": project_a
        })
        assert foreign_project.status_code == 404
        # Nor can B move their own character into A's project.
        own_character = await client_b.post("/api/characters", json={
            "name": "B character", "project_id": project_b
        })
        assert own_character.status_code == 201
        moved = await client_b.patch(
            f"/api/characters/{own_character.json()['id']}",
            json={"project_id": project_a},
        )
        assert moved.status_code == 404

    async with await _client_for(user_a) as client_a:
        assert (await client_a.get(f"/api/characters/{character_id}")).status_code == 200
        assert (await client_a.delete(f"/api/characters/{character_id}")).status_code == 204
        assert (await client_a.get(f"/api/characters/{character_id}")).status_code == 404
    app.app.dependency_overrides.clear()


async def test_character_auth_validation_and_mass_assignment(characters_postgres):
    app.app.dependency_overrides.clear()
    async with await _client_for(None) as client:
        assert (await client.get("/api/characters")).status_code == 401
        assert (await client.post("/api/characters", json={"name": "No auth"})).status_code == 401
    user = await _create_user()
    async with await _client_for(user) as client:
        assert (await client.post("/api/characters", json={"name": "   "})).status_code == 422
        assert (await client.post("/api/characters", json={"name": "x" * 121})).status_code == 422
        assert (await client.post("/api/characters", json={
            "name": "Extra field", "user_id": str(uuid.uuid4())
        })).status_code == 422
        assert (await client.post("/api/characters", json={
            "name": "Large profile", "profile": {"payload": "x" * 17000}
        })).status_code == 422
        assert (await client.patch("/api/characters/" + str(uuid.uuid4()), json={
            "profile": None
        })).status_code == 422
    app.app.dependency_overrides.clear()

