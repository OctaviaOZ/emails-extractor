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

@patch("app.services.extractor.ClaudeProvider.extract")
@patch("app.services.extractor.OpenAIProvider.extract")
@patch("app.services.extractor.GeminiProvider.extract")
def test_circuit_breaker(mock_gemini, mock_openai, mock_claude, mock_env_keys):
    # All providers fail
    mock_claude.side_effect = Exception("Fail")
    mock_openai.side_effect = Exception("Fail")
    mock_gemini.side_effect = Exception("Fail")
    
    extractor = EmailExtractor()
    extractor.max_failures = 2 # Lower threshold for test
    
    # 1st Failure
    extractor.extract("S1", "s1", "B1")
    assert extractor.consecutive_ai_failures == 1
    
    # 2nd Failure
    extractor.extract("S2", "s2", "B2")
    assert extractor.consecutive_ai_failures == 2
    
    # 3rd Call - Circuit Breaker should be active (>= max_failures)
    # Providers should NOT be called again
    mock_claude.reset_mock()
    mock_openai.reset_mock()
    
    extractor.extract("S3", "s3", "B3")
    
    # Assert providers were NOT called
    mock_claude.assert_not_called()
    mock_openai.assert_not_called()
    
    # Failure count increments once to signal tripping, then stays
    assert extractor.consecutive_ai_failures == 3 
    
    # 4th Call - Still broken
    extractor.extract("S4", "s4", "B4")
    mock_claude.assert_not_called()
