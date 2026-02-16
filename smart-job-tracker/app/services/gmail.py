import os.path
import pickle
import logging
import socket
import ssl
import re
import base64
from typing import List, Dict, Optional, Any
from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build, Resource
from googleapiclient.errors import HttpError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

def clean_html_for_llm(html: str) -> str:
    """
    Cleans HTML content to be LLM-friendly while preserving table structures.
    """
    if not html:
        return ""
    
    soup = BeautifulSoup(html, "html.parser")
    
    # Remove non-content elements
    for script_or_style in soup(["script", "style", "nav", "footer", "header", "meta", "link"]):
        script_or_style.decompose()

    # Convert tables to Markdown-like text
    for table in soup.find_all("table"):
        table_text = []
        for row in table.find_all("tr"):
            cells = [cell.get_text(strip=True) for cell in row.find_all(["td", "th"])]
            if any(cells):
                table_text.append("| " + " | ".join(cells) + " |")
        
        if table_text:
            table_md = "\n" + "\n".join(table_text) + "\n"
            table.replace_with(table_md)

    text = soup.get_text(separator=' ', strip=True)
    
    # Clean up whitespace
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n', '\n\n', text)
    
    return text.strip()

def get_gmail_service(credentials_path: str = 'credentials.json', token_path: str = 'token.pickle', scopes: Optional[List[str]] = None) -> Resource:
    """Authenticates and returns the Gmail API service."""
    creds = None
    use_scopes = scopes if scopes else SCOPES
    
    if os.path.exists(token_path):
        with open(token_path, 'rb') as token:
            creds = pickle.load(token)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(credentials_path):
                raise FileNotFoundError(f"Credentials file not found at {credentials_path}")
            
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, use_scopes)
            creds = flow.run_local_server(port=0)
        
        with open(token_path, 'wb') as token:
            pickle.dump(creds, token)

    return build('gmail', 'v1', credentials=creds, cache_discovery=False)

@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((HttpError, socket.timeout, ConnectionError, ssl.SSLError))
)
def get_message_body(service: Resource, msg_id: str) -> Optional[Dict[str, Any]]:
    """Fetches and parses a single Gmail message by ID."""
    try:
        message = service.users().messages().get(userId='me', id=msg_id, format='full').execute()
        return _parse_message_response(message, msg_id)
    except HttpError as error:
        if error.resp.status in [429, 500, 502, 503, 504]:
            logger.warning(f"Transient error fetching message {msg_id}: {error}. Retrying...")
            raise error
        logger.error(f"Permanent error fetching message {msg_id}: {error}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error fetching message {msg_id}: {e}")
        return None

def batch_get_message_bodies(service: Resource, msg_ids: List[str]) -> List[Dict[str, Any]]:
    """Fetches multiple Gmail messages in a single batch request."""
    results: List[Dict[str, Any]] = []
    
    def callback(request_id, response, exception):
        if exception:
            logger.error(f"Error in batch request for {request_id}: {exception}")
        else:
            results.append(_parse_message_response(response, request_id))

    batch = service.new_batch_http_request(callback=callback)
    for msg_id in msg_ids:
        batch.add(service.users().messages().get(userId='me', id=msg_id, format='full'), request_id=msg_id)
    
    batch.execute()
    return results

def _parse_message_response(message: Dict[str, Any], msg_id: str) -> Dict[str, Any]:
    """Internal helper to parse the raw Gmail API message response."""
    payload = message.get('payload', {})
    headers = payload.get('headers', [])
    
    subject = next((h['value'] for h in headers if h['name'].lower() == 'subject'), 'No Subject')
    sender = next((h['value'] for h in headers if h['name'].lower() == 'from'), 'Unknown')
    date = next((h['value'] for h in headers if h['name'].lower() == 'date'), '')
    
    snippet = message.get('snippet', '')
    internal_date = message.get('internalDate', '')

    body_text = ""
    body_html = ""

    def find_parts(parts_list):
        nonlocal body_text, body_html
        for part in parts_list:
            mime_type = part.get('mimeType')
            data = part.get('body', {}).get('data')
            if data:
                decoded = base64.urlsafe_b64decode(data.encode('UTF-8')).decode('utf-8', errors='replace')
                if mime_type == 'text/plain':
                    body_text += decoded
                elif mime_type == 'text/html':
                    body_html += decoded
            if part.get('parts'):
                find_parts(part['parts'])
    
    parts = payload.get('parts', [])
    if not parts and payload.get('body', {}).get('data'):
        data = payload['body']['data']
        decoded = base64.urlsafe_b64decode(data.encode('UTF-8')).decode('utf-8', errors='replace')
        if payload.get('mimeType') == 'text/plain':
            body_text = decoded
        elif payload.get('mimeType') == 'text/html':
            body_html = decoded
    else:
        find_parts(parts)

    return {
        "id": msg_id,
        "subject": subject,
        "sender": sender,
        "date": date,
        "internalDate": internal_date,
        "snippet": snippet,
        "text": body_text,
        "html": body_html
    }
