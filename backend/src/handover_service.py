from datetime import date
from sqlalchemy import select,or_
from fastapi import HTTPException
from .models import PrintJob,ProofSheet,Adjustment
from .production_models import PlateSet,PressHold,MachineOccupancy,PressHandover
from .dispatch_models import PressMachine,PressInspection
from .services import get,audit,now
from .plate_service import require_ready

def occupancy(s,machine_id):
    return s.scalar(select(MachineOccupancy).where(MachineOccupancy.machine_id==machine_id))
def active_handover(s,machine_id):
    return s.scalar(select(PressHandover).where(PressHandover.machine_id==machine_id,PressHandover.state=='initiated').order_by(PressHandover.id.desc()))
def job_machine(s,job_id):
    row=s.scalar(select(PressInspection).where(PressInspection.job_id==job_id,PressInspection.state=='approved').order_by(PressInspection.id.desc()))
    return row.machine_id if row else None
def candidates(s,machine_id):
    out=[]
    for j in s.scalars(select(PrintJob).where(PrintJob.status.in_(['adjusting','production','hold']))):
        if job_machine(s,j.id)==machine_id:out.append(j.id)
    return out

def guard_not_suspended(s,job_id):
    # 交接发起后旧单暂停取样、调机与签印；已受理判读继续留证但不放行
    h=s.scalar(select(PressHandover.id).where(PressHandover.from_job_id==job_id,PressHandover.state=='initiated'))
    if h:raise HTTPException(409,'主管已发起印机交接，本单暂停取样、调机与签印；已受理判读仅留证不放行')

def is_handover_target(s,job_id):
    # 进行中的交接新单视同重新上机：放行量产冻结，允许新首张与调机复验
    return bool(s.scalar(select(PressHandover.id).where(PressHandover.to_job_id==job_id,PressHandover.state=='initiated')))

def target_handover(s,job_id):
    return s.scalar(select(PressHandover).where(PressHandover.to_job_id==job_id,PressHandover.state=='initiated').order_by(PressHandover.id.desc()))

def guard_occupancy(s,job,machine_id,allow_target=True):
    # 抽检与签印的独占守卫：任何时刻一台印机只允许一张单占机生产
    h=active_handover(s,machine_id)
    if h:
        if h.from_job_id==job.id:raise HTTPException(409,'交接进行中，旧单暂停取样、调机与签印')
        if h.to_job_id==job.id:
            if allow_target:return
            raise HTTPException(409,'交接完成前，新单暂不能量产抽检')
        raise HTTPException(409,'印机正在办理交接，其他工单暂不能占机生产')
    occ=occupancy(s,machine_id)
    if occ:
        if occ.job_id!=job.id:
            other=get(s,PrintJob,occ.job_id)
            raise HTTPException(409,f'印机由 {other.code} 占机，须主管办理交接后才能生产')
        return
    others=[x for x in candidates(s,machine_id) if x!=job.id]
    if others:raise HTTPException(409,'印机占机归属不明，已阻断，待主管确认占机单')
    s.add(MachineOccupancy(machine_id=machine_id,job_id=job.id,source='auto',acquired_at=now(),updated_at=now()))

def blockers(s,h):
    out=[]
    if s.scalar(select(PressHold.id).where(PressHold.job_id==h.to_job_id,PressHold.state=='active')):out.append('新单有活动停机，须先复验闭环')
    insp=s.scalar(select(PressInspection).where(PressInspection.job_id==h.to_job_id).order_by(PressInspection.id.desc()))
    if not insp or insp.id<=(h.baseline_inspection_id or 0):out.append('新单须重新登记今日开机点检')
    elif insp.machine_id!=h.machine_id:out.append('新单最新点检不属本印机，须重新点检')
    elif insp.state!='approved' or insp.inspection_date!=date.today():out.append('新单今日点检待主管认可')
    machine=s.get(PressMachine,h.machine_id)
    if machine and machine.state!='ready':out.append('印机保养中，待恢复就绪')
    plate=s.scalar(select(PlateSet).where(PlateSet.job_id==h.to_job_id).order_by(PlateSet.id.desc()))
    if not plate or plate.state!='ready':out.append('新单印版尚未主管认可就绪')
    if not h.first_proof_id:out.append('待登记新首张色标并判读')
    else:
        p=s.get(ProofSheet,h.first_proof_id)
        if not p:out.append('新首张记录缺失')
        elif p.classification_state in ('unmeasured','classifying'):out.append('新首张判读中，等待worker')
        elif p.classification_state=='failed':out.append('新首张判读失败，须重新登记首张')
        elif not (p.pass_flag and p.defect=='合格'):
            adj=s.scalar(select(Adjustment).where(Adjustment.job_id==h.to_job_id,Adjustment.proof_id==h.first_proof_id))
            if not adj:out.append(f'新首张判为{p.defect}，待生成调机指令')
            elif adj.status=='issued':out.append('调机指令待登记执行')
            elif adj.status=='executed':out.append('待同工单新色标复验合格并签印')
    return out

def view(s,h):
    row={c.name:getattr(h,c.name)for c in h.__table__.columns}
    row['blockers']=blockers(s,h) if h.state=='initiated' else []
    return row

def initiate(s,b,u):
    machine=s.scalar(select(PressMachine).where(PressMachine.id==b.machine_id).with_for_update())
    if not machine:raise HTTPException(404,'印机不存在')
    if machine.state!='ready':raise HTTPException(409,'印机保养中，不能发起交接')
    if active_handover(s,machine.id):raise HTTPException(409,'本印机已有进行中的交接，请先完成或撤回')
    to=get(s,PrintJob,b.to_job_id)
    if s.scalar(select(PressHold.id).where(PressHold.job_id==to.id,PressHold.state=='active')):raise HTTPException(409,'新单有活动停机单，须先复验闭环')
    occ=occupancy(s,machine.id)
    if occ and occ.job_id==to.id:raise HTTPException(409,'新单已是本机占机单，无需交接')
    if s.scalar(select(MachineOccupancy).where(MachineOccupancy.job_id==to.id)):raise HTTPException(409,'新单已在其他印机占机，不能重复占机')
    if occ:
        from_id=occ.job_id
        if b.from_job_id and b.from_job_id!=from_id:raise HTTPException(409,'指定旧单与当前占机记录不符')
    elif b.from_job_id:
        from_id=b.from_job_id;get(s,PrintJob,from_id)
        s.add(MachineOccupancy(machine_id=machine.id,job_id=from_id,source='confirmed',acquired_at=now(),updated_at=now()))
    else:
        pool=candidates(s,machine.id)
        if len(pool)>1:raise HTTPException(409,'既有占机归属不明，已阻断；请主管确认占机单或发起时指定旧单')
        from_id=pool[0] if pool else None
        if from_id:s.add(MachineOccupancy(machine_id=machine.id,job_id=from_id,source='auto',acquired_at=now(),updated_at=now()))
    if from_id==to.id:raise HTTPException(409,'新旧开机单相同，无需交接')
    old=get(s,PrintJob,from_id) if from_id else None
    insp=s.scalar(select(PressInspection).where(PressInspection.job_id==to.id).order_by(PressInspection.id.desc()))
    h=PressHandover(machine_id=machine.id,from_job_id=from_id,to_job_id=to.id,note=b.note.strip(),initiated_by=u.email,initiated_at=now(),old_job_status=old.status if old else '',baseline_inspection_id=insp.id if insp else 0)
    s.add(h);audit(s,u,f'发起印机交接 {machine.code} '+(old.code if old else '空闲')+' → '+to.code);s.commit();return h

def first_proof(s,key,p,u,r):
    h=get(s,PressHandover,key)
    if h.state!='initiated':raise HTTPException(409,'交接已结束，不能登记新首张')
    to=get(s,PrintJob,h.to_job_id)
    if h.first_proof_id:
        old=get(s,ProofSheet,h.first_proof_id)
        if old.classification_state=='classifying':raise HTTPException(409,'上一张新首张仍在判读，请等待结果')
        if old.classification_state!='failed':raise HTTPException(409,'新首张已判读；缺陷走调机复验，合格请主管签印完成交接')
    if p.job_id!=to.id:raise HTTPException(409,'新首张必须属于交接新单')
    if s.scalar(select(ProofSheet).where(ProofSheet.code==p.code)):raise HTTPException(409,'色标编号已存在，新首张须用新编号')
    proof=ProofSheet(**p.model_dump(),classification_state='classifying');s.add(proof);s.flush()
    h.first_proof_id=proof.id
    audit(s,u,f'交接新首张 {proof.code} 工单{to.code}')
    from .sampling_service import queue
    queue(s,proof,r);return h

def complete(s,key,u):
    h=s.scalar(select(PressHandover).where(PressHandover.id==key).with_for_update())
    if not h:raise HTTPException(404,'交接不存在')
    if h.state!='initiated':raise HTTPException(409,'交接已完结或撤回，占用不会重复转移')
    machine=s.scalar(select(PressMachine).where(PressMachine.id==h.machine_id).with_for_update())
    if machine.state!='ready':raise HTTPException(409,'印机保养中，不能完成交接')
    job=s.scalar(select(PrintJob).where(PrintJob.id==h.to_job_id).with_for_update())
    insp=s.scalar(select(PressInspection).where(PressInspection.job_id==job.id).order_by(PressInspection.id.desc()))
    if not insp or insp.id<=(h.baseline_inspection_id or 0) or insp.machine_id!=h.machine_id or insp.state!='approved' or insp.inspection_date!=date.today():
        raise HTTPException(409,'新单须在本印机重新完成今日开机点检并获主管认可')
    require_ready(s,job.id)
    if not h.first_proof_id:raise HTTPException(409,'新单尚未登记新首张')
    p=get(s,ProofSheet,h.first_proof_id)
    if p.classification_state!='classified':raise HTTPException(409,'新首张尚未完成判读')
    if p.pass_flag and p.defect=='合格':
        if job.status not in ('setup','adjusting','production'):raise HTTPException(409,'新单当前状态不允许签印量产')
        job.status='production'
    else:
        adj=s.scalar(select(Adjustment).where(Adjustment.job_id==job.id,Adjustment.proof_id==p.id,Adjustment.status=='signed'))
        if not adj or job.status!='production':raise HTTPException(409,'新首张缺陷须完成调机复验并签印合格')
    occ=occupancy(s,h.machine_id)
    if occ:occ.job_id=job.id;occ.source='handover';occ.updated_at=now()
    else:s.add(MachineOccupancy(machine_id=h.machine_id,job_id=job.id,source='handover',acquired_at=now(),updated_at=now()))
    h.state='completed';h.completed_by=u.email;h.completed_at=now()
    audit(s,u,f'主管签印完成交接 {machine.code} → {job.code}');s.commit();return h

def withdraw(s,key,b,u):
    h=s.scalar(select(PressHandover).where(PressHandover.id==key).with_for_update())
    if not h:raise HTTPException(404,'交接不存在')
    if h.state!='initiated':raise HTTPException(409,'交接已完结或撤回，不能重复撤回')
    s.scalar(select(PressMachine).where(PressMachine.id==h.machine_id).with_for_update())
    defect=False
    if h.from_job_id:
        old=get(s,PrintJob,h.from_job_id)
        new_defect=s.scalar(select(ProofSheet.id).where(ProofSheet.job_id==old.id,ProofSheet.classified_at>h.initiated_at,or_(ProofSheet.pass_flag==False,ProofSheet.defect!='合格')).limit(1))
        hold=s.scalar(select(PressHold.id).where(PressHold.job_id==old.id,PressHold.state=='active').limit(1))
        defect=bool(new_defect or hold)
        if defect and old.status=='production':old.status='hold'
        h.old_job_defect=defect
    h.state='withdrawn';h.withdrawn_by=u.email;h.withdrawn_at=now();h.withdraw_note=b.note.strip()
    audit(s,u,f'撤回交接 #{key} '+('旧单有新缺陷保持停机' if defect else '旧单恢复原状态'));s.commit();return h

def confirm(s,machine_id,b,u):
    machine=s.scalar(select(PressMachine).where(PressMachine.id==machine_id).with_for_update())
    if not machine:raise HTTPException(404,'印机不存在')
    if active_handover(s,machine_id):raise HTTPException(409,'交接进行中，占用由交接管理，请先完成或撤回')
    job=get(s,PrintJob,b.job_id)
    occ=occupancy(s,machine_id)
    if occ:
        if occ.job_id==job.id:raise HTTPException(409,'该单已是本机占机单，无需确认')
        occ.job_id=job.id;occ.source='confirmed';occ.updated_at=now()
    else:s.add(MachineOccupancy(machine_id=machine_id,job_id=job.id,source='confirmed',acquired_at=now(),updated_at=now()))
    audit(s,u,f'主管确认占机 {machine.code} → {job.code}');s.commit();return occupancy(s,machine_id)

def machine_views(s):
    out=[]
    for m in s.scalars(select(PressMachine).order_by(PressMachine.id)):
        occ=occupancy(s,m.id);h=active_handover(s,m.id)
        row={'machine_id':m.id,'machine_code':m.code,'machine_state':m.state,'job_id':occ.job_id if occ else None,'source':occ.source if occ else None,'handover_id':h.id if h else None,'ambiguous':False,'candidates':[]}
        if not occ:
            pool=candidates(s,m.id)
            row['candidates']=pool
            if len(pool)>1:row['ambiguous']=True
        out.append(row)
    return out
