import os
import pickle
import re
import csv
import yaml
import logging
import base64
import ssl
import socket

from bs4 import BeautifulSoup

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

import dateutil.parser as dateutil_parser
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
import time
from typing import Dict, List, Tuple, Optional, Any

import urllib3
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import spacy

# --- NLP Model ---
nlp = spacy.load('de_core_news_sm')

# --- Configuration ---
def load_config(config_file='config.yaml') -> Dict[str, Any]:
    with open(config_file, 'r') as f:
        return yaml.safe_load(f)

CONFIG = load_config()

# --- Logging ---
logging.basicConfig(
    filename=CONFIG['log_file'],
    level=CONFIG['log_level'],
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- Gmail Authenticator ---
class GmailAuthenticator:
    def __init__(self, credentials_file, token_file, scopes):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.scopes = scopes

    def authenticate(self):
        creds = None
        if os.path.exists(self.token_file):
            with open(self.token_file, 'rb') as token:
                creds = pickle.load(token)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except RefreshError:
                    os.remove(self.token_file)
                    creds = self._run_flow()
            else:
                creds = self._run_flow()
            with open(self.token_file, 'wb') as token:
                pickle.dump(creds, token)
        return creds

    def _run_flow(self):
        flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, self.scopes)
        return flow.run_console()

# --- Gmail Client ---
class GmailClient:
    def __init__(self, authenticator):
        self.service = build('gmail', 'v1', credentials=authenticator.authenticate(), cache_discovery=False)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        retry=retry_if_exception_type((HttpError, socket.error, ssl.SSLError, requests.exceptions.RequestException))
    )
    def list_labels(self) -> List[Dict[str, Any]]:
        response = self.service.users().labels().list(userId='me').execute()
        return response.get('labels', [])

    def get_label_id(self, label_name: str) -> Optional[str]:
        labels = self.list_labels()
        return next((label['id'] for label in labels if label['name'] == label_name), None)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        retry=retry_if_exception_type((HttpError, socket.error, ssl.SSLError, requests.exceptions.RequestException))
    )
    def list_messages(self, user_id='me', label_ids=[], query='') -> List[Dict[str, Any]]:
        messages = []
        request = self.service.users().messages().list(userId=user_id, labelIds=label_ids, q=query)
        while request is not None:
            response = request.execute()
            messages.extend(response.get('messages', []))
            request = self.service.users().messages().list_next(request, response)
        return messages

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        retry=retry_if_exception_type((HttpError, socket.error, ssl.SSLError, requests.exceptions.RequestException))
    )
    def get_message_details(self, message_id: str) -> Dict[str, Any]:
        return self.service.users().messages().get(userId='me', id=message_id, format='full').execute()

# --- Email Parser ---
class EmailParser:
    def __init__(self, config):
        self.config = config

    def parse_message(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            headers = {header['name']: header['value'] for header in message['payload']['headers']}
            sender = headers.get('From')
            date = headers.get('Date')
            subject = headers.get('Subject')

            plain_text, html_text = self._extract_email_body(message)
            content_analysis = self._analyze_content(plain_text, html_text)

            sender_name, email_address, domain = self._extract_email_parts(sender)
            if domain in self.config['skip_domains']:
                return None

            formatted_date, year, month, day = self._parse_date(date)
            user_name = email_address.split('@')[0]
            company_name = self._get_company_name(domain, user_name, subject)

            return {
                'year': year,
                'month': month,
                'day': day,
                'date': formatted_date,
                'domain': domain,
                'sender_name': sender_name,
                'subject': subject,
                'user_name': user_name,
                'company_name': company_name,
                'word_count': content_analysis['word_count'],
                'has_application_link': content_analysis['has_application_link'],
                'link_count': len(content_analysis['links']),
                'keywords': ','.join(content_analysis['keywords'])
            }
        except Exception as e:
            logger.error(f"Error parsing message: {e}")
            return None

    def _extract_email_body(self, message: Dict[str, Any]) -> Tuple[str, str]:
        payload = message.get('payload', {})
        plain_text = ""
        html_text = ""

        if 'parts' in payload:
            for part in payload['parts']:
                if part['mimeType'] == 'text/plain':
                    plain_text = self._decode_base64(part['body'].get('data', ''))
                elif part['mimeType'] == 'text/html':
                    html_text = self._decode_base64(part['body'].get('data', ''))
        elif 'body' in payload and 'data' in payload['body']:
            if payload['mimeType'] == 'text/plain':
                plain_text = self._decode_base64(payload['body']['data'])
            elif payload['mimeType'] == 'text/html':
                html_text = self._decode_base64(payload['body']['data'])

        return plain_text, html_text

    def _decode_base64(self, data: str) -> str:
        try:
            return base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)).decode('utf-8')
        except Exception as e:
            logger.error(f"Error decoding base64 data: {e}")
            return ""

    def _extract_email_parts(self, email: str) -> Tuple[str, str, str]:
        match = re.match(r'^(.*?)\s*<(.+)>', email)
        if match:
            name, email_address = match.groups()
        else:
            name, email_address = '', email
        domain = email_address.split('@')[-1]
        return name.strip(), email_address, domain

    def _parse_date(self, date_str: str) -> Tuple[str, str, str, str]:
        try:
            dt = dateutil_parser.parse(date_str)
            return dt.strftime('%Y-%m-%d'), str(dt.year), str(dt.month), str(dt.day)
        except Exception as e:
            logger.warning(f"Could not parse date '{date_str}': {e}")
            return '', '', '', ''

    def _get_company_name(self, domain: str, user_name: str, subject: str) -> str:
        if domain in self.config['special_domains']:
            return user_name
        elif domain in self.config['subject_domains']:
            return subject.split()[-1]
        else:
            return domain.split('.')[0]

    def _analyze_content(self, plain_text: str, html_text: str) -> Dict[str, Any]:
        analysis = {
            'word_count': 0,
            'has_application_link': False,
            'links': [],
            'keywords': []
        }
        text = plain_text + " " + BeautifulSoup(html_text, 'html.parser').get_text()
        analysis['word_count'] = len(text.split())
        analysis['links'] = self._extract_links(html_text)
        
        application_keywords = self.config['content_analysis']['application_keywords']
        found_keywords = [word for word in application_keywords if word.lower() in text.lower()]
        analysis['keywords'] = found_keywords
        
        application_domains = self.config['content_analysis']['application_domains']
        analysis['has_application_link'] = any(domain in ' '.join(analysis['links']).lower() for domain in application_domains)
        
        return analysis

    def _extract_links(self, html_content: str) -> List[str]:
        if not html_content:
            return []
        soup = BeautifulSoup(html_content, 'html.parser')
        return [a.get('href') for a in soup.find_all('a', href=True)]

# --- Csv Writer ---
class CsvWriter:
    def __init__(self, filename):
        self.filename = filename

    def write(self, data: List[Dict[str, Any]]):
        if not data:
            logger.warning("No data to write to CSV.")
            return
        logger.info(f"Generating CSV file: {self.filename}")
        with open(self.filename, mode='w', newline='', encoding='utf-8-sig') as file:
            fieldnames = data[0].keys()
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(data)
        logger.info(f"CSV file '{self.filename}' generated successfully with {len(data)} rows.")

# --- Email Processor ---
class EmailProcessor:
    def __init__(self, config):
        self.config = config
        self.authenticator = GmailAuthenticator(
            config['credentials_file'],
            config['token_file'],
            config.get('scopes', ['https://www.googleapis.com/auth/gmail.readonly']) # Add default scopes
        )
        self.gmail_client = GmailClient(self.authenticator)
        self.email_parser = EmailParser(config)
        self.csv_writer = CsvWriter(
            os.path.join(config['output_directory'] or os.getcwd(), config['output_filename'])
        )

    def process_emails(self):
        logger.info("Starting email processing...")
        label_id = self.gmail_client.get_label_id(self.config['label_name'])
        if not label_id:
            logger.error(f'Label "{self.config["label_name"]}" not found.')
            return

        query = f"after:{self.config['start_date']}"
        logger.info(f"Fetching messages with query: {query}")
        messages = self.gmail_client.list_messages(label_ids=[label_id], query=query)

        if not messages:
            logger.warning("No messages found matching the criteria.")
            return

        logger.info(f"Found {len(messages)} messages to process")
        
        csv_data = []
        for msg in messages:
            try:
                message_details = self.gmail_client.get_message_details(msg['id'])
                parsed_data = self.email_parser.parse_message(message_details)
                if parsed_data:
                    csv_data.append(parsed_data)
            except Exception as e:
                logger.error(f"Error processing message: {e}")
        
        self.csv_writer.write(csv_data)
        logger.info(f"CSV file generated successfully with {len(csv_data)} entries.")

# --- Main Execution ---
def main():
    try:
        config = load_config()
        email_processor = EmailProcessor(config)
        email_processor.process_emails()
    except Exception as e:
        logger.critical(f"An unexpected error occurred: {e}")
        raise

if __name__ == "__main__":
    main()