import os.path
import pickle
import logging
import socket
import ssl
import re
import base64
import json
from typing import List, Dict, Optional, Any
from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build, Resource
from googleapiclient.errors import HttpError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.core.config import get_gmail_config, settings

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

_SIGNATURE_PATTERNS = re.compile(
    r'(--\s*\n|_{3,}|-{3,}|'                        # common signature dividers
    r'unsubscribe|abmelden|abbestellen|'              # unsubscribe (EN/DE)
    r'von meinem iphone|sent from my|'               # mobile signatures
    r'diese e-mail wurde|this email was sent|'        # boilerplate footer starts
    r'impressum|datenschutz|privacy policy|'          # legal footer markers
    r'if you.*no longer.*receive|'                    # unsubscribe prose
    r'copyright\s*©|\ball rights reserved\b)',        # copyright lines
    re.IGNORECASE,
)

def clean_html_for_llm(html: str, max_chars: int = 1500) -> str:
    """
    Cleans HTML content to be LLM-friendly.
    Strips noise (signatures, footers, unsubscribe, prior thread) and caps output.
    """
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")

    # Remove non-content elements
    for tag in soup(["script", "style", "nav", "footer", "header", "meta", "link"]):
        tag.decompose()

    # Remove elements that are purely decorative / footer-like by common class/id names.
    # Collect first, then decompose — decompose() recursively clears children, so calling
    # .get() on already-decomposed siblings later in the same iteration raises AttributeError.
    tags_to_remove = [
        tag for tag in soup.find_all(True)
        if re.search(
            r'footer|signature|unsubscribe|disclaimer|legal|mso-',
            " ".join(tag.get("class") or []) + (tag.get("id") or ""),
            re.IGNORECASE,
        )
    ]
    for tag in tags_to_remove:
        tag.decompose()

    # Convert tables to Markdown-like text (keep it lightweight — first 5 rows only)
    for table in soup.find_all("table"):
        rows = table.find_all("tr")[:5]
        table_text = []
        for row in rows:
            cells = [cell.get_text(strip=True) for cell in row.find_all(["td", "th"])]
            if any(cells):
                table_text.append("| " + " | ".join(cells) + " |")
        table.replace_with(("\n" + "\n".join(table_text) + "\n") if table_text else "")

    text = soup.get_text(separator="\n", strip=True)

    # Drop everything from the first signature/footer marker onwards
    match = _SIGNATURE_PATTERNS.search(text)
    if match:
        text = text[:match.start()]

    # Collapse whitespace
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()

    # Hard cap — LLM only needs the opening of the email to understand intent
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[…]"

    return text

def get_gmail_service(token_path: str = None, scopes: Optional[List[str]] = None) -> Resource:
    """Authenticates and returns the Gmail API service using Secret Manager."""
    creds = None
    use_scopes = scopes if scopes else SCOPES
    
    # Use path from settings if not provided
    target_token_path = token_path if token_path else str(settings.token_path)
    
    if os.path.exists(target_token_path):
        with open(target_token_path, 'rb') as token:
            creds = pickle.load(token)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # Use Secret Manager exclusively
            client_config_json = get_gmail_config()
            if not client_config_json:
                raise RuntimeError("Failed to retrieve Gmail configuration from Secret Manager.")
                
            logger.info("Using credentials from Google Cloud Secret Manager")
            try:
                client_config = json.loads(client_config_json)
                flow = InstalledAppFlow.from_client_config(client_config, use_scopes)
                creds = flow.run_local_server(port=0)
            except Exception as e:
                logger.error(f"Failed to initialize OAuth flow from Secret Manager config: {e}")
                raise
        
        with open(target_token_path, 'wb') as token:
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
            data = (part.get('body') or {}).get('data')
            if data:
                decoded = base64.urlsafe_b64decode(data.encode('UTF-8')).decode('utf-8', errors='replace')
                if mime_type == 'text/plain':
                    body_text += decoded
                elif mime_type == 'text/html':
                    body_html += decoded
            if part.get('parts'):
                find_parts(part['parts'])
    
    parts = payload.get('parts', [])
    if not parts and (payload.get('body') or {}).get('data'):
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
