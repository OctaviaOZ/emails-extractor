import os.path
import pickle
import logging
import socket
import ssl
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build, Resource
from googleapiclient.errors import HttpError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

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

    except HttpError as error:
        if error.resp.status in [429, 500, 502, 503, 504]:
            logger.warning(f"Transient error fetching message {msg_id}: {error}. Retrying...")
            raise error
        logger.error(f"Permanent error fetching message {msg_id}: {error}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error fetching message {msg_id}: {e}")
        raise e
