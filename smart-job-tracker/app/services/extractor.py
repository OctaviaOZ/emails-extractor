import os
import json
import logging
import time
from typing import Optional, List, Type
from abc import ABC, abstractmethod

from pydantic import BaseModel, Field
from app.models import ApplicationStatus
from tenacity import retry, stop_after_attempt, wait_exponential
import re

# LLM SDKs
import anthropic
import openai
import google.generativeai as genai

logger = logging.getLogger(__name__)

class ExtractedData(BaseModel):
    company_name: str
    position: Optional[str] = None
    status: ApplicationStatus
    summary: str

class BaseLLMProvider(ABC):
    """Abstract base class for all LLM providers."""
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.last_failure_time = 0
        self.cooldown_period = 300  # 5 minutes

    @property
    def is_available(self) -> bool:
        return (time.time() - self.last_failure_time) > self.cooldown_period

    def mark_failure(self):
        self.last_failure_time = time.time()

    @abstractmethod
    def extract(self, prompt: str) -> ExtractedData:
        pass

class ClaudeProvider(BaseLLMProvider):
    def extract(self, prompt: str) -> ExtractedData:
        client = anthropic.Anthropic(api_key=self.api_key)
        # Using Claude's tool-use for guaranteed structured output
        response = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=1000,
            tools=[{
                "name": "record_job_data",
                "description": "Save job application details",
                "input_schema": ExtractedData.model_json_schema()
            }],
            tool_choice={"type": "tool", "name": "record_job_data"},
            messages=[{"role": "user", "content": prompt}]
        )
        
        # Check if any tool was used
        if response.content and response.content[0].type == 'tool_use':
             data = response.content[0].input
             return ExtractedData(**data)
        
        # Fallback if no tool called (unlikely with tool_choice forced)
        raise ValueError("Claude did not return a tool use response")

class OpenAIProvider(BaseLLMProvider):
    def extract(self, prompt: str) -> ExtractedData:
        client = openai.OpenAI(api_key=self.api_key)
        # Using native structured output (JSON schema)
        completion = client.beta.chat.completions.parse(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a recruitment data extractor."}, 
                {"role": "user", "content": prompt}
            ],
            response_format=ExtractedData,
        )
        return completion.choices[0].message.parsed

class GeminiProvider(BaseLLMProvider):
    def extract(self, prompt: str) -> ExtractedData:
        genai.configure(api_key=self.api_key)
        model = genai.GenerativeModel('gemini-2.0-flash')
        
        # Gemini doesn't always support strict Pydantic parsing in the SDK same way as OpenAI's beta yet,
        # but we can use response_mime_type="application/json" and manual parsing as a robust enough method
        # or use the new generation_config schema if available. 
        # For compatibility, we'll stick to JSON mode and validation. 
        
        response = model.generate_content(
            prompt + "\n\nReturn JSON only complying with the schema: " + json.dumps(ExtractedData.model_json_schema()),
            generation_config=genai.types.GenerationConfig(
                response_mime_type="application/json",
            ),
        )
        
        try:
            data = json.loads(response.text)
            # Ensure status is valid
            if "status" in data:
                 # Map string to enum if needed (though Pydantic does this)
                 pass
            return ExtractedData(**data)
        except Exception as e:
             logger.error(f"Gemini parsing failed: {e}. Text: {response.text}")
             raise e

class EmailExtractor:
    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        self.providers = self._init_providers()
        
    def _init_providers(self) -> List[BaseLLMProvider]:
        providers = []
        # Priority 1: OpenAI
        if key := os.getenv("OPENAI_API_KEY"):
            providers.append(OpenAIProvider(key))
        # Priority 2: Claude
        if key := os.getenv("ANTHROPIC_API_KEY"):
            providers.append(ClaudeProvider(key))
        # Priority 3: Gemini
        if key := os.getenv("GOOGLE_API_KEY"):
            providers.append(GeminiProvider(key))
            
        if not providers:
            logger.warning("No LLM API keys found. Extraction will rely solely on heuristics.")
            
        return providers

    def extract(self, subject: str, sender: str, body_text: str, body_html: str) -> ExtractedData:
        prompt = self._build_prompt(subject, sender, body_text)
        
        # 1. Try LLM Providers in order of priority
        for provider in self.providers:
            if provider.is_available:
                try:
                    logger.info(f"Attempting extraction with {type(provider).__name__}")
                    return provider.extract(prompt)
                except Exception as e:
                    logger.warning(f"Provider {type(provider).__name__} failed: {e}")
                    provider.mark_failure()
                    continue # Try next provider
        
        # 2. Final Fallback: Heuristic Extraction (Zero Cost)
        logger.info("All LLMs unavailable or failed. Falling back to heuristics.")
        return self._extract_heuristic(subject, sender, body_text, body_html)

    def _build_prompt(self, subject: str, sender: str, text: str) -> str:
        # Advanced cleaning: Remove excessive whitespace and limit length
        clean_text = " ".join(text.split())[:3500]
        return f"""
        Extract job application data. 
        Sender: {sender}
        Subject: {subject}
        Content: {clean_text}

        Rules:
        - The 'company_name' must be the employer, not the platform (e.g., use 'Tesla' not 'Workday').
        - 'status' must be one of: {', '.join([s.value for s in ApplicationStatus])}.
        - 'summary' should be a single, professional sentence.
        """

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