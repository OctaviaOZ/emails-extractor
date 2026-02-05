import os
import logging
import re
import json
import time
import gc
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
    from llama_cpp import Llama, LlamaGrammar
    import llama_cpp.llama_chat_format as llama_chat_format
    
    # --- MONKEY PATCH FOR BROKEN GGUF TEMPLATES ---
    # Many newer models (like SmolLM3) use custom Jinja tags (e.g. 'generation') 
    # that standard jinja2 environments don't recognize, causing a crash on load.
    # We patch the Formatter to be lenient and fallback to ChatML if metadata is broken.
    class LenientJinja2ChatFormatter(llama_chat_format.Jinja2ChatFormatter):
        def __init__(self, template: str, eos_token: str, bos_token: str, **kwargs):
            try:
                super().__init__(template, eos_token, bos_token, **kwargs)
            except Exception as e:
                logging.getLogger(__name__).warning(f"Failed to parse model chat template: {e}. Falling back to generic ChatML.")
                # Fallback template (ChatML style)
                self._environment = None
                self._template = None
                self.template = "{% for message in messages %}{{'<|im_start|>' + message['role'] + '\n' + message['content'] + '<|im_end|>' + '\n'}}{% endfor %}{{'<|im_start|>assistant\n'}}"
                self.eos_token = eos_token
                self.bos_token = bos_token
                # Try to init with safe values, ignoring extra args that might have caused issues if they were template-related
                try:
                    # We manually set the attributes that super().__init__ would set, to avoid re-triggering the failure
                    if hasattr(llama_chat_format, "jinja2"):
                        self._environment = llama_chat_format.jinja2.Environment(
                            loader=llama_chat_format.jinja2.BaseLoader(),
                            undefined=llama_chat_format.jinja2.StrictUndefined
                        )
                        self._template = self._environment.from_string(self.template)
                except:
                    pass # Absolute worst case

    # Apply the patch
    llama_chat_format.Jinja2ChatFormatter = LenientJinja2ChatFormatter
    
except ImportError:
    Llama = None

logger = logging.getLogger(__name__)

# --- Structured Output Schema ---
class ApplicationData(BaseModel):
    company_name: str = Field(description="Name of the employer (e.g., 'Google'). IGNORE platforms like 'Workday', 'Successfactors', 'Greenhouse'.")
    position: Optional[str] = Field(default=None, description="Job title if mentioned")
    status: ApplicationStatus = Field(description="Current status based on email content")
    summary: Optional[str] = Field(default="No summary provided", description="A very short, abstractive summary of the email content (max 15 words). Do NOT quote the email body.")
    is_rejection: bool = Field(description="True if this specific email is a rejection")
    next_step: Optional[str] = Field(default=None, description="Immediate next step e.g. 'Wait for feedback'")

    @model_validator(mode='before')
    @classmethod
    def sanitize_input(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Fix field names if LLM is inconsistent
            if 'employer' in data and 'company_name' not in data:
                data['company_name'] = data.pop('employer')
            
            if 'description' in data and 'summary' not in data:
                data['summary'] = data.pop('description')
            
            # Fix Status
            status_val = data.get('status')
            if status_val:
                status_upper = str(status_val).upper().strip()
                valid_values = [s.value for s in ApplicationStatus]
                if status_upper in valid_values:
                    data['status'] = ApplicationStatus(status_upper)
                else:
                    data['status'] = ApplicationStatus.UNKNOWN
            else:
                data['status'] = ApplicationStatus.UNKNOWN
            
            # Fix Summary
            if not data.get('summary') or data.get('summary') == "No summary extracted":
                data['summary'] = None # Allow field required fallback logic
                
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
            raise ImportError("llama-cpp-python is not installed.")
        
        if _SHARED_LLAMA_MODEL is None:
            logger.info(f"Loading Llama model from {model_path}...")
            
            # Base configuration
            kwargs = {
                "model_path": model_path,
                "n_ctx": 4096, # Increased context for better extraction
                "n_threads": 4, # Slightly more threads for speed, safe for most laptops
                "n_gpu_layers": 0,
                "n_batch": 512, # Increased batch for better throughput
                "verbose": False
            }
            
            # Proactive config fix for SmolLM
            if "smollm" in os.path.basename(model_path).lower():
                logger.info("Detected SmolLM model. Forcing 'chatml' format.")
                kwargs["chat_format"] = "chatml"

            try:
                _SHARED_LLAMA_MODEL = Llama(**kwargs)
                
            except Exception as e:
                # Catch-all for loading failures (templates, missing files, corrupted GGUF)
                logger.warning(f"Configured model failed to load ({e}).")
                logger.warning("Attempting to fallback to default model 'Llama-3.2-3B-Instruct-Q4_K_M.gguf'...")
                
                # FORCE CLEANUP
                if _SHARED_LLAMA_MODEL:
                    try:
                        _SHARED_LLAMA_MODEL.close()
                    except: pass
                    del _SHARED_LLAMA_MODEL
                _SHARED_LLAMA_MODEL = None
                gc.collect()
                time.sleep(5) # Increased wait time for resources to free
                
                try:
                    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                    fallback_path = os.path.join(base_dir, "models", "Llama-3.2-3B-Instruct-Q4_K_M.gguf")
                    
                    if not os.path.exists(fallback_path):
                        raise FileNotFoundError(f"Fallback model not found at {fallback_path}")

                    # Load fallback with standard safe settings
                    _SHARED_LLAMA_MODEL = Llama(
                        model_path=fallback_path,
                        n_ctx=2048,
                        n_threads=2,
                        n_gpu_layers=0,
                        n_batch=64,
                        verbose=False
                    )
                    logger.info("Successfully loaded fallback model (Llama 3.2).")
                except Exception as e2:
                    logger.error(f"Fallback model failed to load: {e2}")
                    raise e2

        self.llm = _SHARED_LLAMA_MODEL

    def _parse_json(self, text: str) -> dict:
        """Robustly extracts JSON from LLM output."""
        # 1. Try direct parse
        try:
            return json.loads(text.strip())
        except:
            pass

        # 2. Extract from markdown block
        match = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except:
                pass

        # 3. Find first { and last }
        match = re.search(r'(\{.*\})', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except:
                pass
            
        raise ValueError(f"Could not parse JSON from text: {text[:100]}...")

    def extract(self, sender: str, subject: str, body: str) -> ApplicationData:
        system_prompt = (
            "You are a recruitment assistant. Analyze the email and extract data in JSON format.\n"
            "Required fields: company_name (actual employer, NOT the platform), position, status, summary (max 10 words), is_rejection (bool), next_step.\n"
            "Status values: Applied, Interview, Assessment, Offer, Rejected, Pending.\n"
            "Example: If Richemont.NOREPLY@successfactors.eu sends an email about a job at Richemont, the company is 'Richemont'.\n"
            "Respond ONLY with valid JSON."
        )
        
        user_content = f"Sender: {sender}\nSubject: {subject}\nBody: {body[:4000]}" # Increased for better extraction
        
        # Create explicit GBNF grammar from the Pydantic schema
        schema_json = json.dumps(ApplicationData.model_json_schema())
        grammar = LlamaGrammar.from_json_schema(schema_json)
        
        response = self.llm.create_chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            max_tokens=512,
            temperature=0.1,
            grammar=grammar
        )
        
        text = response['choices'][0]['message']['content']
        data = self._parse_json(text)
        return ApplicationData(**data)

# --- Cloud Providers (Claude, OpenAI, Gemini) ---
class ClaudeProvider(LLMProvider):
    def __init__(self, api_key):
        self.client = anthropic.Anthropic(api_key=api_key)

    def extract(self, sender: str, subject: str, body: str) -> ApplicationData:
        prompt = f"Extract employer and status. Ignore platforms like Successfactors.\nSender: {sender}\nSubject: {subject}\nBody: {body[:15000]}"
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
        raise ValueError("Claude failed")

class OpenAIProvider(LLMProvider):
    def __init__(self, api_key):
        self.client = openai.OpenAI(api_key=api_key)

    def extract(self, sender: str, subject: str, body: str) -> ApplicationData:
        completion = self.client.beta.chat.completions.parse(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Extract EMPLOYER (not platform) precisely."},
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
        prompt = f"Extract job data JSON. Ignore platforms.\nEmail: {sender} | {subject} | {body[:8000]}"
        response = self.model.generate_content(
            prompt,
            generation_config={"response_mime_type": "application/json"}
        )
        return ApplicationData(**json.loads(response.text))

# --- Main Class ---
class EmailExtractor:
    def __init__(self, config: dict = None):
        self.config = config or {}
        self.providers = []
        self.failed_providers = set() # Track providers that hit quota limits in this session
        
        # 1. PRIORITY: Local Model (High speed, no quota)
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        
        # Get configured model name
        configured_model = self.config.get('ai', {}).get('local_model_name', "Llama-3.2-3B-Instruct-Q4_K_M.gguf")
        
        # Paths to check
        primary_path = os.path.join(base_dir, "models", configured_model)
        default_model = "Llama-3.2-3B-Instruct-Q4_K_M.gguf"
        fallback_path = os.path.join(base_dir, "models", default_model)
        
        target_path = None
        if os.path.exists(primary_path):
            target_path = primary_path
            logger.info(f"Using configured local model: {configured_model}")
        elif os.path.exists(fallback_path):
            target_path = fallback_path
            logger.warning(f"Configured model '{configured_model}' not found. Falling back to default: {default_model}")
        else:
            logger.warning(f"No local models found in {os.path.join(base_dir, 'models')}")

        if target_path:
            try:
                self.providers.append(LocalProvider(target_path))
                logger.info(f"Local provider initialized successfully.")
            except Exception as e:
                logger.error(f"Failed to init Local Provider: {e}")

        # 2. Cloud Fallbacks (if local fails or isn't found)
        if k := os.getenv("ANTHROPIC_API_KEY"):
            self.providers.append(ClaudeProvider(k))
        if k := os.getenv("GOOGLE_API_KEY"):
            self.providers.append(GeminiProvider(k))
        if k := os.getenv("OPENAI_API_KEY"):
            self.providers.append(OpenAIProvider(k))

    def extract(self, subject: str, sender: str, body_text: str, body_html: str = "") -> ApplicationData:
        for provider in self.providers:
            provider_name = provider.__class__.__name__
            if provider_name in self.failed_providers:
                continue

            try:
                if provider_name == "LocalProvider":
                    logger.info("Starting Local AI processing...")
                
                result = provider.extract(sender, subject, body_text)
                # Success! Now refine the results
                result = self._refine_company(result, sender, body_text)
                return self._refine_status(result, subject, body_text)
            except Exception as e:
                err_msg = str(e).lower()
                # If it's a quota or identity error, mark provider as failed for this session
                if "quota" in err_msg or "429" in err_msg or "403" in err_msg or "limit" in err_msg:
                    logger.warning(f"Quota exceeded for {provider_name}. Skipping for remainder of session.")
                    self.failed_providers.add(provider_name)
                else:
                    logger.warning(f"Provider {provider_name} failed: {e}")
                continue
        
        # Absolute last resort
        logger.error("All AI providers failed. Falling back to heuristics.")
        result = self._extract_heuristic(subject, sender, body_text)
        return self._refine_status(result, subject, body_text)

    def _refine_company(self, data: ApplicationData, sender: str, text: str) -> ApplicationData:
        platforms = self.config.get('extraction', {}).get('platforms', [])
        # If AI returns a platform name, use heuristics to find the real employer
        if any(p.lower() == data.company_name.lower() for p in platforms):
            logger.info(f"AI returned platform '{data.company_name}'. Refining with heuristics.")
            heuristic = self._extract_heuristic("", sender, text)
            if heuristic.company_name != "Unknown":
                data.company_name = heuristic.company_name
        return data

    def _refine_status(self, data: ApplicationData, subject: str, text: str) -> ApplicationData:
        search_text = (subject + " " + text).lower()
        kw_cfg = self.config.get('status_keywords', {})

        if any(w.lower() in search_text for w in kw_cfg.get('rejected', [])):
            data.status = ApplicationStatus.REJECTED
            data.is_rejection = True
            return data

        if any(w.lower() in search_text for w in kw_cfg.get('assessment', [])):
            data.status = ApplicationStatus.ASSESSMENT
            return data

        weak_statuses = [ApplicationStatus.APPLIED, ApplicationStatus.PENDING, ApplicationStatus.COMMUNICATION, ApplicationStatus.UNKNOWN]
        if data.status not in weak_statuses:
            return data

        if any(w.lower() in search_text for w in kw_cfg.get('offer', [])):
            data.status = ApplicationStatus.OFFER
            return data

        if any(w.lower() in search_text for w in kw_cfg.get('interview', [])):
            data.status = ApplicationStatus.INTERVIEW
            return data

        return data

    def _extract_heuristic(self, subject: str, sender: str, text: str) -> ApplicationData:
        extraction_cfg = self.config.get('extraction', {})
        platforms = set(extraction_cfg.get('platforms', []))
        ignore_names = set(extraction_cfg.get('generic_names', []))
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

        is_platform = any(p in domain for p in platforms) or domain in ['gmail.com', 'yahoo.com', 'outlook.com', 'successfactors.eu', 'successfactors.com', 'greenhouse.io', 'myworkday.com']

        if (is_platform or company == "Unknown") and sender_name:
            clean_name = re.sub(r'(?i)\s+(hiring|team|recruiting|careers|jobs|notifications|via|bewerbermanagement|career|system|hr).*', '', sender_name).strip()
            if clean_name and clean_name.lower() not in ignore_names:
                company = clean_name

        # Try to find company name in body if still unknown
        if company == "Unknown" and text:
            # Look for "at [Company]" in first 500 chars, avoiding common noise
            body_match = re.search(r'(?i)\s+at\s+([A-Z][A-Za-z0-9\s&]{2,50})(?:\s+Corporate|SE|GmbH|AG|Inc|\.|\s|$)', text[:500])
            if body_match:
                potential = body_match.group(1).strip()
                if potential.lower() not in ignore_names and not any(p in potential.lower() for p in platforms):
                    company = potential

        if company == "Unknown" and text:
            # German signature detection
            sig_match = re.search(r'(?i)(Viele Grüße|Mit freundlichen Grüßen|Herzliche Grüße|Best regards|Greetings|Team|Yours sincerely)\s*,?\s+(?:dein|Ihr|Ihre)?\s*(.*?)(?:\s+Team)?(?:[\r\n]|$)', text[-1000:])
            if sig_match:
                potential = sig_match.group(2).strip()
                # If it's too short or contains common generic names, try to look at the next line
                if potential and potential.lower() not in ignore_names and len(potential) < 50:
                    company = potential
            
            # Fallback: Just look at the very last non-empty line if it looks like a company name
            if company == "Unknown":
                lines = [l.strip() for l in text.splitlines() if l.strip()]
                if lines:
                    last_line = lines[-1]
                    if 3 < len(last_line) < 50 and not any(p in last_line.lower() for p in platforms) and last_line.lower() not in ignore_names:
                         # Ensure it's not a URL or email
                         if not re.search(r'http|@', last_line):
                             company = last_line

        if company == "Unknown":
            for entry in extraction_cfg.get('subject_patterns', []):
                m = re.search(entry.get('pattern', ''), subject)
                if m:
                    res = m.group(entry.get('group', 1)).strip()
                    res = re.sub(r'(?i)\s+(application|role|job|position|update).*', '', res).strip()
                    if res.lower() not in ignore_names:
                        company = res
                        break

        status = ApplicationStatus.PENDING
        return ApplicationData(
            company_name=company, 
            status=status, 
            summary=f"Extracted via heuristics ({status.value})",
            is_rejection=False
        )