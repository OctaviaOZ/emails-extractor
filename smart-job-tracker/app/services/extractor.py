import os
import logging
import re
import json
from abc import ABC, abstractmethod
from typing import Optional, Any, Dict, Set, List
from pydantic import BaseModel, Field, model_validator
from app.models import ApplicationStatus
from app.core.config import settings
from app.core.constants import PLATFORM_NAMES, GENERIC_NAMES, GENERIC_DOMAINS

# Import SDKs lazily or with error handling
try:
    import anthropic
except ImportError:
    anthropic = None
try:
    import openai
except ImportError:
    openai = None
try:
    import google.generativeai as genai
except ImportError:
    genai = None

# Try to import llama_cpp for local provider
try:
    from llama_cpp import Llama, LlamaGrammar
    import llama_cpp.llama_chat_format as llama_chat_format
    
    class LenientJinja2ChatFormatter(llama_chat_format.Jinja2ChatFormatter):
        """Custom Jinja2 formatter to handle problematic GGUF templates."""
        def __init__(self, template: str, eos_token: str, bos_token: str, **kwargs):
            clean_template = template.replace("{% generation %}", "").replace("{% endgeneration %}", "")
            try:
                super().__init__(clean_template, eos_token, bos_token, **kwargs)
            except Exception as e:
                logging.getLogger(__name__).warning(f"Failed to parse chat template: {e}. Falling back to ChatML.")
                self._environment = None
                self._template = None
                self.template = "{% for message in messages %}{{'<|im_start|>' + message['role'] + '\n' + message['content'] + '<|im_end|>' + '\n'}}{% endfor %}{{'<|im_start|>assistant\n'}}"
                self.eos_token = eos_token
                self.bos_token = bos_token
                try:
                    if hasattr(llama_chat_format, "jinja2"):
                        self._environment = llama_chat_format.jinja2.Environment(
                            loader=llama_chat_format.jinja2.BaseLoader(),
                            undefined=llama_chat_format.jinja2.StrictUndefined
                        )
                        self._template = self._environment.from_string(self.template)
                except Exception:
                    pass

    llama_chat_format.Jinja2ChatFormatter = LenientJinja2ChatFormatter
except ImportError:
    Llama = None

logger = logging.getLogger(__name__)

class ApplicationData(BaseModel):
    """Structured data extracted from a job-related email."""
    company_name: str = Field(description="Name of the employer (e.g., 'Google').")
    position: Optional[str] = Field(default="Unknown Position", description="Job title.")
    status: ApplicationStatus = Field(description="Current application status.")
    summary: Optional[str] = Field(default="No summary provided", description="One-sentence summary.")
    is_rejection: bool = Field(description="Whether the email is a rejection.")
    next_step: Optional[str] = Field(default="Wait for feedback", description="Identified next step.")

    @model_validator(mode='before')
    @classmethod
    def sanitize_input(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Normalize field names
            if 'employer' in data and 'company_name' not in data:
                data['company_name'] = data.pop('employer')
            if 'description' in data and 'summary' not in data:
                data['summary'] = data.pop('description')
            
            # Normalize Status
            status_val = data.get('status')
            if isinstance(status_val, ApplicationStatus):
                pass
            elif status_val:
                status_str = str(status_val).split('.')[-1].upper().strip()
                if status_str in ApplicationStatus.all_values():
                    data['status'] = ApplicationStatus(status_str)
                else:
                    data['status'] = ApplicationStatus.UNKNOWN
            else:
                data['status'] = ApplicationStatus.UNKNOWN
            
            data.setdefault('summary', "No summary provided")
            data.setdefault('is_rejection', False)
            data.setdefault('position', "Unknown Position")
            data.setdefault('next_step', "Wait for feedback")
                
        return data

class LLMProvider(ABC):
    """Base class for LLM-based data extraction."""
    @abstractmethod
    def extract(self, sender: str, subject: str, body: str) -> ApplicationData:
        pass

_SHARED_LLAMA_MODEL: Optional[Any] = None

class LocalProvider(LLMProvider):
    """Local LLM provider using llama-cpp-python."""
    def __init__(self, model_path: str):
        global _SHARED_LLAMA_MODEL
        if not Llama:
            raise ImportError("llama-cpp-python is not installed.")
        
        if _SHARED_LLAMA_MODEL is None:
            _SHARED_LLAMA_MODEL = self._initialize_model(model_path)
        self.llm = _SHARED_LLAMA_MODEL

    def _initialize_model(self, model_path: str) -> Any:
        logger.info(f"Loading Llama model from {model_path}...")
        kwargs = {
            "model_path": model_path,
            "n_ctx": 3072,
            "n_threads": 4,
            "n_gpu_layers": 0,
            "n_batch": 128,
            "verbose": False
        }
        
        if "smollm" in os.path.basename(model_path).lower():
            kwargs["chat_format"] = "chatml"

        try:
            return Llama(**kwargs)
        except Exception as e:
            logger.warning(f"Failed to load model: {e}. Attempting fallback...")
            return self._load_fallback()

    def _load_fallback(self) -> Any:
        # Use settings base dir
        fallback_path = os.path.join(settings.base_dir, "models", "Llama-3.2-3B-Instruct-Q4_K_M.gguf")
        
        if not os.path.exists(fallback_path):
            raise FileNotFoundError(f"Fallback model not found at {fallback_path}")

        return Llama(
            model_path=fallback_path,
            n_ctx=2048,
            n_threads=2,
            n_gpu_layers=0,
            n_batch=64,
            verbose=False
        )

    def _parse_json(self, text: str) -> Dict[str, Any]:
        """Robustly extracts JSON from LLM output."""
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            pass

        match = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                pass

        match = re.search(r'(\{.*\})', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                pass
            
        raise ValueError(f"Could not parse JSON from text: {text[:100]}...")

    def extract(self, sender: str, subject: str, body: str) -> ApplicationData:
        system_prompt = (
            "You are a professional recruitment assistant. Extract structured job application data in JSON format.\n"
            "IGNORE platform names (Workday, Greenhouse, etc.). Use the actual employer name.\n"
            "Statuses: [Applied, Interview, Assessment, Offer, Rejected, Pending].\n"
            "Respond ONLY with valid JSON."
        )
        user_content = f"Sender: {sender}\nSubject: {subject}\nBody: {body[:3000]}"
        
        schema_json = json.dumps(ApplicationData.model_json_schema())
        grammar = LlamaGrammar.from_json_schema(schema_json)
        
        response = self.llm.create_chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            max_tokens=settings.ai.max_tokens,
            temperature=settings.ai.temperature,
            grammar=grammar
        )
        
        text = response['choices'][0]['message']['content']
        data = self._parse_json(text)
        return ApplicationData(**data)

class ClaudeProvider(LLMProvider):
    def __init__(self, api_key: str):
        if not anthropic:
            raise ImportError("anthropic is not installed.")
        self.client = anthropic.Anthropic(api_key=api_key)

    def extract(self, sender: str, subject: str, body: str) -> ApplicationData:
        prompt = f"Extract job application data. Ignore platforms.\nSender: {sender}\nSubject: {subject}\nBody: {body[:10000]}"
        response = self.client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=1024,
            tools=[{
                "name": "extract_job_data",
                "description": "Extract job application data",
                "input_schema": ApplicationData.model_json_schema()
            }],
            tool_choice={"type": "tool", "name": "extract_job_data"},
            messages=[{"role": "user", "content": prompt}]
        )
        if response.content and response.content[0].type == 'tool_use':
             return ApplicationData(**response.content[0].input)
        raise ValueError("Claude extraction failed")

class OpenAIProvider(LLMProvider):
    def __init__(self, api_key: str):
        if not openai:
            raise ImportError("openai is not installed.")
        self.client = openai.OpenAI(api_key=api_key)

    def extract(self, sender: str, subject: str, body: str) -> ApplicationData:
        completion = self.client.beta.chat.completions.parse(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Extract employer and status precisely."},
                {"role": "user", "content": f"Sender: {sender}\nSubject: {subject}\nBody: {body[:8000]}"}
            ],
            response_format=ApplicationData,
        )
        return completion.choices[0].message.parsed

class GeminiProvider(LLMProvider):
    def __init__(self, api_key: str):
        if not genai:
            raise ImportError("google-generativeai is not installed.")
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel('gemini-2.0-flash')

    def extract(self, sender: str, subject: str, body: str) -> ApplicationData:
        prompt = f"Extract job data JSON. Ignore platforms.\nEmail: {sender} | {subject} | {body[:8000]}"
        response = self.model.generate_content(
            prompt,
            generation_config={"response_mime_type": "application/json"}
        )
        return ApplicationData(**json.loads(response.text))

class EmailExtractor:
    """Orchestrates data extraction using multiple LLM providers with failover."""
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        # config is now largely unused but kept for interface compatibility if needed
        self.providers: List[LLMProvider] = []
        self.failed_providers: Set[str] = set()
        
        self._init_providers()

    def _init_providers(self):
        # 1. Local Provider
        model_name = settings.ai.local_model_name
        model_path = os.path.join(settings.base_dir, "models", model_name)
        
        if os.path.exists(model_path):
            try:
                self.providers.append(LocalProvider(model_path))
            except Exception as e:
                logger.error(f"Failed to init Local Provider: {e}")

        # 2. Cloud Providers
        if settings.anthropic_api_key:
            self.providers.append(ClaudeProvider(settings.anthropic_api_key))
        if settings.google_api_key:
            self.providers.append(GeminiProvider(settings.google_api_key))
        if settings.openai_api_key:
            self.providers.append(OpenAIProvider(settings.openai_api_key))

    def extract(self, subject: str, sender: str, body_text: str, body_html: str = "") -> ApplicationData:
        effective_body = body_html if body_html else body_text
        
        for provider in self.providers:
            provider_name = provider.__class__.__name__
            if provider_name in self.failed_providers:
                continue

            try:
                result = provider.extract(sender, subject, effective_body)
                result = self._refine_company(result, sender, effective_body)
                return self._refine_status(result, subject, effective_body)
            except Exception as e:
                err_msg = str(e).lower()
                if any(x in err_msg for x in ["quota", "429", "403", "limit"]):
                    logger.warning(f"Quota exceeded for {provider_name}. Skipping.")
                    self.failed_providers.add(provider_name)
                else:
                    logger.warning(f"Provider {provider_name} failed: {e}")
                continue
        
        logger.error("All AI providers failed. Falling back to heuristics.")
        result = self._extract_heuristic(subject, sender, body_text)
        return self._refine_status(result, subject, body_text)

    def _refine_company(self, data: ApplicationData, sender: str, text: str) -> ApplicationData:
        platforms = settings.extraction.platforms
        
        if any(p.lower() in data.company_name.lower() for p in platforms):
            data.company_name = "Unknown"

        if data.company_name == "Unknown":
            heuristic = self._extract_heuristic("", sender, text)
            if heuristic.company_name != "Unknown":
                data.company_name = heuristic.company_name
        
        if any(p.lower() in data.company_name.lower() for p in platforms):
            data.company_name = "Unknown"
            
        return data

    def _refine_status(self, data: ApplicationData, subject: str, text: str) -> ApplicationData:
        search_text = (subject + " " + text).lower()
        
        if any(w.lower() in search_text for w in settings.status_keywords.rejected):
            data.status = ApplicationStatus.REJECTED
            data.is_rejection = True
            return data

        if any(w.lower() in search_text for w in settings.status_keywords.assessment):
            data.status = ApplicationStatus.ASSESSMENT
            return data

        applied_kws = settings.status_keywords.applied
        if any(w.lower() in search_text for w in applied_kws):
            weak_statuses = [ApplicationStatus.PENDING, ApplicationStatus.COMMUNICATION, ApplicationStatus.UNKNOWN]
            if data.status in weak_statuses:
                data.status = ApplicationStatus.APPLIED
            return data

        if data.status not in [ApplicationStatus.APPLIED, ApplicationStatus.PENDING, ApplicationStatus.COMMUNICATION, ApplicationStatus.UNKNOWN]:
            return data

        if any(w.lower() in search_text for w in settings.status_keywords.offer):
            data.status = ApplicationStatus.OFFER
            return data

        if any(w.lower() in search_text for w in settings.status_keywords.interview):
            data.status = ApplicationStatus.INTERVIEW
            return data

        return data

    def _extract_heuristic(self, subject: str, sender: str, text: str) -> ApplicationData:
        platforms = set(settings.extraction.platforms)
        ignore_names = set(settings.extraction.generic_names)
        # Add constants if not already in config
        ignore_names.update(GENERIC_NAMES)
        ignore_names.update(platforms)
        
        company = "Unknown"
        sender_name = ""
        email_addr = sender
        if '<' in sender:
            parts = sender.split('<')
            sender_name = parts[0].strip().replace('"', '')
            email_addr = parts[1].replace('>', '').strip()

        domain = ""
        if '@' in email_addr:
            domain = email_addr.split('@')[1].lower()

        is_platform = any(p.lower() in domain for p in platforms) or domain in GENERIC_DOMAINS or any(p in domain for p in PLATFORM_NAMES)

        if (is_platform or company == "Unknown") and sender_name:
            clean_name = re.sub(r'(?i)\s+(hiring|team|recruiting|careers|jobs|notifications|via|bewerbermanagement|career|system|hr).*', '', sender_name).strip()
            if clean_name and clean_name.lower() not in [n.lower() for n in ignore_names]:
                company = clean_name

        if company == "Unknown" and text:
            body_match = re.search(r'(?i)\s+at\s+([A-Z][A-Za-z0-9\s&]{2,50})(?:\s+Corporate|SE|GmbH|AG|Inc|\.|\s|$)', text[:500])
            if body_match:
                potential = body_match.group(1).strip()
                if potential.lower() not in [n.lower() for n in ignore_names] and not any(p.lower() in potential.lower() for p in platforms):
                    company = potential

        if company == "Unknown":
            for entry in settings.extraction.subject_patterns:
                m = re.search(entry.get('pattern', ''), subject)
                if m:
                    res = m.group(entry.get('group', 1)).strip()
                    res = re.sub(r'(?i)\s+(application|role|job|position|update).*', '', res).strip()
                    if res.lower() not in [n.lower() for n in ignore_names]:
                        company = res
                        break

        return ApplicationData(
            company_name=company, 
            status=ApplicationStatus.APPLIED, 
            summary="Extracted via heuristics",
            is_rejection=False
        )
