from sqlmodel import Session, select
from datetime import datetime
import re
from app.models import JobApplication, ApplicationEventLog, ApplicationStatus, STATUS_RANK, Company
from app.services.extractor import ApplicationData
import logging

logger = logging.getLogger(__name__)

class ApplicationProcessor:
    def __init__(self, session: Session, config: dict = None):
        self.session = session
        self.config = config or {}

    def _normalize_company(self, name: str) -> str:
        # ... (implementation same as before) ...
        if not name:
            return ""
        
        # 1. Lowercase and remove punctuation
        name = name.lower()
        name = re.sub(r'[^\w\s]', '', name)
        
        # 2. Define suffixes to strip ONLY at the end
        suffixes = [
            'gmbh', 'ag', 'inc', 'ltd', 'co', 'kg', 'plc', 'se', 'corp', 'corporation', 
            'holding', 'group', 'germany', 'deutschland', 'berlin', 'europe', 'emea', 
            'international', 'solutions', 'systems', 'technology', 'technologies'
        ]
        
        # Iteratively strip suffixes from the end
        changed = True
        while changed:
            changed = False
            name = name.strip()
            for suffix in suffixes:
                pattern = rf'\s+{suffix}$'
                if re.search(pattern, name):
                    name = re.sub(pattern, '', name)
                    changed = True
                    break
        
        return name.strip()

    def _find_existing_application(self, data: ApplicationData, email_meta: dict) -> tuple[JobApplication | None, bool]:
        """
        Tries to find an existing application matching the thread_id, company name, or domain.
        Returns (Application, is_active_match).
        """
        # 1. Try Thread ID Match (Strongest - Priority 4)
        if thread_id := email_meta.get('thread_id'):
            stmt = select(JobApplication).where(JobApplication.thread_id == thread_id)
            app = self.session.exec(stmt).first()
            if app:
                return app, app.is_active

        # 2. Fallback to Name/Domain match
        all_apps = self.session.exec(select(JobApplication)).all()
        
        new_company_name = data.company_name
        norm_new_name = self._normalize_company(new_company_name)
        
        sender_domain = ""
        if email_meta.get('sender_email') and '@' in email_meta['sender_email']:
            sender_domain = email_meta['sender_email'].split('@')[1].lower()

        # Platforms and generic domains should NEVER be used for matching
        generic_domains = {'gmail.com', 'yahoo.com', 'outlook.com', 'hotmail.com', 'icloud.com', 'me.com', 'live.com'}
        platforms = []
        if extraction_cfg := self.config.get('extraction'):
            platforms = extraction_cfg.get('platforms', [])
        
        # Check if the sender domain belongs to a platform
        is_platform_domain = any(p in sender_domain for p in platforms) or sender_domain in generic_domains

        # Helper to check match
        def is_match(app):
            norm_app_name = self._normalize_company(app.company_name)
            
            # 1. Name Match (Strongest)
            if norm_new_name and norm_app_name and norm_new_name == norm_app_name:
                return True
            
            # 2. Domain Match (Fallback - ONLY if not a platform and names aren't contradictory)
            if not is_platform_domain and app.sender_email and sender_domain:
                app_domain = app.sender_email.split('@')[1].lower() if '@' in app.sender_email else ""
                if app_domain == sender_domain and app_domain not in generic_domains:
                    if norm_new_name and norm_app_name:
                        if norm_new_name not in norm_app_name and norm_app_name not in norm_new_name:
                            return False
                    return True
            return False

        # First pass: Look for ACTIVE matches
        for app in all_apps:
            if app.is_active and is_match(app):
                return app, True
        
        # Second pass: Look for INACTIVE matches (most recent)
        all_apps.sort(key=lambda x: x.last_updated, reverse=True)
        for app in all_apps:
            if not app.is_active and is_match(app):
                return app, False

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
            raw_status = data.status
            
            # Normalize Applied/Unknown to Pending for existing apps to see if they are better than current
            if raw_status in [ApplicationStatus.APPLIED, ApplicationStatus.UNKNOWN]:
                raw_status = ApplicationStatus.PENDING
                if not data.summary or data.summary == "No summary extracted":
                    data.summary = "Application confirmation/update"

            # Use STATUS_RANK to ensure we only upgrade status
            current_rank = STATUS_RANK.get(existing_app.status, 0)
            new_rank = STATUS_RANK.get(raw_status, 0)
            
            final_status_for_db = existing_app.status
            if new_rank > current_rank:
                final_status_for_db = raw_status
            
            self._update_application(existing_app, data, email_meta, email_timestamp, override_status=final_status_for_db)

        else:
            # Case 3: Inactive (Rejected) Application Exists
            if data.status in [ApplicationStatus.APPLIED, ApplicationStatus.INTERVIEW, ApplicationStatus.ASSESSMENT, ApplicationStatus.OFFER]:
                self._create_application(data, email_meta, email_timestamp)
            else:
                self._update_application(existing_app, data, email_meta, email_timestamp, override_status=existing_app.status)


    def _get_or_create_company(self, data: ApplicationData, email_meta: dict) -> Company:
        """
        Finds or creates a Company record.
        """
        name = data.company_name
        domain = None
        if email_meta.get('sender_email') and '@' in email_meta['sender_email']:
            domain = email_meta['sender_email'].split('@')[1].lower()
            generic_domains = {'gmail.com', 'yahoo.com', 'outlook.com', 'hotmail.com', 'icloud.com', 'me.com', 'live.com'}
            if domain in generic_domains:
                domain = None

        # 1. Try by Name (Exact)
        stmt = select(Company).where(Company.name == name)
        company = self.session.exec(stmt).first()
        if company:
            if domain and not company.domain:
                company.domain = domain
                self.session.add(company)
                self.session.commit()
            return company

        # 2. Try by Domain (if not generic)
        if domain:
            stmt = select(Company).where(Company.domain == domain)
            company = self.session.exec(stmt).first()
            if company:
                return company

        # 3. Create New
        company = Company(name=name, domain=domain)
        self.session.add(company)
        self.session.commit()
        self.session.refresh(company)
        return company

    def _create_application(self, data: ApplicationData, meta: dict, timestamp: datetime):
        company = self._get_or_create_company(data, meta)
        
        new_app = JobApplication(
            company_id=company.id,
            company_name=data.company_name,
            position=data.position or "Unknown Position",
            status=data.status if data.status != ApplicationStatus.UNKNOWN else ApplicationStatus.APPLIED,
            is_active=not data.is_rejection,
            created_at=timestamp,
            last_updated=timestamp,
            email_id=meta.get('id'),
            thread_id=meta.get('thread_id'),
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
        # Ensure company linkage if it was missing
        if not app.company_id:
            company = self._get_or_create_company(data, meta)
            app.company_id = company.id
            
        old_status = app.status
        new_status = override_status if override_status else data.status
        
        app.status = new_status
        
        if timestamp >= app.last_updated:
            app.last_updated = timestamp
            app.email_id = meta.get('id', app.email_id)
            app.email_subject = meta.get('subject', app.email_subject)
            app.email_snippet = meta.get('snippet', app.email_snippet)
            app.summary = data.summary
            app.sender_name = meta.get('sender_name', app.sender_name)
            app.sender_email = meta.get('sender_email', app.sender_email)
            
        if data.is_rejection:
            app.is_active = False
        
        if data.position and (app.position == "Unknown Position" or not app.position):
            app.position = data.position
            
        self.session.add(app)
        self.session.commit()

        self._log_event(app.id, old_status, new_status, data.summary, meta.get('subject', app.email_subject), timestamp)
        logger.info(f"🔄 Updated Application: {app.company_name} ({old_status} -> {new_status})")

    def _log_event(self, app_id, old_s, new_s, summary, subject, timestamp):
        event = ApplicationEventLog(
            application_id=app_id,
            old_status=old_s,
            new_status=new_s,
            summary=summary or "No summary provided",
            email_subject=subject,
            event_date=timestamp
        )
        self.session.add(event)
        self.session.commit()