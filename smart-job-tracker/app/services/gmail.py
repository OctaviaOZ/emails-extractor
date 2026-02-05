import os.path
import pickle
import logging
import socket
import ssl
from typing import List
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
    Cleans HTML content to be LLM-friendly by removing non-essential tags
    and reducing token usage.
    """
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    # Remove script, style, and nav elements which contain no job info
    for script_or_style in soup(["script", "style", "nav", "footer", "header", "meta", "link"]):
        script_or_style.decompose()
    return soup.get_text(separator=' ', strip=True)

def get_gmail_service(credentials_path: str = 'credentials.json', token_path: str = 'token.pickle', scopes: list = None) -> Resource:
    """Shows basic usage of the Gmail API.
    Lists the user's Gmail labels.
    """
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
            
            flow = InstalledAppFlow.from_client_secrets_file(
                credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
        
        with open(token_path, 'wb') as token:
            pickle.dump(creds, token)

    service = build('gmail', 'v1', credentials=creds, cache_discovery=False)
    return service

@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((HttpError, socket.timeout, ConnectionError, ssl.SSLError))
)
def get_message_body(service: Resource, msg_id: str) -> dict:
    """
    Fetches and decodes the email body.
    Returns a dict with 'snippet', 'text', 'html', 'date', 'subject', 'from', 'internalDate'.
    """
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
        raise e

def batch_get_message_bodies(service: Resource, msg_ids: List[str]) -> List[dict]:
    """
    Fetches multiple message bodies in a single batch request.
    """
    results = []
    
    def callback(request_id, response, exception):
        if exception:
            logger.error(f"Error in batch request for {request_id}: {exception}")
            results.append(None)
        else:
            # We need to manually parse the response here similar to get_message_body
            results.append(_parse_message_response(response, request_id))

    batch = service.new_batch_http_request(callback=callback)
    for msg_id in msg_ids:
        batch.add(service.users().messages().get(userId='me', id=msg_id, format='full'), request_id=msg_id)
    
    batch.execute()
    return [r for r in results if r is not None]

def _parse_message_response(message: dict, msg_id: str) -> dict:
    """Helper to parse raw Gmail message response."""
    headers = message['payload']['headers']
    subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
    sender = next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown')
    date = next((h['value'] for h in headers if h['name'] == 'Date'), '')
    snippet = message.get('snippet', '')
    internal_date = message.get('internalDate', '')

    parts = message['payload'].get('parts', [])
    body_text = ""
    body_html = ""

    def find_parts(parts_list):
        nonlocal body_text, body_html
        for part in parts_list:
            mime_type = part.get('mimeType')
            data = part.get('body', {}).get('data')
            if data:
                import base64
                decoded = base64.urlsafe_b64decode(data.encode('UTF-8')).decode('utf-8')
                if mime_type == 'text/plain':
                    body_text += decoded
                elif mime_type == 'text/html':
                    body_html += decoded
            if part.get('parts'):
                find_parts(part['parts'])
    
    if not parts and message['payload'].get('body', {}).get('data'):
        data = message['payload']['body']['data']
        import base64
        decoded = base64.urlsafe_b64decode(data.encode('UTF-8')).decode('utf-8')
        if message['payload']['mimeType'] == 'text/plain':
            body_text = decoded
        elif message['payload']['mimeType'] == 'text/html':
            body_html = decoded

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
