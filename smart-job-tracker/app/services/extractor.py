import re
import os
import json
import logging
from typing import Optional
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
from models import ApplicationStatus
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)

class ExtractedData(BaseModel):
    company_name: str
    position: Optional[str] = None
    status: ApplicationStatus
    summary: str

class EmailExtractor:
    def __init__(self, config: Optional[dict] = None, openai_api_key: Optional[str] = None, gemini_api_key: Optional[str] = None):
        self.config = config or {}
        self.openai_key = openai_api_key or os.getenv("OPENAI_API_KEY")
        self.gemini_key = gemini_api_key or os.getenv("GOOGLE_API_KEY")
        
        # Handle empty strings from .env
        if not self.openai_key: self.openai_key = None
        if not self.gemini_key: self.gemini_key = None
        
        # Session-level flags to skip AI if quota is hit
        self.skip_openai = False
        self.skip_gemini = False

    def extract(self, subject: str, sender: str, body_text: str, body_html: str) -> ExtractedData:
        # Priority 1: OpenAI (if not skipped)
        if self.openai_key and not self.skip_openai:
            try:
                return self._extract_with_openai(subject, sender, body_text)
            except Exception as e:
                if "insufficient_quota" in str(e) or "429" in str(e):
                    logger.warning("OpenAI quota hit. Switching to fallback for this session.")
                    self.skip_openai = True
                else:
                    logger.warning(f"OpenAI extraction failed: {e}. Falling back.")
        
        # Priority 2: Gemini (if not skipped)
        if self.gemini_key and not self.skip_gemini:
             try:
                return self._extract_with_gemini(subject, sender, body_text)
             except Exception as e:
                if "429" in str(e):
                    logger.warning("Gemini quota hit. Switching to fallback for this session.")
                    self.skip_gemini = True
                else:
                    logger.warning(f"Gemini extraction failed: {e}. Falling back.")

        # Priority 3: Heuristic Extraction (Fast, Free)
        return self._extract_heuristic(subject, sender, body_text, body_html)

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=2, max=5),
        reraise=True
    )
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
        
        Extract:
        1. "company_name": The employer name.
        2. "position": The job title (if mentioned).
        3. "status": One of: 'Applied', 'Communication', 'Interview', 'Assessment', 'Offer', 'Rejected'.
        4. "summary": A very short (one sentence) summary of the current state.
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
            summary=data.get("summary", f"AI Summary: {status_str}")
        )

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=2, max=5),
        reraise=True
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
        4. "summary": A very short summary of the message.
        """
        
        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                response_mime_type="application/json",
            ),
        )
        
        data = json.loads(response.text)
        status_str = data.get("status", "Unknown")
        status = self._map_status(status_str)

        return ExtractedData(
            company_name=data.get("company_name", "Unknown"),
            position=data.get("position"),
            status=status,
            summary=data.get("summary", f"AI Summary (G): {status_str}")
        )

    def _extract_heuristic(self, subject: str, sender: str, text: str, html: str) -> ExtractedData:
        extraction_cfg = self.config.get('extraction', {})
        platforms = extraction_cfg.get('platforms', [])
        generic_names = set(extraction_cfg.get('generic_names', []))
        
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

        # Better company detection from sender name
        if (is_platform or company == "Unknown") and sender_name:
            clean_name = re.sub(r'(?i)\s+(hiring|team|recruiting|careers|jobs|notifications|via|bewerbermanagement|career).*', '', sender_name).strip()
            if clean_name and clean_name.lower() not in generic_names and len(clean_name) > 2:
                company = clean_name

        # Better company detection from Body Signatures
        if company == "Unknown" and text:
            # Look for common German closing patterns
            closing_match = re.search(r'(?i)(Mit freundlichen Grüßen|Herzliche Grüße|Best regards|Viele Grüße|Greetings)\s*(?:[\r\n]+.*?){1,2}[\r\n]+(.*?)(?:[\r\n]|$)', text[-1000:])
            if closing_match:
                potential_company = closing_match.group(2).strip()
                if potential_company and len(potential_company) > 2 and len(potential_company) < 50:
                    company = potential_company

        if company == "Unknown":
            subject_patterns = extraction_cfg.get('subject_patterns', [])
            for entry in subject_patterns:
                pat = entry.get('pattern')
                group = entry.get('group', 1)
                if not pat: continue
                m = re.search(pat, subject)
                if m:
                    candidate = m.group(group).strip()
                    company = re.sub(r'(?i)\s+(application|role|job|position|update|candidates|ist\s+abgeschlossen|received|eingegangen|for\s+the|was\s+sent).*', '', candidate).strip()
                    company = re.sub(r'[^\w\s]', '', company).strip()
                    break

        # Status Detection
        search_text = (subject + " " + text).lower()
        status = ApplicationStatus.COMMUNICATION
        kw_cfg = self.config.get('status_keywords', {})
        
        if any(w.lower() in search_text for w in kw_cfg.get('rejected', [])):
            status = ApplicationStatus.REJECTED
        elif any(w.lower() in search_text for w in kw_cfg.get('offer', [])):
            status = ApplicationStatus.OFFER
        elif any(w.lower() in search_text for w in kw_cfg.get('interview', [])):
            status = ApplicationStatus.INTERVIEW
        elif any(w.lower() in search_text for w in kw_cfg.get('assessment', [])):
            status = ApplicationStatus.ASSESSMENT
        elif any(w.lower() in search_text for w in kw_cfg.get('applied', [])):
            status = ApplicationStatus.APPLIED
            
        clean_subject = subject.replace("Re:", "").replace("Aw:", "").strip()
        summary_map = {
            ApplicationStatus.APPLIED: f"Application confirmed: {clean_subject}",
            ApplicationStatus.REJECTED: f"Application rejected or closed: {clean_subject}",
            ApplicationStatus.INTERVIEW: f"Interview/Meeting related: {clean_subject}",
            ApplicationStatus.OFFER: f"Job offer received: {clean_subject}",
            ApplicationStatus.ASSESSMENT: f"Assessment/Test task: {clean_subject}",
            ApplicationStatus.COMMUNICATION: f"General update: {clean_subject}"
        }
        summary = summary_map.get(status, f"Interaction: {clean_subject}")

        return ExtractedData(
            company_name=company,
            position=None,
            status=status,
            summary=summary
        )

    def _map_status(self, status_str: str) -> ApplicationStatus:
        try:
            return ApplicationStatus(status_str)
        except ValueError:
            s = status_str.lower()
            kw_cfg = self.config.get('status_keywords', {})
            if any(w.lower() in s for w in kw_cfg.get('rejected', [])): return ApplicationStatus.REJECTED
            if any(w.lower() in s for w in kw_cfg.get('interview', [])): return ApplicationStatus.INTERVIEW
            if any(w.lower() in s for w in kw_cfg.get('offer', [])): return ApplicationStatus.OFFER
            if any(w.lower() in s for w in kw_cfg.get('applied', [])): return ApplicationStatus.APPLIED
            if any(w.lower() in s for w in kw_cfg.get('assessment', [])): return ApplicationStatus.ASSESSMENT
            return ApplicationStatus.COMMUNICATION
