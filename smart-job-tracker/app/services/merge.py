from sqlmodel import Session, select
from app.models import JobApplication, Interview, Assessment, Offer, ApplicationEventLog
import logging

logger = logging.getLogger(__name__)

def merge_applications(session: Session, source_id: int, target_id: int):
    """
    Merges source_application INTO target_application.
    Moves all history and related items.
    Deletes source_application.
    """
    source = session.get(JobApplication, source_id)
    target = session.get(JobApplication, target_id)
    
    if not source or not target:
        raise ValueError("One or both applications not found.")
    
    if source.id == target.id:
        raise ValueError("Cannot merge an application into itself.")

    logger.info(f"Merging App ID {source.id} ({source.company_name}) into App ID {target.id} ({target.company_name})")

    # 1. Move History (Events)
    # We need to fetch them specifically if relationships aren't loaded, 
    # but SQLAlchemy usually handles lazy loading. To be safe, we query.
    events = session.exec(select(ApplicationEventLog).where(ApplicationEventLog.application_id == source.id)).all()
    for event in events:
        event.application_id = target.id
        session.add(event)

    # 2. Move Interviews
    interviews = session.exec(select(Interview).where(Interview.application_id == source.id)).all()
    for i in interviews:
        i.application_id = target.id
        session.add(i)

    # 3. Move Assessments
    assessments = session.exec(select(Assessment).where(Assessment.application_id == source.id)).all()
    for a in assessments:
        a.application_id = target.id
        session.add(a)

    # 4. Move Offers
    offers = session.exec(select(Offer).where(Offer.application_id == source.id)).all()
    for o in offers:
        o.application_id = target.id
        session.add(o)

    # 5. Merge Notes
    if source.notes:
        merge_note = f"\n\n--- Merged Data from {source.company_name} ({source.position}) ---\n{source.notes}"
        if target.notes:
            target.notes += merge_note
        else:
            target.notes = merge_note
            
    # 6. Merge Flags
    if source.reached_interview:
        target.reached_interview = True
    if source.reached_assessment:
        target.reached_assessment = True
        
    # 7. Add a merge event log to the target
    merge_event = ApplicationEventLog(
        application_id=target.id,
        new_status=target.status,
        summary=f"Merged duplicate application '{source.company_name}' into this one.",
        email_subject="System Merge"
    )
    session.add(merge_event)
    session.add(target)

    # 8. Delete Source
    session.delete(source)
    session.commit()
    
    return True
