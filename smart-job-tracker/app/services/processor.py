from sqlmodel import Session, select
from datetime import datetime, timedelta
from app.models import JobApplication, ApplicationEvent, ApplicationStatus
from app.services.extractor import ApplicationData
import logging

logger = logging.getLogger(__name__)

class ApplicationProcessor:
    def __init__(self, session: Session):
        self.session = session

    def process_extraction(self, data: ApplicationData, email_meta: dict):
        """
        Decides if this is a NEW application or an UPDATE to an existing one.
        """
        # 1. Find all 'Active' applications for this company
        # An application is 'Active' if it's not Rejected/Archived OR it was updated recently (e.g., < 6 months)
        existing_apps = self.session.exec(
            select(JobApplication)
            .where(JobApplication.company_name == data.company_name)
            .order_by(JobApplication.last_updated.desc())
        ).all()

        # 2. Smart Matching Logic
        selected_app = None
        
        # Heuristic A: If "Applied" status and no active app exists -> NEW
        if data.status == ApplicationStatus.APPLIED:
            # Check if we have a very recent app (e.g. duplicate email within 7 days)
            recent = next((app for app in existing_apps if (datetime.utcnow() - app.last_updated).days < 7), None)
            if recent:
                selected_app = recent
            else:
                selected_app = None # Triggers creation of new app

        # Heuristic B: If there are existing apps, try to match by Position
        elif existing_apps:
            # If the email explicitly mentions a position, match it
            if data.position:
                for app in existing_apps:
                    if app.position and self._is_fuzzy_match(app.position, data.position):
                        selected_app = app
                        break
            
            # Fallback: If no position match, assume it relates to the most recently updated active application
            if not selected_app:
                selected_app = existing_apps[0]

        # 3. Create or Update
        if selected_app:
            self._update_application(selected_app, data, email_meta)
        else:
            self._create_application(data, email_meta)

    def _create_application(self, data: ApplicationData, meta: dict):
        # Handle potential missing date fields in meta or generate them
        now = datetime.utcnow()
        year = meta.get('year', now.year)
        month = meta.get('month', now.month)
        day = meta.get('day', now.day)

        new_app = JobApplication(
            company_name=data.company_name,
            position=data.position or "Unknown Position",
            status=data.status,
            is_active=not data.is_rejection,
            last_updated=now,
            email_subject=meta.get('subject', 'No Subject'),
            year=year,
            month=month,
            day=day
        )
        self.session.add(new_app)
        self.session.commit()
        self.session.refresh(new_app)
        
        self._log_event(new_app.id, None, data.status, data.summary, meta.get('subject', ''))
        logger.info(f"Created NEW process for {data.company_name}")

    def _update_application(self, app: JobApplication, data: ApplicationData, meta: dict):
        old_status = app.status
        
        # Update main record
        app.status = data.status
        app.summary = data.summary
        app.last_updated = datetime.utcnow()
        if data.is_rejection:
            app.is_active = False
        if data.position and app.position == "Unknown Position":
            app.position = data.position
        
        # Update latest email info
        app.email_subject = meta.get('subject', app.email_subject)

        self.session.add(app)
        self._log_event(app.id, old_status, data.status, data.summary, meta.get('subject', ''))
        logger.info(f"Updated process {app.id} for {data.company_name}")

    def _log_event(self, app_id, old_s, new_s, summary, subject):
        event = ApplicationEvent(
            application_id=app_id,
            old_status=old_s,
            new_status=new_s,
            summary=summary,
            email_subject=subject
        )
        self.session.add(event)
        self.session.commit()

    def _is_fuzzy_match(self, pos1: str, pos2: str) -> bool:
        # Simple containment check, can be upgraded to Levenshtein distance
        p1, p2 = pos1.lower(), pos2.lower()
        return p1 in p2 or p2 in p1
