import os,redis
from fastapi import APIRouter,Depends
from sqlalchemy import select
from .db import db_session
from .auth import current,writers,admin
from .schemas import ProofInput
from .production_models import PressHandover
from .production_schemas import HandoverInput,WithdrawInput,OccupancyConfirm
from .services import pack
from . import handover_service
router=APIRouter();queue=redis.Redis.from_url(os.environ['REDIS_URL'],decode_responses=True)
@router.get('/occupancies')
def occupancies(s=Depends(db_session),u=Depends(current)):return handover_service.machine_views(s)
@router.post('/press-machines/{key}/occupancy')
def confirm(key:int,b:OccupancyConfirm,s=Depends(db_session),u=Depends(admin)):return pack(handover_service.confirm(s,key,b,u))
@router.get('/handovers')
def handovers(s=Depends(db_session),u=Depends(current)):return [handover_service.view(s,x) for x in s.scalars(select(PressHandover).order_by(PressHandover.id.desc()))]
@router.post('/handovers')
def initiate(b:HandoverInput,s=Depends(db_session),u=Depends(admin)):return handover_service.view(s,handover_service.initiate(s,b,u))
@router.post('/handovers/{key}/first-proof')
def first(key:int,p:ProofInput,s=Depends(db_session),u=Depends(writers)):return handover_service.view(s,handover_service.first_proof(s,key,p,u,queue))
@router.post('/handovers/{key}/complete')
def complete(key:int,s=Depends(db_session),u=Depends(admin)):return handover_service.view(s,handover_service.complete(s,key,u))
@router.post('/handovers/{key}/withdraw')
def withdraw(key:int,b:WithdrawInput,s=Depends(db_session),u=Depends(admin)):return handover_service.view(s,handover_service.withdraw(s,key,b,u))
