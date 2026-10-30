"""Private account profiles and durable, duplicate-safe support intake."""
import base64
import hashlib
import json
import re
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
import database
from auth import get_authenticated_user_id
from account_agreement_store import require_account_agreement
from media_storage import validate_image_bytes

router = APIRouter(prefix='/api/account', tags=['account'])


class BrandBrief(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(default='', max_length=100)
    colors: str = Field(default='', max_length=150)
    font: str = Field(default='', max_length=100)
    rules: str = Field(default='', max_length=3000)


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    photo: str | None = Field(default=None, max_length=550000)
    brand: BrandBrief | None = None

    @field_validator('photo')
    @classmethod
    def private_image_only(cls, value):
        if value is None or value == '':
            return value
        match = re.fullmatch(r'data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=]+)', value)
        if not match:
            raise ValueError('Only embedded PNG, JPEG or WebP photos are allowed.')
        try:
            raw = base64.b64decode(match[2], validate=True)
            mime, _ = validate_image_bytes(raw)
            if len(raw) > 400 * 1024 or mime != match[1]:
                raise ValueError('Photo size or type is invalid.')
        except Exception as error:
            raise ValueError('Photo size or type is invalid.') from error
        return value

    @model_validator(mode='after')
    def nonempty_patch(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError('Provide at least one non-null profile field.')
        return self


class SupportReport(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    request_id: uuid.UUID
    kind: Literal['rating', 'problem', 'suggestion', 'abuse']
    message: str = Field(min_length=1, max_length=5000)
    rating: int | None = Field(default=None, ge=1, le=5, strict=True)

    @model_validator(mode='after')
    def rating_required(self):
        if self.kind == 'rating' and self.rating is None:
            raise ValueError('Choose a rating from 1 to 5.')
        if self.kind != 'rating' and self.rating is not None:
            raise ValueError('A rating is valid only for rating reports.')
        return self


async def initialize_profile_schema():
    if database._pool is None:
        return
    await database.execute('''
        CREATE TABLE IF NOT EXISTS account_profiles (
            user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            data JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS support_reports (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            request_id UUID NOT NULL,
            fingerprint TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('rating','problem','suggestion','abuse')),
            message TEXT NOT NULL,
            rating INTEGER CHECK(rating BETWEEN 1 AND 5),
            status TEXT NOT NULL DEFAULT 'received' CHECK(status IN ('received','reviewing','closed')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(user_id,request_id)
        );
        CREATE INDEX IF NOT EXISTS idx_support_reports_owner_created ON support_reports(user_id,created_at DESC);
        ALTER TABLE account_profiles ENABLE ROW LEVEL SECURITY;
        ALTER TABLE support_reports ENABLE ROW LEVEL SECURITY;
        REVOKE ALL ON account_profiles,support_reports FROM PUBLIC;
    ''')


def require_database():
    if database._pool is None:
        raise HTTPException(503, 'Account storage is temporarily unavailable.')


@router.get('/profile')
async def get_profile(user_id: str = Depends(get_authenticated_user_id)):
    require_database()
    row = await database.fetch_one('SELECT data,updated_at FROM account_profiles WHERE user_id=%s', (user_id,))
    return {'profile': row['data'] if row else {}, 'updated_at': row['updated_at'] if row else None}


@router.patch('/profile')
async def update_profile(body: ProfileUpdate, user_id: str = Depends(get_authenticated_user_id)):
    require_database()
    patch = body.model_dump(exclude_unset=True)
    async with database._pool.connection() as conn:
        await require_account_agreement(conn, user_id)
        # Merge only validated fields. Never accept ownership or wallet fields.
        row = await (await conn.execute('''INSERT INTO account_profiles(user_id,data)
            VALUES(%s,%s::jsonb) ON CONFLICT(user_id) DO UPDATE
            SET data=account_profiles.data || EXCLUDED.data,updated_at=NOW()
            RETURNING data,updated_at''', (user_id,json.dumps(patch)))).fetchone()
    return {'profile': row['data'], 'updated_at': row['updated_at']}


@router.get('/reports')
async def get_reports(user_id: str = Depends(get_authenticated_user_id)):
    require_database()
    rows = await database.fetch_all('''SELECT id,request_id,kind,message,rating,status,created_at
        FROM support_reports WHERE user_id=%s ORDER BY created_at DESC,id DESC LIMIT 50''', (user_id,))
    return {'reports': rows, 'staff_notification_enabled': False}


@router.post('/reports')
async def create_report(body: SupportReport, user_id: str = Depends(get_authenticated_user_id)):
    require_database()
    fingerprint = hashlib.sha256(json.dumps(body.model_dump(mode='json',exclude={'request_id'}),sort_keys=True).encode()).hexdigest()
    async with database._pool.connection() as conn:
        await require_account_agreement(conn, user_id)
        # Shared transaction lock also makes the daily limit safe under concurrency.
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ('support:'+user_id,))
        old = await (await conn.execute('SELECT id,fingerprint,status,created_at FROM support_reports WHERE user_id=%s AND request_id=%s', (user_id,str(body.request_id)))).fetchone()
        if old:
            if old['fingerprint'] != fingerprint:
                raise HTTPException(409, 'This request ID belongs to a different report.')
            return {'report': {k:v for k,v in old.items() if k!='fingerprint'}, 'repeated': True, 'staff_notification_enabled': False}
        count = await (await conn.execute("SELECT count(*) AS n FROM support_reports WHERE user_id=%s AND created_at>NOW()-INTERVAL '24 hours'", (user_id,))).fetchone()
        if count['n'] >= 20:
            raise HTTPException(429, 'Report limit reached. Try again later.', headers={'Retry-After':'3600'})
        row = await (await conn.execute('''INSERT INTO support_reports(id,user_id,request_id,fingerprint,kind,message,rating)
            VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id,status,created_at''', (str(uuid.uuid4()),user_id,str(body.request_id),fingerprint,body.kind,body.message,body.rating))).fetchone()
    return {'report': row, 'repeated': False, 'staff_notification_enabled': False}
