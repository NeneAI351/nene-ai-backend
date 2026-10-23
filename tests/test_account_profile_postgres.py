import asyncio
import os
import uuid
import pytest
import pytest_asyncio
from fastapi import HTTPException
import database
import account_profile as profile
from account_agreement_store import initialize_account_agreement_schema,record_agreement

@pytest_asyncio.fixture
async def profile_db():
    if not os.getenv('DATABASE_URL'):pytest.skip('Disposable PostgreSQL required')
    await database.start_database();await database.initialize_schema()
    await initialize_account_agreement_schema();await profile.initialize_profile_schema()
    users=[str(uuid.uuid4()),str(uuid.uuid4())]
    for user in users:
        await database.execute('INSERT INTO users(id) VALUES(%s)',(user,));await record_agreement(user)
    yield users
    await database.execute('DELETE FROM users WHERE id IN (%s,%s)',tuple(users))

@pytest.mark.asyncio
async def test_private_profile_partial_updates_survive_connection_reopen(profile_db):
    user,other=profile_db
    await profile.update_profile(profile.ProfileUpdate(display_name='Creator'),user)
    await profile.update_profile(profile.ProfileUpdate(brand={'name':'NENE','colors':'Gold'}),user)
    await database.close_database();await database.start_database()
    row=await profile.get_profile(user)
    assert row['profile']['display_name']=='Creator' and row['profile']['brand']['name']=='NENE'
    assert (await profile.get_profile(other))['profile']=={}
    assert (await database.fetch_one("SELECT relrowsecurity FROM pg_class WHERE relname='account_profiles'"))['relrowsecurity']

@pytest.mark.asyncio
async def test_concurrent_retries_create_one_private_report(profile_db):
    user,other=profile_db
    body=profile.SupportReport(request_id=uuid.uuid4(),kind='problem',message='Playback failed')
    results=await asyncio.gather(*(profile.create_report(body,user) for _ in range(4)))
    assert len({r['report']['id'] for r in results})==1
    assert sum(not r['repeated'] for r in results)==1
    assert len((await profile.get_reports(user))['reports'])==1
    assert (await profile.get_reports(other))['reports']==[]
    changed=profile.SupportReport(request_id=body.request_id,kind='problem',message='Different')
    with pytest.raises(HTTPException) as error:await profile.create_report(changed,user)
    assert error.value.status_code==409
    assert len((await profile.get_reports(user))['reports'])==1

@pytest.mark.asyncio
async def test_report_limit_atomic_and_retries_still_work(profile_db):
    user,_=profile_db
    first=profile.SupportReport(request_id=uuid.uuid4(),kind='suggestion',message='Add transitions')
    await profile.create_report(first,user)
    for _ in range(18):await profile.create_report(profile.SupportReport(request_id=uuid.uuid4(),kind='problem',message='Problem'),user)
    result=await asyncio.gather(*(profile.create_report(profile.SupportReport(request_id=uuid.uuid4(),kind='problem',message='Problem'),user) for _ in range(3)),return_exceptions=True)
    assert sum(isinstance(r,HTTPException) and r.status_code==429 for r in result)==2
    assert len((await profile.get_reports(user))['reports'])==20
    assert (await profile.create_report(first,user))['repeated'] is True
