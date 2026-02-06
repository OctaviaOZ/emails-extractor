from sqlmodel import Session, select
from datetime import datetime
import re
from app.models import JobApplication, ApplicationEventLog, ApplicationStatus, STATUS_RANK, Company, CompanyEmail
from app.services.extractor import ApplicationData
import logging

logger = logging.getLogger(__name__)

class ApplicationProcessor:
    def __init__(self, session: Session, config: dict = None):
        self.session = session
        self.config = config or {}

    def _normalize_company(self, name: str) -> str:
        if not name:
            return ""
        
        # 1. Lowercase and remove punctuation
        name = name.lower()
        name = re.sub(r'[^\w\s]', '', name)
        
        # 2. Define suffixes to strip ONLY at the end
        suffixes = [
            'gmbh', 'ag', 'inc', 'ltd', 'co', 'kg', 'plc', 'se', 'corp', 'corporation', 
            'holding', 'group', 'germany', 'deutschland', 'berlin', 'europe', 'emea', 
            'international', 'solutions', 'systems', 'technology', 'technologies',
            'successfactors', 'workday', 'greenhouse'
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
        Robustly finds an existing application using a tiered matching strategy.
        """
        # 1. Tier 1: Thread ID Match (Highest confidence)
        if thread_id := email_meta.get('thread_id'):
            stmt = select(JobApplication).where(JobApplication.thread_id == thread_id)
            app = self.session.exec(stmt).first()
            if app:
                return app, app.is_active

        all_apps = self.session.exec(select(JobApplication)).all()
        
        # Prepare current email metadata
        new_company_name = data.company_name
        norm_new_name = self._normalize_company(new_company_name)
        sender_email = (email_meta.get('sender_email') or "").lower()
        
        sender_domain = ""
        if '@' in sender_email:
            sender_domain = sender_email.split('@')[1]

        # Get platform/generic info from config
        generic_domains = {'gmail.com', 'yahoo.com', 'outlook.com', 'hotmail.com', 'icloud.com', 'me.com', 'live.com', 'msn.com'}
        extraction_cfg = self.config.get('extraction', {})
        platforms = extraction_cfg.get('platforms', [])
        
        # Detect shared platform
        is_shared_platform = False
        if any(p == sender_domain or sender_domain.endswith("." + p) for p in platforms):
            shared_platform_anchors = {'myworkdayjobs.com', 'successfactors.eu', 'successfactors.com', 'greenhouse.io', 'smartrecruiters.com'}
            if sender_domain in shared_platform_anchors:
                is_shared_platform = True
            
        shared_emails = {
            'notifications@smartrecruiters.com', 
            'no-reply@successfactors.com', 
            'noreply@myworkday.com'
        }

        # Pre-fetch historical emails
        stmt_emails = select(CompanyEmail)
        historical_emails = self.session.exec(stmt_emails).all()
        email_to_company_id = {ce.email: ce.company_id for ce in historical_emails}

        def is_match(app: JobApplication):
            app_email = (app.sender_email or "").lower()
            app_domain = app_email.split('@')[1] if '@' in app_email else ""
            norm_app_name = self._normalize_company(app.company_name)

            # 2. Tier 2: Exact Email Identity Match
            if sender_email and app_email and sender_email == app_email:
                if sender_email not in shared_emails:
                    return True

            # 2.5 Tier 2.5: Historical Company Email Match
            if sender_email in email_to_company_id and app.company_id == email_to_company_id[sender_email]:
                if sender_email not in shared_emails:
                    return True

            # 3. Tier 3: Company Domain Match
            if sender_domain and app_domain and sender_domain == app_domain:
                if not is_shared_platform and sender_domain not in generic_domains:
                    return True

            # 4. Tier 4: Smart Name Match
            if norm_new_name and norm_app_name:
                if norm_new_name == norm_app_name:
                    return True
                
                def is_acronym(short, long):
                    if not short or not long or len(short) < 2: return False
                    words = long.split()
                    if not words: return False
                    acronym = "".join(w[0] for w in words if w)
                    return short == acronym

                if is_acronym(norm_new_name, norm_app_name) or is_acronym(norm_app_name, norm_new_name):
                    return True

                if len(norm_new_name) > 4 and len(norm_app_name) > 4:
                    if norm_new_name in norm_app_name or norm_app_name in norm_new_name:
                        generic_words = {'group', 'systems', 'solutions', 'technologies', 'holding', 'limited'}
                        clean_new = " ".join(w for w in norm_new_name.split() if w not in generic_words)
                        clean_app = " ".join(w for w in norm_app_name.split() if w not in generic_words)
                        if len(clean_new) > 3 and len(clean_app) > 3:
                            if clean_new in clean_app or clean_app in clean_new:
                                return True

            # 5. Tier 5: Shared Platform Context Match (Position + Sender Name)
            # If we are on a shared platform where email/domain matching is impossible
            if is_shared_platform and app_domain == sender_domain:
                # Match if Position is the same AND Sender Name is the same
                if data.position and app.position and data.position.lower() == app.position.lower():
                    sender_name_new = email_meta.get('sender_name', '').lower()
                    sender_name_app = (app.sender_name or '').lower()
                    if sender_name_new and sender_name_app and sender_name_new == sender_name_app:
                        return True

            return False

        # Search prioritized by active status
        for app in all_apps:
            if app.is_active and is_match(app):
                return app, True
        
        # Second pass: recent inactive matches
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
            
            # --- UPGRADE NAME IF CURRENT IS UNKNOWN ---
            # If we matched an application but the current name is 'Unknown' 
            # and the new data HAS a name, upgrade the existing record.
            if existing_app.company_name == "Unknown" and data.company_name != "Unknown":
                logger.info(f"Upgrading application identity from 'Unknown' to '{data.company_name}'")
                company = self._get_or_create_company(data, email_meta)
                existing_app.company_id = company.id
                existing_app.company_name = data.company_name
                self.session.add(existing_app)
                # Note: self.session.commit() will happen inside _update_application
            
            # Normalize Applied/Unknown to COMMUNICATION for existing apps
            if raw_status in [ApplicationStatus.APPLIED, ApplicationStatus.UNKNOWN]:
                raw_status = ApplicationStatus.COMMUNICATION
                if not data.summary or data.summary == "No summary extracted":
                    data.summary = "Application update/communication"

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
        Finds or creates a Company record and remembers the sender email.
        """
        name = data.company_name
        sender_email = (email_meta.get('sender_email') or "").lower()
        domain = None
        if '@' in sender_email:
            domain = sender_email.split('@')[1]
            generic_domains = {'gmail.com', 'yahoo.com', 'outlook.com', 'hotmail.com', 'icloud.com', 'me.com', 'live.com', 'msn.com'}
            if domain in generic_domains:
                domain = None

        # 1. Try by Name (Exact)
        stmt = select(Company).where(Company.name == name)
        company = self.session.exec(stmt).first()
        
        if not company and domain:
            # 2. Try by Domain (if not generic and not a shared platform)
            extraction_cfg = self.config.get('extraction', {})
            platforms = extraction_cfg.get('platforms', [])
            shared_platform_anchors = {'myworkdayjobs.com', 'successfactors.eu', 'successfactors.com', 'greenhouse.io', 'smartrecruiters.com'}
            is_shared_platform = domain in shared_platform_anchors
            
            if not is_shared_platform and domain not in platforms and domain not in ['gmail.com', 'yahoo.com']:
                stmt = select(Company).where(Company.domain == domain)
                company = self.session.exec(stmt).first()

        if company:
            if domain and not company.domain:
                company.domain = domain
                self.session.add(company)
        else:
            # 3. Create New
            company = Company(name=name, domain=domain)
            self.session.add(company)
            self.session.commit()
            self.session.refresh(company)

        # 4. Remember this email for the company (if it's not a generic shared platform email)
        shared_emails = {'notifications@smartrecruiters.com', 'no-reply@successfactors.com', 'noreply@myworkday.com'}
        if sender_email and sender_email not in shared_emails:
            stmt_email = select(CompanyEmail).where(CompanyEmail.email == sender_email)
            existing_email = self.session.exec(stmt_email).first()
            if not existing_email:
                new_email = CompanyEmail(email=sender_email, company_id=company.id)
                self.session.add(new_email)
                self.session.commit()

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
