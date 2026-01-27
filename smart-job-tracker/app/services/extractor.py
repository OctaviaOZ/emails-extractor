import re
import os
import json
import logging
from typing import Optional
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
from models import ApplicationStatus

logger = logging.getLogger(__name__)

class ExtractedData(BaseModel):
    company_name: str
    position: Optional[str] = None
    status: ApplicationStatus
    summary: str

class EmailExtractor:
    def __init__(self, openai_api_key: Optional[str] = None, gemini_api_key: Optional[str] = None):
        self.openai_key = openai_api_key or os.getenv("OPENAI_API_KEY")
        self.gemini_key = gemini_api_key or os.getenv("GOOGLE_API_KEY")
        
        # Handle empty strings from .env
        if not self.openai_key: self.openai_key = None
        if not self.gemini_key: self.gemini_key = None

    def extract(self, subject: str, sender: str, body_text: str, body_html: str) -> ExtractedData:
        # Priority 1: LLM Extraction (most accurate)
        if self.openai_key:
            try:
                return self._extract_with_openai(subject, sender, body_text)
            except Exception as e:
                logger.warning(f"OpenAI extraction failed: {e}. Falling back to heuristic.")
        
        if self.gemini_key:
             try:
                return self._extract_with_gemini(subject, sender, body_text)
             except Exception as e:
                logger.warning(f"Gemini extraction failed: {e}. Falling back to heuristic.")

        # Priority 2: Heuristic Extraction (Fast, Free, less accurate)
        return self._extract_heuristic(subject, sender, body_text, body_html)

    def _extract_with_openai(self, subject: str, sender: str, text: str) -> ExtractedData:
        from openai import OpenAI
        client = OpenAI(api_key=self.openai_key)
        
        prompt = f"""
        Analyze this job application email and extract data into JSON.
        Sender: {sender}
        Subject: {subject}
        Body: {text[:3000]}
        
        IMPORTANT: Extract the ACTUAL EMPLOYER (the company applied to).
        - IGNORE platform names (Workday, LinkedIn, SmartRecruiters, Greenhouse, Lever, Personio, etc.).
        - Check the Subject and Body carefully for the employer.
        
        Extract:
        1. "company_name": The employer name.
        2. "position": The job title (if mentioned).
        3. "status": Must be one of: 'Applied', 'Communication', 'Interview', 'Assessment', 'Offer', 'Rejected'. Default to 'Communication'.
        """
        
        completion = client.chat.completions.create(
            model="gpt-3.5-turbo-0125",
            messages=[
                {"role": "system", "content": "You are a recruitment data extractor. Return JSON only."},
                {"role": "user", "content": prompt}
            ],
            response_format={ "type": "json_object" }
        )
        
        content = completion.choices[0].message.content
        data = json.loads(content)
        
        status_str = data.get("status", "Unknown")
        status = self._map_status(status_str)

        return ExtractedData(
            company_name=data.get("company_name", "Unknown"),
            position=data.get("position"),
            status=status,
            summary=f"Extracted via AI: {status_str}"
        )

    def _extract_with_gemini(self, subject: str, sender: str, text: str) -> ExtractedData:
        import google.generativeai as genai
        genai.configure(api_key=self.gemini_key)
        model = genai.GenerativeModel('gemini-2.0-flash')
        
        prompt = f"""
        Analyze this recruitment email and return a JSON object.
        Sender: {sender}
        Subject: {subject}
        Body: {text[:4000]}
        
        Return JSON:
        1. "company_name": The company name (NOT the platform like LinkedIn/Personio).
        2. "position": Job title.
        3. "status": One of: 'Applied', 'Communication', 'Interview', 'Assessment', 'Offer', 'Rejected'.
        Choose 'Communication' for general updates or back-and-forth.
        """
        
        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                response_mime_type="application/json",
            ),
        )
        
        try:
            data = json.loads(response.text)
            status_str = data.get("status", "Unknown")
            status = self._map_status(status_str)

            return ExtractedData(
                company_name=data.get("company_name", "Unknown"),
                position=data.get("position"),
                status=status,
                summary=f"Extracted via Gemini: {status_str}"
            )
        except Exception as e:
            logger.error(f"Failed to parse Gemini response: {e}")
            raise e

    def _extract_heuristic(self, subject: str, sender: str, text: str, html: str) -> ExtractedData:
        platforms = [
            'myworkday', 'workday', 'smartrecruiters', 'linkedin', 'indeed', 
            'greenhouse', 'lever', 'ashby', 'recruitee', 'bamboohr', 'jobvite', 
            'icims', 'taleo', 'successfactors', 'join.com', 'teamtailor', 'personio',
            'workable'
        ]
        
        generic_names = {
            'noreply', 'no-reply', 'donotreply', 'mailer', 'mailer-daemon',
            'candidates', 'candidate', 'careers', 'jobs', 'hiring', 'hire', 'team', 'work', 'talent',
            'recruiting', 'recruit', 'application', 'applications', 'apply',
            'notification', 'notifications', 'alert', 'alerts', 'update', 'updates',
            'info', 'contact', 'support', 'hello', 'welcome', 'service',
            'system', 'mail', 'email', 'inbox', 'post', 'office', 'admin', 'hr'
        }
        
        company = "Unknown"
        sender_name = ""
        email_addr = sender
        if '<' in sender:
            split_parts = sender.split('<')
            sender_name = split_parts[0].strip().replace('"', '')
            email_addr = split_parts[1].replace('>', '').strip()

        match = re.search(r'@([\w.-]+)', email_addr)
        
        is_platform = False
        if match:
            domain = match.group(1).lower()
            if any(p in domain for p in platforms) or domain in ['gmail.com', 'yahoo.com', 'outlook.com']:
                is_platform = True
            else:
                potential = domain.split('.')[0]
                if potential in generic_names or len(potential) <= 2:
                    is_platform = True
                else:
                    company = potential.capitalize()

        if (is_platform or company == "Unknown") and sender_name:
            clean_name = re.sub(r'(?i)\s+(hiring|team|recruiting|careers|jobs|notifications|via).*', '', sender_name).strip()
            if clean_name and clean_name.lower() not in generic_names and len(clean_name) > 2:
                company = clean_name

        if company == "Unknown":
            subject_patterns = [
                (r"(?i) at (.*)", 1),
                (r"(?i) regarding (.*)", 1),
                (r"(?i)Welcome to (.*)", 1),
                (r"(?i)applying to (.*)", 1),
                (r"(?i)Application to (.*)", 1),
                (r"(?i)interest in (.*)", 1),
                (r"(?i)sent to (.*)", 1),
                (r"(?i) bei der (.*)", 1),
                (r"(?i) bei (.*)", 1),
                (r"(?i)Bewerbung auf (.*)", 1),
            ]
            for pat, group in subject_patterns:
                m = re.search(pat, subject)
                if m:
                    candidate = m.group(group).strip()
                    company = re.sub(r'(?i)\s+(application|role|job|position|update|candidates|ist\s+abgeschlossen|received|eingegangen|for\s+the|was\s+sent).*', '', candidate).strip()
                    company = re.sub(r'[^\w\s]', '', company).strip()
                    break

        # Status Detection
        lower_all = (subject + " " + text).lower()
        status = ApplicationStatus.COMMUNICATION # Default to communication for non-unknown
        
        if any(w in lower_all for w in ['reject', 'unfortunately', 'not moving forward', 'not be proceeding', 'other candidates', 'not continuing']):
            status = ApplicationStatus.REJECTED
        elif any(w in lower_all for w in ['interview', 'schedule a call', 'availability', 'phone screen', 'zoom']):
            status = ApplicationStatus.INTERVIEW
        elif any(w in lower_all for w in ['offer', 'congratulations', 'pleased to offer']):
            status = ApplicationStatus.OFFER
        elif any(w in lower_all for w in ['assessment', 'coding challenge', 'take-home', 'test']):
            status = ApplicationStatus.ASSESSMENT
        elif any(w in lower_all for w in ['applied', 'confirming', 'application received', 'successfully submitted']):
            status = ApplicationStatus.APPLIED
            
        return ExtractedData(
            company_name=company,
            position=None,
            status=status,
            summary="Heuristic extraction"
        )

    def _map_status(self, status_str: str) -> ApplicationStatus:
        try:
            return ApplicationStatus(status_str)
        except ValueError:
            s = status_str.lower()
            if 'reject' in s: return ApplicationStatus.REJECTED
            if 'interview' in s: return ApplicationStatus.INTERVIEW
            if 'offer' in s: return ApplicationStatus.OFFER
            if 'apply' in s or 'applied' in s: return ApplicationStatus.APPLIED
            if 'assess' in s: return ApplicationStatus.ASSESSMENT
            return ApplicationStatus.COMMUNICATION
