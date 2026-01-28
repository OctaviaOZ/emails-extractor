import pytest
import os
from unittest.mock import MagicMock, patch
from app.services.extractor import EmailExtractor, ExtractedData, ApplicationStatus, OpenAIProvider, ClaudeProvider

@pytest.fixture
def mock_env_keys(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-anthropic")
    monkeypatch.setenv("GOOGLE_API_KEY", "sk-test-google")

def test_provider_initialization(mock_env_keys):
    extractor = EmailExtractor()
    assert len(extractor.providers) == 3
    assert isinstance(extractor.providers[0], OpenAIProvider)
    assert isinstance(extractor.providers[1], ClaudeProvider)
    # 3rd is Gemini

@patch("app.services.extractor.OpenAIProvider.extract")
def test_openai_success(mock_extract, mock_env_keys):
    mock_extract.return_value = ExtractedData(
        company_name="OpenAI Corp",
        status=ApplicationStatus.APPLIED,
        summary="Applied successfully"
    )
    
    extractor = EmailExtractor()
    result = extractor.extract("Subject", "sender", "Body", "HTML")
    
    assert result.company_name == "OpenAI Corp"
    mock_extract.assert_called_once()

@patch("app.services.extractor.OpenAIProvider.extract")
@patch("app.services.extractor.ClaudeProvider.extract")
def test_failover_to_claude(mock_claude_extract, mock_openai_extract, mock_env_keys):
    # OpenAI fails
    mock_openai_extract.side_effect = Exception("OpenAI Down")
    
    # Claude succeeds
    mock_claude_extract.return_value = ExtractedData(
        company_name="Claude Inc",
        status=ApplicationStatus.INTERVIEW,
        summary="Interview scheduled"
    )
    
    extractor = EmailExtractor()
    result = extractor.extract("Subject", "sender", "Body", "HTML")
    
    assert result.company_name == "Claude Inc"
    mock_openai_extract.assert_called_once()
    mock_claude_extract.assert_called_once()
    
    # Verify OpenAI provider is marked as failed (unavailable)
    assert not extractor.providers[0].is_available

def test_heuristic_fallback():
    # No keys set, so no providers
    extractor = EmailExtractor() 
    
    # Subject matching heuristic
    result = extractor.extract("Application for Software Engineer received", "hr@example.com", "Body", "HTML")
    
    # Just checking it returns a valid object and tries to parse
    assert isinstance(result, ExtractedData)
    # "received" pattern usually extracts company if nicely formatted, but here simple check
    assert result.status == ApplicationStatus.COMMUNICATION # Default if keywords missing
