import os
import logging
import re
from abc import ABC, abstractmethod
from typing import Optional
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential
from app.models import ApplicationStatus

# Import SDKs
import anthropic
import openai
import google.generativeai as genai

logger = logging.getLogger(__name__)

# --- Structured Output Schema ---
class ApplicationData(BaseModel):
    company_name: str = Field(description="Name of the employer (e.g., 'Google'). IGNORE platforms like 'Workday'.")
    position: Optional[str] = Field(description="Job title if mentioned")
    status: ApplicationStatus = Field(description="Current status based on email content")
    summary: str = Field(description="A concise, professional summary of the email content")
    is_rejection: bool = Field(description="True if this specific email is a rejection")
    next_step: Optional[str] = Field(description="Immediate next step e.g. 'Wait for feedback'")

# --- Base Provider ---
class LLMProvider(ABC):
    @abstractmethod
    def extract(self, sender: str, subject: str, body: str) -> ApplicationData:
        pass

# --- Claude Implementation (Best Reasoning) ---
class ClaudeProvider(LLMProvider):
    def __init__(self, api_key):
        self.client = anthropic.Anthropic(api_key=api_key)

    def extract(self, sender: str, subject: str, body: str) -> ApplicationData:
        prompt = f"Analyze this job email.\nSender: {sender}\nSubject: {subject}\nBody: {body[:15000]}" # Claude supports large context
        
        response = self.client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=1024,
            tools=[{
                "name": "extract_job_data",
                "description": "Extract structured job application data",
                "input_schema": ApplicationData.model_json_schema()
            }],
            tool_choice={"type": "tool", "name": "extract_job_data"},
            messages=[{"role": "user", "content": prompt}]
        )
        # Check if any tool was used
        if response.content and response.content[0].type == 'tool_use':
             data = response.content[0].input
             return ApplicationData(**data)
        raise ValueError("Claude did not return a tool use response")

# --- OpenAI Implementation (Fast & Cheap) ---
class OpenAIProvider(LLMProvider):
    def __init__(self, api_key):
        self.client = openai.OpenAI(api_key=api_key)

    def extract(self, sender: str, subject: str, body: str) -> ApplicationData:
        completion = self.client.beta.chat.completions.parse(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are an expert recruiter AI. Extract the employer name precisely."},
                {"role": "user", "content": f"Sender: {sender}\nSubject: {subject}\nBody: {body[:8000]}"}
            ],
            response_format=ApplicationData,
        )
        return completion.choices[0].message.parsed

# --- Gemini Implementation (High Volume/Free Tier) ---
class GeminiProvider(LLMProvider):
    def __init__(self, api_key):
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel('gemini-2.0-flash')

    def extract(self, sender: str, subject: str, body: str) -> ApplicationData:
        prompt = f"""
        Extract job data in JSON matching this schema:
        {{
            "company_name": str,
            "position": str (nullable),
            "status": str (Applied, Interview, Assessment, Offer, Rejected, Communication),
            "summary": str,
            "is_rejection": bool,
            "next_step": str (nullable)
        }}
        Email: {sender} | {subject} | {body[:8000]}
        """
        response = self.model.generate_content(
            prompt,
            generation_config={"response_mime_type": "application/json"}
        )
        import json
        return ApplicationData(**json.loads(response.text))

# --- Main Class ---
class EmailExtractor:
    def __init__(self, config: dict = None):
        self.config = config or {}
        self.providers = []
        
        # Load Providers (Priority Order: Claude -> OpenAI -> Gemini)
        if k := os.getenv("ANTHROPIC_API_KEY"):
            self.providers.append(ClaudeProvider(k))
        if k := os.getenv("OPENAI_API_KEY"):
            self.providers.append(OpenAIProvider(k))
        if k := os.getenv("GOOGLE_API_KEY"):
            self.providers.append(GeminiProvider(k))
            
        # Circuit Breaker state
        self.consecutive_ai_failures = 0
        self.max_failures = 3 # Stop trying AI after 3 consecutive failures

    def extract(self, subject: str, sender: str, body_text: str, body_html: str = "") -> ApplicationData:
        # Check Circuit Breaker
        if self.consecutive_ai_failures >= self.max_failures:
            if self.consecutive_ai_failures == self.max_failures:
                logger.warning("Circuit breaker tripped: Too many consecutive AI failures. Switching to heuristics for remaining emails.")
                self.consecutive_ai_failures += 1 # Increment once more to avoid repeated logging
            return self._extract_heuristic(subject, sender, body_text)

        # Try AI Providers
        success = False
        for provider in self.providers:
            try:
                result = provider.extract(sender, subject, body_text)
                self.consecutive_ai_failures = 0 # Reset on success
                success = True
                return result
            except Exception as e:
                logger.warning(f"Provider {provider.__class__.__name__} failed: {e}")
                continue
        
        # If we reach here, all providers failed for this email
        self.consecutive_ai_failures += 1
        logger.error(f"All AI providers failed (Failure count: {self.consecutive_ai_failures}). Falling back to Heuristics.")
        return self._extract_heuristic(subject, sender, body_text)

    def _extract_heuristic(self, subject: str, sender: str, text: str) -> ApplicationData:
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
            
        return ApplicationData(
            company_name=company, 
            status=status, 
            summary=f"Heuristic summary: {status}",
            is_rejection=(status == ApplicationStatus.REJECTED),
            position=None,
            next_step=None
        )
