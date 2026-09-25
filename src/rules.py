from __future__ import annotations
from .audit import parse_utc
from .domain import ConflictError, ValidationError
TITLE='空气污染源许可与合规检查'; ENTITY='排污许可'; ID_PREFIX='AQ'
SEVERITIES=['low', 'medium', 'high', 'critical']; STATES=['draft', 'submitted', 'inspection', 'correction', 'approved']; TRANSITIONS={'draft': ['submitted'], 'submitted': ['inspection'], 'inspection': ['correction'], 'correction': ['approved'], 'approved': []}; TRANSITION_ROLES={'submitted': ['applicant'], 'inspection': ['inspector'], 'correction': ['inspector'], 'approved': ['compliance_manager']}
CREATE_ROLES=set(['applicant']); RECORD_ROLES=set(['applicant', 'inspector']); AUDIT_ROLES=set(['compliance_manager', 'viewer']); VIEW_ROLES=set(['applicant', 'inspector', 'compliance_manager', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'medium': 3.0, 'high': 6.0, 'critical': 9.0}; DEADLINE_HOURS={'low': 72, 'medium': 24, 'high': 8, 'critical': 4}; TERMINAL_STATES=set(['approved'])
RENEWAL_ENTITY='排污许可续期'; DEFAULT_VALID_YEARS=5; MAX_VALID_YEARS=50
RENEWAL_STATES=['submitted', 'correction', 'review_passed', 'completed', 'rejected']; RENEWAL_OPEN_STATES=['submitted', 'correction', 'review_passed']; RENEWAL_TERMINAL_STATES=set(['completed', 'rejected'])
RENEWAL_CREATE_ROLES=set(['applicant']); RENEWAL_REVIEW_ROLES=set(['inspector']); RENEWAL_RESUBMIT_ROLES=set(['applicant']); RENEWAL_DECIDE_ROLES=set(['compliance_manager'])
RENEWAL_CONCLUSIONS=['pass', 'fail']; RENEWAL_DECISIONS=['reissue', 'reject']
PERMIT_LIFECYCLES=['active', 'replaced']
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
def permit_effective(item,now=None):
    from .audit import utc_now_dt
    if item.get("status") not in TERMINAL_STATES or item.get("lifecycle")!='active': return False
    valid_to=item.get("valid_to")
    if not valid_to: return False
    try: expiry=parse_utc(valid_to)
    except (TypeError,ValueError): return False
    return expiry>(now if now is not None else utc_now_dt())
def renewal_blockers(item,now):
    blockers=[]
    if item.get("status") not in TERMINAL_STATES: blockers.append("许可证尚未获批生效")
    if item.get("lifecycle")!='active': blockers.append("许可证已被换发或失效")
    valid_to=item.get("valid_to")
    if not valid_to: blockers.append("许可证缺少有效期")
    else:
        try: expiry=parse_utc(valid_to)
        except (TypeError,ValueError): blockers.append("许可证有效期无效")
        else:
            if expiry<=now: blockers.append("原证已失效，不能发起续期")
    return blockers
