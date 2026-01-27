import os.path
import pickle
import logging
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build, Resource

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

def get_gmail_service(credentials_path: str = 'credentials.json', token_path: str = 'token.pickle', scopes: list = None) -> Resource:
    """Shows basic usage of the Gmail API.
    Lists the user's Gmail labels.
    """
    creds = None
    use_scopes = scopes if scopes else SCOPES
    # The file token.pickle stores the user's access and refresh tokens, and is
    # created automatically when the authorization flow completes for the first
    # time.
    if os.path.exists(token_path):
        with open(token_path, 'rb') as token:
            creds = pickle.load(token)
    
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(credentials_path):
                raise FileNotFoundError(f"Credentials file not found at {credentials_path}")
            
            flow = InstalledAppFlow.from_client_secrets_file(
                credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
        
        # Save the credentials for the next run
        with open(token_path, 'wb') as token:
            pickle.dump(creds, token)

    service = build('gmail', 'v1', credentials=creds)
    return service

def get_message_body(service: Resource, msg_id: str) -> dict:
    """
    Fetches and decodes the email body.
    Returns a dict with 'snippet', 'text', 'html', 'date', 'subject', 'from'.
    """
    try:
        message = service.users().messages().get(userId='me', id=msg_id, format='full').execute()
        
        headers = message['payload']['headers']
        subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
        sender = next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown')
        date = next((h['value'] for h in headers if h['name'] == 'Date'), '')
        snippet = message.get('snippet', '')

        parts = message['payload'].get('parts', [])
        body_text = ""
        body_html = ""

        # Recursive function to find parts
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
             # Single part message
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
            "snippet": snippet,
            "text": body_text,
            "html": body_html
        }

    except Exception as e:
        logger.error(f"Error fetching message {msg_id}: {e}")
        return None
