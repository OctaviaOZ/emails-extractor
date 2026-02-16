import pytest
import os
from unittest.mock import MagicMock, patch
from app.services.extractor import EmailExtractor, ApplicationData, ApplicationStatus, OpenAIProvider, ClaudeProvider, LocalProvider, GeminiProvider

@pytest.fixture
def mock_env_keys(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-anthropic")
    monkeypatch.setenv("GOOGLE_API_KEY", "sk-test-google")

def test_provider_initialization(mock_env_keys):
    extractor = EmailExtractor()
    # Order in new code: Local, Claude, Gemini, OpenAI
    assert len(extractor.providers) == 4
    assert isinstance(extractor.providers[0], LocalProvider)
    assert isinstance(extractor.providers[1], ClaudeProvider)
    assert isinstance(extractor.providers[2], GeminiProvider)
    assert isinstance(extractor.providers[3], OpenAIProvider)

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

def test_all_providers_fail():
    # No keys set, so no providers
    extractor = EmailExtractor() 
    
    # It should fall back to heuristic, not raise
    result = extractor.extract("Application for Software Engineer received", "hr@example.com", "Body")
    
    assert isinstance(result, ApplicationData)
    assert result.status == ApplicationStatus.APPLIED # New Heuristic default

@patch("app.services.extractor.ClaudeProvider.extract")
@patch("app.services.extractor.OpenAIProvider.extract")
@patch("app.services.extractor.GeminiProvider.extract")
def test_provider_failover_and_quota_skipping(mock_gemini, mock_openai, mock_claude, mock_env_keys):
    # Claude fails with general error
    mock_claude.side_effect = Exception("General Fail")
    # Gemini fails with quota error
    mock_gemini.side_effect = Exception("Rate limit reached 429")
    # OpenAI succeeds
    mock_openai.return_value = ApplicationData(
        company_name="OpenAI Inc",
        status=ApplicationStatus.INTERVIEW,
        is_rejection=False
    )
    
    extractor = EmailExtractor()
    
    # 1st Call - Uses failover
    result = extractor.extract("S1", "s1", "B1")
    assert result.company_name == "OpenAI Inc"
    assert "GeminiProvider" in extractor.failed_providers
    assert "ClaudeProvider" not in extractor.failed_providers # General error doesn't skip
    
    # 2nd Call - Should skip Gemini
    mock_gemini.reset_mock()
    extractor.extract("S2", "s2", "B2")
    mock_gemini.assert_not_called()
