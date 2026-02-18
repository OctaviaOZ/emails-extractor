from sqlmodel import Session, select
from app.models import JobApplication, Interview, Assessment, Offer, ApplicationStatus
import logging

logger = logging.getLogger(__name__)

def migrate_milestones_to_tables(session: Session):
    """
    One-time migration to ensure old boolean flags are represented in the new detailed tables.
    This avoids the need for a full Gmail resync.
    """
    logger.info("Checking for milestones to migrate...")
    
    # 1. Migrate Interviews
    # Find apps with the flag but no entry in the Interview table
    stmt_i = select(JobApplication).where(JobApplication.reached_interview)
    apps_i = session.exec(stmt_i).all()
    count_i = 0
    for app in apps_i:
        exists = session.exec(select(Interview).where(Interview.application_id == app.id)).first()
        if not exists:
            new_i = Interview(
                application_id=app.id,
                interview_date=app.last_updated,
                notes="Auto-migrated from legacy milestone flag"
            )
            session.add(new_i)
            count_i += 1
            
    # 2. Migrate Assessments
    stmt_a = select(JobApplication).where(JobApplication.reached_assessment)
    apps_a = session.exec(stmt_a).all()
    count_a = 0
    for app in apps_a:
        exists = session.exec(select(Assessment).where(Assessment.application_id == app.id)).first()
        if not exists:
            new_a = Assessment(
                application_id=app.id,
                due_date=app.last_updated,
                notes="Auto-migrated from legacy milestone flag"
            )
            session.add(new_a)
            count_a += 1

    # 3. Migrate Offers
    stmt_o = select(JobApplication).where(JobApplication.status == ApplicationStatus.OFFER)
    apps_o = session.exec(stmt_o).all()
    count_o = 0
    for app in apps_o:
        exists = session.exec(select(Offer).where(Offer.application_id == app.id)).first()
        if not exists:
            new_o = Offer(
                application_id=app.id,
                offer_date=app.last_updated,
                notes="Auto-migrated from legacy application status"
            )
            session.add(new_o)
            count_o += 1

    if count_i > 0 or count_a > 0 or count_o > 0:
        session.commit()
        logger.info(f"Migration complete: {count_i} interviews, {count_a} assessments, {count_o} offers migrated.")
    else:
        logger.info("No new milestones to migrate.")
