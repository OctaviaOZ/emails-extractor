import pytest
from datetime import datetime
from app.models import JobApplication, ApplicationStatus

def test_job_application_creation():
    app = JobApplication(
        company_name="Test Company",
        position="Software Engineer",
        status=ApplicationStatus.APPLIED,
        email_subject="Application Received",
        email_id="gmail_123",
        year=2026,
        month=1,
        day=23
    )
    assert app.company_name == "Test Company"
    assert app.status == ApplicationStatus.APPLIED
    assert isinstance(app.last_updated, datetime)
    assert app.email_id == "gmail_123"

def test_application_status_enum():
    assert ApplicationStatus.APPLIED == "APPLIED"
    assert ApplicationStatus.REJECTED == "REJECTED"
