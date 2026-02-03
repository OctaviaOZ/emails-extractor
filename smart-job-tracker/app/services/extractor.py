import os
import logging
import re
from abc import ABC, abstractmethod
from typing import Optional, Any
from pydantic import BaseModel, Field, model_validator
from tenacity import retry, stop_after_attempt, wait_exponential
from app.models import ApplicationStatus

# Import SDKs
import anthropic
import openai
import google.generativeai as genai

# Try to import llama_cpp for local provider
try:
    from llama_cpp import Llama
except ImportError:
    Llama = None

logger = logging.getLogger(__name__)

# --- Structured Output Schema ---
class ApplicationData(BaseModel):
    company_name: str = Field(description="Name of the employer (e.g., 'Google'). IGNORE platforms like 'Workday'.")
    position: Optional[str] = Field(default=None, description="Job title if mentioned")
    status: ApplicationStatus = Field(description="Current status based on email content")
    summary: Optional[str] = Field(default="No summary provided", description="A very short, abstractive summary of the email content (max 15 words). Do NOT quote the email body.")
    is_rejection: bool = Field(description="True if this specific email is a rejection")
    next_step: Optional[str] = Field(default=None, description="Immediate next step e.g. 'Wait for feedback'")

    @model_validator(mode='before')
    @classmethod
    def sanitize_input(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Fix Status
            status_val = data.get('status')
            if not status_val or status_val not in [s.value for s in ApplicationStatus]:
                data['status'] = ApplicationStatus.UNKNOWN
            
            # Fix Summary
            if not data.get('summary'):
                data['summary'] = "No summary extracted"
                
            # Fix Rejection
            if data.get('is_rejection') is None:
                data['is_rejection'] = False

            # Ensure optional fields exist to avoid 'Field required' validation errors
            if 'position' not in data:
                data['position'] = None
            if 'next_step' not in data:
                data['next_step'] = None
                
        return data

# --- Base Provider ---
class LLMProvider(ABC):
    @abstractmethod
    def extract(self, sender: str, subject: str, body: str) -> ApplicationData:
        pass

# --- Local Llama Implementation ---
_SHARED_LLAMA_MODEL = None

class LocalProvider(LLMProvider):
    def __init__(self, model_path):
        global _SHARED_LLAMA_MODEL
        if not Llama:
            raise ImportError("llama-cpp-python is not installed. Run `poetry add llama-cpp-python`.")
        
        # Singleton: Load model only once
        if _SHARED_LLAMA_MODEL is None:
            logger.info(f"Loading Llama model from {model_path}...")
            # Initialize Llama 3.2
            # n_ctx=2048 is safer for 8GB RAM machines (saves ~500MB vs 8192)
            # Optimized for CPU-only (8GB RAM, i3 CPU)
            _SHARED_LLAMA_MODEL = Llama(
                model_path=model_path,
                n_ctx=2048,
                n_threads=2, # Limit threads to prevent CPU starvation
                n_gpu_layers=0, # CPU only
                n_batch=64, # Minimal batch size to prevent bad_alloc
                verbose=False
            )
        else:
            logger.info("Using cached Llama model instance.")

        self.llm = _SHARED_LLAMA_MODEL

    def extract(self, sender: str, subject: str, body: str) -> ApplicationData:
        # Llama 3.2 Instruct Prompt Template
        system_prompt = (
            "You are an expert recruiter AI. Analyze the job email and output ONLY valid JSON.\n"
            "Fields: company_name, position (nullable), status, summary, is_rejection (bool), next_step (nullable).\n"
            "Status MUST be one of: Applied, Interview, Assessment, Offer, Rejected, Pending.\n"
            "IMPORTANT COMPANY IDENTIFICATION:\n"
            "- Look for the actual employer name in the SENDER NAME and EMAIL SIGNATURE (e.g. 'DKB Hiring Team' means company is DKB).\n"
            "- IGNORE generic platform names in the email address (e.g. if sender is 'system@successfactors.eu', the company is NOT Successfactors).\n"
            "- Be precise: 'Richemont Careers' means company is Richemont.\n"
            "IMPORTANT CLASSIFICATION RULES:\n"
            "- 'Rejected': Includes polite rejections, feedback after interview saying 'no', or suggestions to apply for other roles.\n"
            "- 'Assessment': For tests, tasks, Arbeitsproben.\n"
            "- Summary: Short (max 10 words), abstractive, keep same language as email.\n"
            "Respond ONLY with JSON."
        )
        
        # Reduce body length to 3500 chars to fit in 2048 context window (approx 1000 tokens for body)
        user_content = f"Sender: {sender}\nSubject: {subject}\nBody: {body[:3500]}"
        
        # 1. Try with enforced JSON mode
        try:
            response = self.llm.create_chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                max_tokens=512,
                temperature=0.1,
                response_format={"type": "json_object"}, 
                stop=["<|eot_id|>"]
            )
            import json
            text = response['choices'][0]['message']['content']
            text = text.replace("```json", "").replace("```", "").strip()
            data = json.loads(text)
            return ApplicationData(**data)
        except Exception as e:
            logger.warning(f"Local LLM JSON mode failed: {e}. Retrying with loose mode...")

        # 2. Fallback: Loose mode (no enforced JSON, parse with regex)
        try:
            response = self.llm.create_chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt + " Respond ONLY with the JSON object."},
                    {"role": "user", "content": user_content}
                ],
                max_tokens=512,
                temperature=0.1,
                stop=["<|eot_id|>"]
            )
            text = response['choices'][0]['message']['content']
            # Find JSON object in text
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                import json
                json_str = match.group(0)
                data = json.loads(json_str)
                return ApplicationData(**data)
            else:
                raise ValueError("No JSON found in response")
        except Exception as e:
            logger.error(f"Local LLM fallback failed: {e}")
            raise e

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
                {"role": "system", "content": "You are an expert recruiter AI. Extract the employer name precisely. Summarize the content abstractively (do not quote)."},
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
            "status": str (Applied, Interview, Assessment, Offer, Rejected, Pending),
            "summary": str (Short executive summary of the status. Max 15 words. e.g. "Invitation to first round interview"),
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
        
        # 1. Check for Local Model First (Priority)
        # Assumes model is in 'models/' folder relative to project root
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        local_model_path = os.path.join(base_dir, "models", "Llama-3.2-3B-Instruct-Q4_K_M.gguf")
        
        if os.path.exists(local_model_path):
            logger.info(f"Loading Local Llama Model from {local_model_path}...")
            try:
                self.providers.append(LocalProvider(local_model_path))
            except Exception as e:
                logger.error(f"Failed to load Local Provider: {e}")

        # 2. Load Cloud Providers (Fallbacks)
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
                # Refine status with heuristics to catch obvious keywords the model might miss
                return self._refine_status(result, subject, body_text)
            except Exception as e:
                logger.warning(f"Provider {provider.__class__.__name__} failed: {e}")
                continue
        
        # If we reach here, all providers failed for this email
        self.consecutive_ai_failures += 1
        logger.error(f"All AI providers failed (Failure count: {self.consecutive_ai_failures}). Falling back to Heuristics.")
        return self._extract_heuristic(subject, sender, body_text)

    def _refine_status(self, data: ApplicationData, subject: str, text: str) -> ApplicationData:
        """
        Overrides the AI model's status if strong keywords are found in the text/subject.
        Useful when the model is too conservative (e.g., marks 'Assessment Invitation' as just 'Applied')
        or wrong (e.g. marks 'Arbeitsprobe' as 'Offer').
        """
        search_text = (subject + " " + text).lower()
        kw_cfg = self.config.get('status_keywords', {})

        # 1. Rejection always overrides
        if any(w.lower() in search_text for w in kw_cfg.get('rejected', [])):
            data.status = ApplicationStatus.REJECTED
            data.is_rejection = True
            return data

        # 2. Assessment keywords should override anything except rejection
        # This fixes cases where positive assessment feedback is mistaken for an offer
        if any(w.lower() in search_text for w in kw_cfg.get('assessment', [])):
            data.status = ApplicationStatus.ASSESSMENT
            return data

        # For other statuses, only override "weak" or "unknown" ones
        weak_statuses = [ApplicationStatus.APPLIED, ApplicationStatus.PENDING, ApplicationStatus.COMMUNICATION, ApplicationStatus.UNKNOWN]
        if data.status not in weak_statuses:
            return data

        # 3. Offer
        if any(w.lower() in search_text for w in kw_cfg.get('offer', [])):
            data.status = ApplicationStatus.OFFER
            return data

        # 4. Interview
        if any(w.lower() in search_text for w in kw_cfg.get('interview', [])):
            data.status = ApplicationStatus.INTERVIEW
            return data

        return data

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
        status = ApplicationStatus.PENDING
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