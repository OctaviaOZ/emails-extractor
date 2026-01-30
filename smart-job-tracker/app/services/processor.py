from sqlmodel import Session, select
from datetime import datetime
import re
from app.models import JobApplication, ApplicationEvent, ApplicationStatus
from app.services.extractor import ApplicationData
import logging

logger = logging.getLogger(__name__)

class ApplicationProcessor:
    def __init__(self, session: Session):
        self.session = session

    def _normalize_company(self, name: str) -> str:
        """
        Normalize company name for fuzzy matching.
        Removes legal suffixes (GmbH, AG, Inc, etc.) and locations (Germany, Berlin, etc.).
        """
        if not name:
            return ""
        name = name.lower()
        # Common suffixes to strip
        suffixes = [
            r'\bgmbh\b', r'\bag\b', r'\binc\b', r'\bltd\b', r'\bco\b', r'\bkg\b', r'\bplc\b',
            r'\bse\b', r'\bcorp\b', r'\bcorporation\b', r'\bholding\b', r'\bgroup\b',
            r'\bgermany\b', r'\bdeutschland\b', r'\bberlin\b', r'\beurope\b', r'\bemea\b',
            r'\binternational\b', r'\bsolutions\b', r'\bsystems\b', r'\btechnology\b', r'\btechnologies\b'
        ]
        for suffix in suffixes:
            name = re.sub(suffix, '', name)
        
        # Remove special chars and extra whitespace
        name = re.sub(r'[^\w\s]', '', name)
        return name.strip()

    def _find_existing_application(self, data: ApplicationData, email_meta: dict) -> tuple[JobApplication | None, bool]:
        """
        Tries to find an existing application matching the company name.
        Returns (Application, is_active_match).
        Prioritizes Active matches.
        """
        # 1. Get all apps
        all_apps = self.session.exec(select(JobApplication)).all()
        
        norm_new_name = self._normalize_company(data.company_name)
        sender_domain = ""
        if email_meta.get('sender_email') and '@' in email_meta['sender_email']:
            sender_domain = email_meta['sender_email'].split('@')[1].lower()

        best_match = None
        
        # Helper to check match
        def is_match(app):
            # 1. Exact or Normalized Name Match
            norm_app_name = self._normalize_company(app.company_name)
            if norm_new_name and norm_app_name and norm_new_name == norm_app_name:
                return True
            # 2. Domain Match (if available in DB - currently strict sender_email check isn't stored as domain,
            # but we can check sender_email if populated)
            if app.sender_email and sender_domain:
                app_domain = app.sender_email.split('@')[1].lower() if '@' in app.sender_email else ""
                # Ignore generic domains
                if app_domain and app_domain not in ['gmail.com', 'yahoo.com', 'outlook.com', 'hotmail.com']:
                    if app_domain == sender_domain:
                        return True
            return False

        # First pass: Look for ACTIVE matches
        for app in all_apps:
            if app.is_active and is_match(app):
                return app, True # Found active match, return immediately
        
        # Second pass: Look for INACTIVE matches (most recent)
        # Sort by last_updated desc
        all_apps.sort(key=lambda x: x.last_updated, reverse=True)
        for app in all_apps:
            if not app.is_active and is_match(app):
                return app, False # Found inactive match

        return None, False

    def process_extraction(self, data: ApplicationData, email_meta: dict, email_timestamp: datetime):
        """
        Main logic: Link email -> Application.
        """
        existing_app, is_app_active = self._find_existing_application(data, email_meta)
        
        # Decision Logic
        if not existing_app:
            # Case 1: New Company entirely -> Create New
            self._create_application(data, email_meta, email_timestamp)
            return

        if is_app_active:
            # Case 2: Active Application Exists
            # "Applied" can only happen once. If we see it again, treat as Communication.
            # "Unknown" after Applied is Communication.
            new_status = data.status
            
            if new_status == ApplicationStatus.APPLIED:
                # Already active, don't restart status loop. Treat as confirmation/comm.
                new_status = ApplicationStatus.COMMUNICATION
                if data.summary == "No summary extracted":
                    data.summary = "Application confirmation/update"
            elif new_status == ApplicationStatus.UNKNOWN:
                new_status = ApplicationStatus.COMMUNICATION

            # Check if we should update the DB status
            # Only update if the new status is "meaningful" or advances the process
            # But if it's COMMUNICATION, we just log it and update timestamps, usually keeping the old MAIN status?
            # Actually, user wants "after applied... it's all communication".
            # But if it's INTERVIEW, we should update.
            # So: Update if status != COMMUNICATION and != APPLIED (handled above)
            
            # Refined: If new_status is COMMUNICATION, we generally keep the OLD status (e.g. keep "Applied" or "Interview")
            # unless the old status was Unknown.
            
            final_status_for_db = existing_app.status
            if new_status not in [ApplicationStatus.COMMUNICATION, ApplicationStatus.UNKNOWN]:
                final_status_for_db = new_status
            
            # Update the app
            self._update_application(existing_app, data, email_meta, email_timestamp, override_status=final_status_for_db)

        else:
            # Case 3: Inactive (Rejected) Application Exists
            # User: "After absage it can be communication more and application as well"
            if data.status == ApplicationStatus.APPLIED:
                # Re-application -> Create NEW active application
                self._create_application(data, email_meta, email_timestamp)
            elif data.status in [ApplicationStatus.INTERVIEW, ApplicationStatus.ASSESSMENT, ApplicationStatus.OFFER]:
                # Strong signal -> Start NEW active application (Revival?)
                self._create_application(data, email_meta, email_timestamp)
            else:
                # Communication/Unknown/Rejected -> Append to the inactive log (don't re-activate)
                # Just log the event to history, update last_updated, keep inactive.
                self._update_application(existing_app, data, email_meta, email_timestamp, override_status=existing_app.status)


    def _create_application(self, data: ApplicationData, meta: dict, timestamp: datetime):
        new_app = JobApplication(
            company_name=data.company_name,
            position=data.position or "Unknown Position",
            status=data.status if data.status != ApplicationStatus.UNKNOWN else ApplicationStatus.APPLIED, # Default to Applied if unknown at start
            is_active=not data.is_rejection,
            created_at=timestamp,
            last_updated=timestamp,
            # Required fields
            email_id=meta.get('id'),
            email_subject=meta.get('subject', 'No Subject'),
            email_snippet=meta.get('snippet'),
            summary=data.summary,
            sender_name=meta.get('sender_name'),
            sender_email=meta.get('sender_email'),
            year=meta.get('year', timestamp.year),
            month=meta.get('month', timestamp.month),
            day=meta.get('day', timestamp.day)
        )
        self.session.add(new_app)
        self.session.commit()
        self.session.refresh(new_app)
        
        self._log_event(new_app.id, None, new_app.status, data.summary, meta.get('subject', 'No Subject'), timestamp)
        logger.info(f"🆕 New Application: {data.company_name}")

    def _update_application(self, app: JobApplication, data: ApplicationData, meta: dict, timestamp: datetime, override_status=None):
        old_status = app.status
        new_status = override_status if override_status else data.status
        
        # Update main record
        app.status = new_status
        
        # Only update last_updated if this email is actually newer than what we have
        # Or if it's an important status change even if dates are weird? No, trust timestamp.
        if timestamp >= app.last_updated:
            app.last_updated = timestamp
            # Update latest email info
            app.email_id = meta.get('id', app.email_id)
            app.email_subject = meta.get('subject', app.email_subject)
            app.email_snippet = meta.get('snippet', app.email_snippet)
            app.summary = data.summary
            app.sender_name = meta.get('sender_name', app.sender_name)
            app.sender_email = meta.get('sender_email', app.sender_email)
            
        if data.is_rejection:
            app.is_active = False # Close the process
        
        # Update position if we found a better one and didn't have one
        if data.position and (app.position == "Unknown Position" or not app.position):
            app.position = data.position
            
        self.session.add(app)
        self.session.commit()

        # Add entry to history timeline
        # Use data.status for the event log to record what THIS specific email was,
        # even if it didn't change the main app status (e.g. Communication).
        event_status = data.status 
        if event_status == ApplicationStatus.UNKNOWN:
            event_status = ApplicationStatus.COMMUNICATION

        self._log_event(app.id, old_status, event_status, data.summary, meta.get('subject', 'No Subject'), timestamp)
        logger.info(f"🔄 Updated Application: {app.company_name} ({old_status} -> {new_status})")

    def _log_event(self, app_id, old_s, new_s, summary, subject, timestamp):
        event = ApplicationEvent(
            application_id=app_id,
            old_status=old_s,
            new_status=new_s,
            summary=summary,
            email_subject=subject,
            event_date=timestamp
        )
        self.session.add(event)
        self.session.commit()