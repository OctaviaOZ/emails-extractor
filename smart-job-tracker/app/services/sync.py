import os
import logging
import re
from datetime import datetime
from typing import Callable, List, Optional
from sqlmodel import Session, select
from dateutil import parser

from app.models import JobApplication, ApplicationStatus, ProcessedEmail, ApplicationEventLog
from app.services.gmail import get_message_body, batch_get_message_bodies
from app.services.extractor import EmailExtractor, ApplicationData
from app.services.processor import ApplicationProcessor

logger = logging.getLogger(__name__)

class SyncService:
    def __init__(self, session: Session, config: dict, extractor: EmailExtractor):
        self.session = session
        self.config = config
        self.extractor = extractor
        self.processor = ApplicationProcessor(session, config=config)

    def run_sync(self, service, query: str, progress_callback: Optional[Callable[[float, str], None]] = None):
        """
        Runs the full sync process using batch fetching for efficiency.
        """
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
        
        # Sort messages by internalDate ascending so we process the history in order
        messages.reverse()
        total_msgs = len(messages)
        logger.info(f"Processing {total_msgs} messages")
        
        new_emails_count = 0
        errors_count = 0
        batch_size = 10

        # Process in batches
        for i in range(0, total_msgs, batch_size):
            batch_chunk = messages[i:i+batch_size]
            
            # Filter IDs that haven't been processed yet
            msg_ids_to_fetch = []
            for msg in batch_chunk:
                if not self.session.get(ProcessedEmail, msg['id']):
                    msg_ids_to_fetch.append(msg['id'])
            
            # Skip messages already processed in progress bar
            already_processed_in_this_chunk = len(batch_chunk) - len(msg_ids_to_fetch)
            
            # Map of message IDs to their thread IDs for later use
            thread_map = {m['id']: m.get('threadId') for m in batch_chunk}
            
            if not msg_ids_to_fetch:
                if progress_callback:
                    progress_callback(min((i + len(batch_chunk)) / total_msgs, 1.0), f"Skipping {len(batch_chunk)} old emails...")
                continue

            # Fetch bodies in parallel
            try:
                full_msgs = batch_get_message_bodies(service, msg_ids_to_fetch)
                
                # Process each successfully fetched message
                for j, full_msg in enumerate(full_msgs):
                    msg_id = full_msg['id']
                    thread_id = thread_map.get(msg_id)
                    success = self._process_message(full_msg, thread_id=thread_id)
                    if success:
                        new_emails_count += 1
                    else:
                        errors_count += 1
                    
                    if progress_callback:
                        progress_callback(min((i + already_processed_in_this_chunk + j + 1) / total_msgs, 1.0), f"Processed: {full_msg.get('subject', 'Email')[:30]}...")
            
            except Exception as e:
                logger.error(f"Batch processing error: {e}")
                errors_count += len(msg_ids_to_fetch)
                continue
        
        logger.info(f"Sync complete. {new_emails_count} new, {errors_count} errors.")
        return new_emails_count, errors_count

    def _process_message(self, full_msg: dict, thread_id: Optional[str] = None) -> bool:
        """Helper to process a single fetched message."""
        msg_id = full_msg['id']
        try:
            # ... (rest of should_skip logic) ...
            skip_emails = self.config.get('skip_emails', [])
            skip_domains = self.config.get('skip_domains', [])

            # Skip restricted senders or domains
            sender_full = full_msg.get('sender', '')
            sender_name = ""
            sender_email = sender_full
            
            if '<' in sender_full:
                match = re.match(r'^(.*?)\s*<(.+)>', sender_full)
                if match:
                    sender_name, sender_email = match.groups()
                    sender_name = sender_name.strip().replace('"', '')
                    sender_email = sender_email.strip()

            should_skip = False
            if any(email.lower() in sender_email.lower() for email in skip_emails):
                should_skip = True
            elif any(domain.lower() in sender_email.lower() for domain in skip_domains):
                should_skip = True
                
            if should_skip:
                self.session.add(ProcessedEmail(email_id=msg_id, company_name="Skipped"))
                self.session.commit()
                return True

            # Parse Date
            email_dt = datetime.now()
            if full_msg.get('internalDate'):
                try:
                    email_dt = datetime.fromtimestamp(int(full_msg['internalDate']) / 1000.0)
                except: pass
            elif full_msg.get('date'):
                try:
                    email_dt = parser.parse(full_msg['date']).replace(tzinfo=None)
                except: pass

            # Extract Data
            data = self.extractor.extract(full_msg['subject'], full_msg['sender'], full_msg['text'], full_msg['html'])
            
            if not data or data.company_name == "Unknown":
                self.session.add(ProcessedEmail(email_id=msg_id, company_name="Unknown"))
                self.session.commit()
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
            
            # Mark email as processed
            self.session.add(ProcessedEmail(email_id=msg_id, company_name=data.company_name))
            self.session.commit()
            return True
        except Exception as e:
            logger.error(f"Error processing {msg_id}: {e}")
            return False