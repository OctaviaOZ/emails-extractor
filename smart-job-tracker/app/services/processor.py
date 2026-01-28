from sqlmodel import Session, select
from datetime import datetime
from app.models import JobApplication, ApplicationEvent, ApplicationStatus
from app.services.extractor import ApplicationData
import logging

logger = logging.getLogger(__name__)

class ApplicationProcessor:
    def __init__(self, session: Session):
        self.session = session

    def process_extraction(self, data: ApplicationData, email_meta: dict):
        """
        Main logic: Link email -> Application.
        """
        # 1. Fetch active applications for this company
        existing_apps = self.session.exec(
            select(JobApplication)
            .where(JobApplication.company_name == data.company_name)
            .where(JobApplication.is_active == True)
            .order_by(JobApplication.last_updated.desc())
        ).all()

        selected_app = None

        # 2. Logic: Is this a new process?
        if not existing_apps:
            # No active apps -> Create new
            selected_app = None
        elif data.status == ApplicationStatus.APPLIED:
            # "Applied" usually signals a new start, unless we have a very recent active app (e.g. < 7 days)
            recent = next((app for app in existing_apps if (datetime.utcnow() - app.last_updated).days < 7), None)
            selected_app = recent # If recent exists, merge. If not, selected_app is None (New).
        else:
            # Ongoing status (Interview, Offer) -> attach to most recent active app
            # (Refinement: Could use fuzzy matching on 'data.position' if available)
            selected_app = existing_apps[0]

        # 3. Execute DB Action
        if selected_app:
            self._update_application(selected_app, data, email_meta)
        else:
            self._create_application(data, email_meta)

    def _create_application(self, data: ApplicationData, meta: dict):
        new_app = JobApplication(
            company_name=data.company_name,
            position=data.position or "Unknown Position",
            status=data.status,
            is_active=not data.is_rejection,
            created_at=datetime.utcnow(),
            last_updated=datetime.utcnow()
        )
        self.session.add(new_app)
        self.session.commit()
        self.session.refresh(new_app)
        
        self._log_event(new_app.id, None, data.status, data.summary, meta['subject'])
        logger.info(f"🆕 New Application: {data.company_name}")

    def _update_application(self, app: JobApplication, data: ApplicationData, meta: dict):
        old_status = app.status
        
        # Update main record
        app.status = data.status # Always take latest status
        app.last_updated = datetime.utcnow()
        if data.is_rejection:
            app.is_active = False # Close the process
        if data.position and app.position == "Unknown Position":
            app.position = data.position
            
        self.session.add(app)
        self.session.commit()

        # Add entry to history timeline
        self._log_event(app.id, old_status, data.status, data.summary, meta['subject'])
        logger.info(f"🔄 Updated Application: {app.company_name} ({old_status} -> {data.status})")

    def _log_event(self, app_id, old_s, new_s, summary, subject):
        event = ApplicationEvent(
            application_id=app_id,
            old_status=old_s,
            new_status=new_s,
            summary=summary,
            email_subject=subject,
            event_date=datetime.utcnow()
        )
        self.session.add(event)
        self.session.commit()