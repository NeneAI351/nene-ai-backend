"""Public terms plus authenticated, one-time account setup acceptance."""
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, field_validator
import database
from auth import get_authenticated_user_id
from account_agreement_store import TERMS, TERMS_VERSION, TERMS_SHA256, record_agreement

router=APIRouter(prefix="/api/account",tags=["account"])

class AccountAgreement(BaseModel):
    model_config=ConfigDict(extra="forbid")
    accepted: Literal[True]
    terms_version: Literal["2026-10-23-account-v1"]

    @field_validator("accepted",mode="before")
    @classmethod
    def explicit_acceptance(cls,value):
        if value is not True:raise ValueError("Explicit acceptance is required.")
        return value

@router.get("/terms")
async def read_terms():
    return {"version":TERMS_VERSION,"sha256":TERMS_SHA256,"sections":TERMS}

@router.get("/agreement")
async def agreement_status(user_id: str=Depends(get_authenticated_user_id)):
    if database._pool is None:raise HTTPException(503,"Account setup storage is unavailable.")
    row=await database.fetch_one("SELECT terms_version,accepted_at FROM account_agreements WHERE user_id=%s",(user_id,))
    return {"accepted":row is not None,"agreement":row}

@router.post("/agreement")
async def complete_account_setup(body: AccountAgreement,user_id: str=Depends(get_authenticated_user_id)):
    if database._pool is None:raise HTTPException(503,"Account setup storage is unavailable.")
    return {"accepted":True,"agreement":await record_agreement(user_id)}
