import logging
import re
from datetime import datetime
from typing import Callable, List, Optional, Dict, Tuple, Any
from sqlmodel import Session
from dateutil import parser

from app.models import ProcessedEmail
from app.services.gmail import batch_get_message_bodies, clean_html_for_llm
from app.services.extractor import EmailExtractor
from app.services.processor import ApplicationProcessor
from app.core.config import settings

logger = logging.getLogger(__name__)

class SyncService:
    """Orchestrates the synchronization between Gmail and the local database."""
    def __init__(self, session: Session, extractor: EmailExtractor):
        self.session = session
        self.extractor = extractor
        self.processor = ApplicationProcessor(session)

    def run_sync(self, service: Any, query: str, progress_callback: Optional[Callable[[float, str], None]] = None) -> Tuple[int, int]:
        """Runs the full sync process using batch fetching."""
        logger.info("Sync started")
        
        messages = []
        try:
            request = service.users().messages().list(userId='me', q=query)
            while request is not None:
                response = request.execute()
                messages.extend(response.get('messages', []))
                request = service.users().messages().list_next(request, response)
        except Exception as e:
            logger.error(f"Failed to fetch messages: {e}")
            raise e
        
        if not messages:
            logger.info("No messages found.")
            return 0, 0
        
        # Process chronologically
        messages.reverse()
        total_msgs = len(messages)
        logger.info(f"Processing {total_msgs} messages")
        
        new_emails_count = 0
        errors_count = 0
        batch_size = 10

        for i in range(0, total_msgs, batch_size):
            batch_chunk = messages[i:i+batch_size]
            
            msg_ids_to_fetch = [
                m['id'] for m in batch_chunk 
                if not self.session.get(ProcessedEmail, m['id'])
            ]
            
            already_processed = len(batch_chunk) - len(msg_ids_to_fetch)
            thread_map = {m['id']: m.get('threadId') for m in batch_chunk}
            
            if not msg_ids_to_fetch:
                if progress_callback:
                    progress_callback(min((i + len(batch_chunk)) / total_msgs, 1.0), "Skipping old emails...")
                continue

            try:
                full_msgs = batch_get_message_bodies(service, msg_ids_to_fetch)
                
                for j, full_msg in enumerate(full_msgs):
                    msg_id = full_msg['id']
                    thread_id = thread_map.get(msg_id)
                    success = self._process_message(full_msg, thread_id=thread_id)
                    
                    if success:
                        new_emails_count += 1
                    else:
                        errors_count += 1
                    
                    if progress_callback:
                        progress_callback(
                            min((i + already_processed + j + 1) / total_msgs, 1.0), 
                            f"Processed: {full_msg.get('subject', 'Email')[:30]}..."
                        )
            except Exception as e:
                logger.error(f"Batch processing error: {e}")
                errors_count += len(msg_ids_to_fetch)
                continue
        
        logger.info(f"Sync complete. {new_emails_count} new, {errors_count} errors.")
        return new_emails_count, errors_count

    def _process_message(self, full_msg: Dict, thread_id: Optional[str] = None) -> bool:
        """Processes a single email message."""
        msg_id = full_msg['id']
        try:
            sender_full = full_msg.get('sender', '')
            sender_name, sender_email = self._parse_sender(sender_full)

            if self._should_skip(sender_email):
                self._mark_processed(msg_id, "Skipped")
                return True

            email_dt = self._parse_date(full_msg)
            cleaned_html = clean_html_for_llm(full_msg.get('html', ''))
            
            data = self.extractor.extract(full_msg['subject'], full_msg['sender'], full_msg['text'], cleaned_html)
            
            if not data or data.company_name == "Unknown":
                self._mark_processed(msg_id, "Unknown")
                return True

            email_meta = {
                'subject': full_msg['subject'],
                'year': email_dt.year,
                'month': email_dt.month,
                'day': email_dt.day,
                'sender_name': sender_name,
                'sender_email': sender_email,
                'snippet': full_msg.get('snippet'),
                'id': msg_id,
                'thread_id': thread_id
            }
            self.processor.process_extraction(data, email_meta, email_timestamp=email_dt)
            
            self._mark_processed(msg_id, data.company_name)
            return True
        except Exception as e:
            logger.error(f"Error processing {msg_id}: {e}")
            return False

    def _parse_sender(self, sender_full: str) -> Tuple[str, str]:
        sender_name = ""
        sender_email = sender_full
        if '<' in sender_full:
            match = re.match(r'^(.*?)\s*<(.+)>', sender_full)
            if match:
                sender_name, sender_email = match.groups()
                sender_name = sender_name.strip().replace('"', '')
                sender_email = sender_email.strip()
        return sender_name, sender_email

    def _should_skip(self, email: str) -> bool:
        email_lower = email.lower()
        return any(e.lower() in email_lower for e in settings.skip_emails) or \
               any(d.lower() in email_lower for d in settings.skip_domains)

    def _parse_date(self, full_msg: Dict) -> datetime:
        if full_msg.get('internalDate'):
            try:
                return datetime.fromtimestamp(int(full_msg['internalDate']) / 1000.0)
            except: pass
        if full_msg.get('date'):
            try:
                return parser.parse(full_msg['date']).replace(tzinfo=None)
            except: pass
        return datetime.now()

    def _mark_processed(self, msg_id: str, company_name: str):
        self.session.add(ProcessedEmail(email_id=msg_id, company_name=company_name))
        self.session.commit()
