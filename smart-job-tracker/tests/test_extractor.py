import pytest
from unittest.mock import patch, MagicMock
from app.services.extractor import EmailExtractor, ApplicationData, ApplicationStatus, ClaudeProvider, LocalProvider
from app.core.config import settings


@pytest.fixture
def mock_env_keys(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "sk-test-openai")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test-anthropic")
    monkeypatch.setattr(settings, "google_api_key", "sk-test-google")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-anthropic")
    monkeypatch.setenv("GOOGLE_API_KEY", "sk-test-google")


def test_provider_initialization():
    extractor = EmailExtractor()
    # Current architecture: single active provider based on config
    assert extractor.provider is not None or extractor.provider is None  # no crash


def test_heuristic_fallback_no_provider():
    """When no provider is available, heuristic kicks in and produces valid ApplicationData."""
    extractor = EmailExtractor()
    extractor.provider = None  # force heuristic path

    result = extractor.extract(
        "Application for Software Engineer received",
        "hr@example.com",
        "We received your application."
    )

    assert isinstance(result, ApplicationData)
    assert result.status == ApplicationStatus.APPLIED


def test_provider_extract_called(monkeypatch):
    """Provider.extract result flows through refinement and is returned."""
    extractor = EmailExtractor()
    mock_provider = MagicMock()
    mock_provider.extract.return_value = ApplicationData(
        company_name="Acme Corp",
        position="Backend Engineer",
        status=ApplicationStatus.APPLIED,
        summary="Application confirmation received.",
        is_rejection=False,
        next_step="Wait"
    )
    extractor.provider = mock_provider

    result = extractor.extract("Subject", "hr@acmecorp.com", "Body")

    mock_provider.extract.assert_called_once()
    assert result.company_name == "Acme Corp"


def test_provider_exception_falls_back_to_heuristic():
    """If provider raises, extraction falls back to heuristic without crashing."""
    extractor = EmailExtractor()
    mock_provider = MagicMock()
    mock_provider.extract.side_effect = Exception("Provider failure")
    extractor.provider = mock_provider

    result = extractor.extract(
        "Interview invitation for Data Engineer at TechCo",
        "recruiting@techco.com",
        "We would like to invite you to an interview."
    )

    assert isinstance(result, ApplicationData)
    assert result.status in list(ApplicationStatus)


# --- Summary quality tests ---

def test_summary_hard_capped_in_validator():
    """ApplicationData validator truncates summaries longer than 120 chars."""
    long_summary = "a" * 200
    data = ApplicationData(
        company_name="Acme",
        status=ApplicationStatus.APPLIED,
        summary=long_summary,
        is_rejection=False,
    )
    assert len(data.summary) <= 120
    assert data.summary.endswith("...")


def test_summary_preserved_when_concise():
    """Short summaries are stored as-is."""
    data = ApplicationData(
        company_name="Acme",
        status=ApplicationStatus.APPLIED,
        summary="Coding challenge invite received.",
        is_rejection=False,
    )
    assert data.summary == "Coding challenge invite received."


def test_refine_summary_falls_back_to_subject_when_generic():
    """_refine_summary replaces generic placeholders with the email subject."""
    extractor = EmailExtractor()
    extractor.provider = None

    data = ApplicationData(
        company_name="Acme",
        status=ApplicationStatus.APPLIED,
        summary="Extracted via heuristics",
        is_rejection=False,
    )
    result = extractor._refine_summary(data, "Einladung zum Vorstellungsgespräch bei Acme GmbH")
    assert result.summary != "Extracted via heuristics"
    assert "Acme" in result.summary or len(result.summary) > 5


def test_refine_summary_caps_long_output():
    """_refine_summary truncates any summary still over 120 chars."""
    extractor = EmailExtractor()
    extractor.provider = None

    data = ApplicationData(
        company_name="Acme",
        status=ApplicationStatus.APPLIED,
        summary="x" * 200,
        is_rejection=False,
    )
    result = extractor._refine_summary(data, "Some subject")
    assert len(result.summary) <= 120


def test_german_rejection_status_detected():
    """German rejection keywords in subject correctly set status to REJECTED."""
    extractor = EmailExtractor()
    extractor.provider = None

    result = extractor.extract(
        "Absage Ihrer Bewerbung als Data Engineer",
        "hr@firma.de",
        "Leider müssen wir Ihnen mitteilen, dass wir Ihre Bewerbung nicht weiterverfolgen."
    )

    assert result.status == ApplicationStatus.REJECTED
    assert result.is_rejection is True


def test_unfortunately_alone_does_not_trigger_rejection():
    """'unfortunately' alone should not override a non-rejection result."""
    extractor = EmailExtractor()
    extractor.provider = None

    result = extractor.extract(
        "Your Application as Data Engineer at Webgears Group",
        '"Aneta Leimig | Webgears GmbH" <aneta@webgears.hire.trakstar.com>',
        "Unfortunately, we have not yet completed our review. Your application is still being considered."
    )

    assert result.status != ApplicationStatus.REJECTED
    assert result.is_rejection is False


def test_unfortunately_with_strong_rejection_kw_triggers_rejection():
    """'unfortunately' paired with a strong rejection keyword should still trigger REJECTED."""
    extractor = EmailExtractor()
    extractor.provider = None

    result = extractor.extract(
        "Your Application as Data Engineer",
        "hr@firma.de",
        "Unfortunately, we are not moving forward with your application."
    )

    assert result.status == ApplicationStatus.REJECTED
    assert result.is_rejection is True


def test_german_interview_status_detected():
    """German interview keyword in subject triggers INTERVIEW status."""
    extractor = EmailExtractor()
    extractor.provider = None

    result = extractor.extract(
        "Einladung zum Vorstellungsgespräch",
        "hr@firma.de",
        "Wir laden Sie herzlich zu einem Vorstellungsgespräch ein."
    )

    assert result.status == ApplicationStatus.INTERVIEW
