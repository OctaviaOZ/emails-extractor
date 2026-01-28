import pytest
import os
from unittest.mock import MagicMock, patch
from app.services.extractor import EmailExtractor, ApplicationData, ApplicationStatus, OpenAIProvider, ClaudeProvider

@pytest.fixture
def mock_env_keys(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-anthropic")
    monkeypatch.setenv("GOOGLE_API_KEY", "sk-test-google")

def test_provider_initialization(mock_env_keys):
    extractor = EmailExtractor()
    # Order in new code: Claude, OpenAI, Gemini
    assert len(extractor.providers) == 3
    assert isinstance(extractor.providers[0], ClaudeProvider)
    assert isinstance(extractor.providers[1], OpenAIProvider)
    # 3rd is Gemini

@patch("app.services.extractor.ClaudeProvider.extract")
def test_claude_success(mock_extract, mock_env_keys):
    mock_extract.return_value = ApplicationData(
        company_name="Claude Corp",
        position="Software Engineer",
        status=ApplicationStatus.APPLIED,
        summary="Applied successfully",
        is_rejection=False,
        next_step="Wait"
    )
    
    extractor = EmailExtractor()
    result = extractor.extract("Subject", "sender", "Body")
    
    assert result.company_name == "Claude Corp"
    mock_extract.assert_called_once()

@patch("app.services.extractor.ClaudeProvider.extract")
@patch("app.services.extractor.OpenAIProvider.extract")
def test_failover_to_openai(mock_openai_extract, mock_claude_extract, mock_env_keys):
    # Claude fails
    mock_claude_extract.side_effect = Exception("Claude Down")
    
    # OpenAI succeeds
    mock_openai_extract.return_value = ApplicationData(
        company_name="OpenAI Inc",
        position=None,
        status=ApplicationStatus.INTERVIEW,
        summary="Interview scheduled",
        is_rejection=False,
        next_step="Book time"
    )
    
    extractor = EmailExtractor()
    result = extractor.extract("Subject", "sender", "Body")
    
    assert result.company_name == "OpenAI Inc"
    mock_claude_extract.assert_called_once()
    mock_openai_extract.assert_called_once()

def test_all_providers_fail():
    # No keys set, so no providers
    extractor = EmailExtractor() 
    
    # It should fall back to heuristic, not raise
    result = extractor.extract("Application for Software Engineer received", "hr@example.com", "Body")
    
    assert isinstance(result, ApplicationData)
    assert result.status == ApplicationStatus.COMMUNICATION # Heuristic default