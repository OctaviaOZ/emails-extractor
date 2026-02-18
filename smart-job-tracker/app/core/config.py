import os
import yaml
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = BASE_DIR / "config.yaml"

class AIConfig(BaseModel):
    local_model_name: str = "Llama-3.2-3B-Instruct-Q4_K_M.gguf"
    temperature: float = 0.1
    max_tokens: int = 512

class ExtractionConfig(BaseModel):
    platforms: List[str] = [
        "Workday", "Greenhouse", "SmartRecruiters", "Lever", 
        "Ashby", "Jobvite", "SuccessFactors"
    ]
    generic_names: List[str] = [
        "Hiring Team", "Recruiting", "Careers", "Talent Acquisition"
    ]
    subject_patterns: List[Dict[str, Any]] = []

class StatusKeywordsConfig(BaseModel):
    rejected: List[str] = ["regret", "unfortunately", "not moving forward", "other candidates", "declined"]
    assessment: List[str] = ["assessment", "coding challenge", "take-home", "hackerrank", "codility"]
    interview: List[str] = ["interview", "meet with", "schedule a time", "availability"]
    offer: List[str] = ["offer", "pleased to offer", "congratulations"]
    applied: List[str] = ["received", "thank you for applying", "application confirmation"]

class Settings(BaseModel):
    # App Settings
    label_name: str = Field(default="apply", description="Gmail label to sync")
    start_date: str = Field(default="2025-01-01", description="Start date for sync")
    skip_domains: List[str] = Field(default_factory=list)
    skip_emails: List[str] = Field(default_factory=list)
    scopes: List[str] = ["https://www.googleapis.com/auth/gmail.readonly"]
    
    # Paths
    base_dir: Path = BASE_DIR
    credentials_path: Path = BASE_DIR / "credentials.json"
    token_path: Path = BASE_DIR / "token.pickle"
    database_url: str = Field(default_factory=lambda: os.getenv("DATABASE_URL", "postgresql:///job_tracker"))
    
    # AI & Extraction
    ai: AIConfig = Field(default_factory=AIConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    status_keywords: StatusKeywordsConfig = Field(default_factory=StatusKeywordsConfig)
    report_mapping: Dict[str, str] = Field(default_factory=dict)

    # API Keys
    openai_api_key: Optional[str] = Field(default_factory=lambda: os.getenv("OPENAI_API_KEY"))
    anthropic_api_key: Optional[str] = Field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY"))
    google_api_key: Optional[str] = Field(default_factory=lambda: os.getenv("GOOGLE_API_KEY"))

    @classmethod
    def load(cls) -> "Settings":
        """Loads configuration from yaml and overrides with env vars."""
        config_data = {}
        
        # Load YAML
        if CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH, 'r') as f:
                    yaml_data = yaml.safe_load(f)
                    if yaml_data:
                        config_data.update(yaml_data)
            except Exception as e:
                print(f"Warning: Could not load config.yaml: {e}")

        # Create instance (this will also pull defaults and env vars for fields with factories)
        settings = cls(**config_data)
        
        # Explicit env overrides for root fields if needed (though factories handle most)
        if os.getenv("LABEL_NAME"):
            settings.label_name = os.getenv("LABEL_NAME")
            
        return settings

# Global settings instance
settings = Settings.load()

def save_settings(new_settings: Settings):
    """Persists current settings to config.yaml (only serializable fields)."""
    data = new_settings.model_dump(exclude={'base_dir', 'credentials_path', 'token_path', 'database_url', 'openai_api_key', 'anthropic_api_key', 'google_api_key'})
    
    # Convert paths to strings for YAML
    # (Pydantic's model_dump might do this, but being safe for YAML)
    
    with open(CONFIG_PATH, 'w') as f:
        yaml.safe_dump(data, f, default_flow_style=False)
