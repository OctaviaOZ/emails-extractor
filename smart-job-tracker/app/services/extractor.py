import os
import logging
from abc import ABC, abstractmethod
from typing import Optional, List
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential
from app.models import ApplicationStatus

# APIs
import anthropic
import openai
import google.generativeai as genai

logger = logging.getLogger(__name__)

# --- Structured Output Schema ---
class ApplicationData(BaseModel):
    company_name: str = Field(description="Name of the employer (not the platform)")
    position: Optional[str] = Field(description="Job title if mentioned")
    status: ApplicationStatus = Field(description="Current status based on email content")
    summary: str = Field(description="A concise, professional summary of the email content")
    is_rejection: bool = Field(description="True if this specific email is a rejection")
    next_step: Optional[str] = Field(description="What is the immediate next step? e.g. 'Wait for feedback', 'Book interview'")

# --- Provider Pattern ---
class LLMProvider(ABC):
    @abstractmethod
    def extract(self, sender: str, subject: str, body: str) -> ApplicationData:
        pass

class ClaudeProvider(LLMProvider):
    def __init__(self, api_key):
        self.client = anthropic.Anthropic(api_key=api_key)

    def extract(self, sender: str, subject: str, body: str) -> ApplicationData:
        prompt = f"Analyze this job email.\nSender: {sender}\nSubject: {subject}\nBody: {body[:8000]}"
        
        response = self.client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=1000,
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

class OpenAIProvider(LLMProvider):
    def __init__(self, api_key):
        self.client = openai.OpenAI(api_key=api_key)

    def extract(self, sender: str, subject: str, body: str) -> ApplicationData:
        completion = self.client.beta.chat.completions.parse(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are an expert recruiter AI."},
                {"role": "user", "content": f"Sender: {sender}\nSubject: {subject}\nBody: {body[:8000]}"}
            ],
            response_format=ApplicationData,
        )
        return completion.choices[0].message.parsed

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
        data = json.loads(response.text)
        return ApplicationData(**data)

class EmailExtractor:
    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        self.providers = []
        if k := os.getenv("ANTHROPIC_API_KEY"): self.providers.append(ClaudeProvider(k))
        if k := os.getenv("OPENAI_API_KEY"): self.providers.append(OpenAIProvider(k))
        if k := os.getenv("GOOGLE_API_KEY"): self.providers.append(GeminiProvider(k))

    def extract(self, subject: str, sender: str, body_text: str, body_html: str = "") -> ApplicationData:
        # body_html is accepted but not used by current providers to keep signature compatible if needed
        for provider in self.providers:
            try:
                return provider.extract(sender, subject, body_text)
            except Exception as e:
                logger.error(f"Provider {provider.__class__.__name__} failed: {e}")
                continue
        
        # If all LLMs fail, we could have a heuristic fallback here.
        # For now, per the new recommendation, we raise if AI fails or return a dummy/heuristic.
        # But let's re-add a basic heuristic to avoid crashing if requested.
        # However, the user's code snippet for this turn specifically ends with "raise Exception".
        # I will follow the user's snippet logic but perhaps wrap it slightly to be safe.
        raise Exception("All AI providers failed")
