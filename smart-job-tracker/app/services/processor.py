from sqlmodel import Session, select
from datetime import datetime
import re
from typing import Optional, Dict, Tuple, List, Set
from app.models import JobApplication, ApplicationEventLog, ApplicationStatus, Company, CompanyEmail
from app.services.extractor import ApplicationData
from app.core.constants import (
    COMPANY_SUFFIXES, SHARED_EMAILS, SHARED_PLATFORMS, GENERIC_DOMAINS
)
import logging

logger = logging.getLogger(__name__)

class ApplicationProcessor:
    """Processes extracted email data and updates the job application database."""
    def __init__(self, session: Session):
        self.session = session

    def _normalize_company(self, name: str) -> str:
        """Normalizes company names for better matching."""
        if not name:
            return ""
        
        name = name.lower()
        name = re.sub(r'[^\w\s]', '', name)
        
        changed = True
        while changed:
            changed = False
            name = name.strip()
            for suffix in COMPANY_SUFFIXES:
                pattern = rf'\s+{suffix}$'
                if re.search(pattern, name):
                    name = re.sub(pattern, '', name)
                    changed = True
                    break
        
        return name.strip()

    def _find_existing_application(self, data: ApplicationData, email_meta: Dict) -> Tuple[Optional[JobApplication], bool]:
        """Robustly finds an existing application using a tiered matching strategy."""
        
        # Tier 1: Thread ID Match
        if thread_id := email_meta.get('thread_id'):
            stmt = select(JobApplication).where(JobApplication.thread_id == thread_id)
            app = self.session.exec(stmt).first()
            if app:
                return app, app.is_active

        all_apps = self.session.exec(select(JobApplication)).all()
        
        norm_new_name = self._normalize_company(data.company_name)
        sender_email = (email_meta.get('sender_email') or "").lower()
        sender_domain = sender_email.split('@')[1] if '@' in sender_email else ""

        # Fetch historical emails for matching
        stmt_emails = select(CompanyEmail)
        historical_emails = self.session.exec(stmt_emails).all()
        email_to_company_id = {ce.email: ce.company_id for ce in historical_emails}

        def is_match(app: JobApplication) -> bool:
            app_email = (app.sender_email or "").lower()
            norm_app_name = self._normalize_company(app.company_name)

            # Tier 2: Exact Email Match
            if sender_email and app_email and sender_email == app_email:
                if not self._is_shared_email(sender_email):
                    return True

            # Tier 2.5: Historical Company Email Match
            if sender_email in email_to_company_id and app.company_id == email_to_company_id[sender_email]:
                if not self._is_shared_email(sender_email):
                    return True

            # Tier 3: Company Domain Match
            app_domain = app_email.split('@')[1] if '@' in app_email else ""
            if sender_domain and app_domain and sender_domain == app_domain:
                if not self._is_shared_domain(sender_domain) and not self._is_generic_domain(sender_domain):
                    return True

            # Tier 4: Smart Name Match
            if norm_new_name and norm_app_name:
                if norm_new_name == norm_app_name:
                    return True
                if self._is_acronym_match(norm_new_name, norm_app_name):
                    return True
                if self._is_fuzzy_name_match(norm_new_name, norm_app_name):
                    return True

            return False

        # Prioritize active applications
        for app in all_apps:
            if app.is_active and is_match(app):
                return app, True
        
        # Check inactive applications
        all_apps.sort(key=lambda x: x.last_updated, reverse=True)
        for app in all_apps:
            if not app.is_active and is_match(app):
                return app, False

        return None, False

    def _is_shared_email(self, email: str) -> bool:
        return email in SHARED_EMAILS

    def _is_shared_domain(self, domain: str) -> bool:
        return domain in SHARED_PLATFORMS

    def _is_generic_domain(self, domain: str) -> bool:
        return domain in GENERIC_DOMAINS

    def _is_acronym_match(self, name1: str, name2: str) -> bool:
        def check(short, long):
            if not short or not long or len(short) < 2: return False
            words = long.split()
            if not words: return False
            acronym = "".join(w[0] for w in words if w)
            return short == acronym or (len(short) >= 3 and acronym.startswith(short))
        return check(name1, name2) or check(name2, name1)

    def _is_fuzzy_name_match(self, name1: str, name2: str) -> bool:
        if len(name1) > 4 and len(name2) > 4:
            if name1 in name2 or name2 in name1:
                generic_words = {'group', 'systems', 'solutions', 'technologies', 'holding', 'limited'}
                clean1 = " ".join(w for w in name1.split() if w not in generic_words)
                clean2 = " ".join(w for w in name2.split() if w not in generic_words)
                if len(clean1) > 3 and len(clean2) > 3:
                    if clean1 in clean2 or clean2 in clean1:
                        return True
        return False

    def process_extraction(self, data: ApplicationData, email_meta: Dict, email_timestamp: datetime):
        """Links extracted email data to an existing or new application."""
        existing_app, is_app_active = self._find_existing_application(data, email_meta)
        
        if not existing_app:
            self._create_application(data, email_meta, email_timestamp)
            return

        if is_app_active:
            # Upgrade identity if it was previously unknown
            if existing_app.company_name == "Unknown" and data.company_name != "Unknown":
                logger.info(f"Upgrading application identity to '{data.company_name}'")
                company = self._get_or_create_company(data, email_meta)
                existing_app.company_id = company.id
                existing_app.company_name = data.company_name
            
            # Normalize status for existing apps
            new_status = data.status
            if new_status in [ApplicationStatus.APPLIED, ApplicationStatus.UNKNOWN]:
                new_status = ApplicationStatus.COMMUNICATION
                if data.summary == "No summary provided":
                    data.summary = "Application update/communication"

            # Use JobApplication helper to check if status should be updated
            final_status = existing_app.status
            if existing_app.can_update_status(new_status):
                final_status = new_status
            
            self._update_application(existing_app, data, email_meta, email_timestamp, override_status=final_status)
        else:
            # Re-activate or ignore based on status
            if data.status in [ApplicationStatus.APPLIED, ApplicationStatus.INTERVIEW, ApplicationStatus.ASSESSMENT, ApplicationStatus.OFFER]:
                self._create_application(data, email_meta, email_timestamp)
            else:
                self._update_application(existing_app, data, email_meta, email_timestamp, override_status=existing_app.status)

    def _get_or_create_company(self, data: ApplicationData, email_meta: Dict) -> Company:
        """Finds or creates a Company record."""
        name = data.company_name
        sender_email = (email_meta.get('sender_email') or "").lower()
        domain = sender_email.split('@')[1] if '@' in sender_email else None
        
        if domain and self._is_generic_domain(domain):
            domain = None

        company = self.session.exec(select(Company).where(Company.name == name)).first()
        
        if not company and domain and not self._is_shared_domain(domain):
            company = self.session.exec(select(Company).where(Company.domain == domain)).first()

        if company:
            if domain and not company.domain:
                company.domain = domain
                self.session.add(company)
        else:
            company = Company(name=name, domain=domain)
            self.session.add(company)
            self.session.commit()
            self.session.refresh(company)

        # Link email to company
        if sender_email and not self._is_shared_email(sender_email):
            existing_email = self.session.exec(select(CompanyEmail).where(CompanyEmail.email == sender_email)).first()
            if not existing_email:
                new_email = CompanyEmail(email=sender_email, company_id=company.id)
                self.session.add(new_email)
                self.session.commit()

        return company

    def _create_application(self, data: ApplicationData, meta: Dict, timestamp: datetime):
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
            reached_assessment=data.status == ApplicationStatus.ASSESSMENT,
            reached_interview=data.status == ApplicationStatus.INTERVIEW,
            year=meta.get('year', timestamp.year),
            month=meta.get('month', timestamp.month),
            day=meta.get('day', timestamp.day)
        )
        self.session.add(new_app)
        self.session.commit()
        self.session.refresh(new_app)
        
        self._log_event(new_app.id, None, new_app.status, data.summary, meta.get('subject', 'No Subject'), timestamp)
        logger.info(f"🆕 New Application: {data.company_name}")

    def _update_application(self, app: JobApplication, data: ApplicationData, meta: Dict, timestamp: datetime, override_status: Optional[ApplicationStatus] = None):
        old_status = app.status
        new_status = override_status if override_status else data.status
        
        app.status = new_status
        
        if timestamp >= app.last_updated:
            app.last_updated = timestamp
            app.email_id = meta.get('id', app.email_id)
            app.email_subject = meta.get('subject', app.email_subject)
            app.email_snippet = meta.get('snippet', app.email_snippet)
            app.summary = data.summary
            
        if data.is_rejection:
            app.is_active = False
        
        if data.position and (app.position == "Unknown Position" or not app.position):
            app.position = data.position
            
        if new_status == ApplicationStatus.ASSESSMENT:
            app.reached_assessment = True
        if new_status == ApplicationStatus.INTERVIEW:
            app.reached_interview = True
            
        self.session.add(app)
        self.session.commit()

        self._log_event(app.id, old_status, new_status, data.summary, meta.get('subject', app.email_subject), timestamp)
        logger.info(f"🔄 Updated Application: {app.company_name} ({old_status} -> {new_status})")

    def _log_event(self, app_id: int, old_s: Optional[ApplicationStatus], new_s: ApplicationStatus, summary: str, subject: str, timestamp: datetime):
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
