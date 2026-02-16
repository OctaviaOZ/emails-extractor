import pytest
from datetime import datetime
from unittest.mock import MagicMock
from sqlmodel import Session, select
from app.services.processor import ApplicationProcessor
from app.services.extractor import ApplicationData
from app.models import JobApplication, ApplicationStatus, Company, CompanyEmail

@pytest.fixture
def session():
    return MagicMock(spec=Session)

@pytest.fixture
def processor(session):
    return ApplicationProcessor(session=session, config={})

def test_normalize_company(processor):
    assert processor._normalize_company("Google GmbH") == "google"
    assert processor._normalize_company("ACME Corp.") == "acme"
    assert processor._normalize_company("Tech Solutions SE") == "tech"
    assert processor._normalize_company("Unknown") == "unknown"

def test_can_update_status():
    app = JobApplication(company_name="Test", status=ApplicationStatus.APPLIED, email_subject="test", year=2026, month=1, day=1)
    
    # Applied -> Interview is a progression
    assert app.can_update_status(ApplicationStatus.INTERVIEW) is True
    
    # Applied -> Rejected is allowed
    assert app.can_update_status(ApplicationStatus.REJECTED) is True
    
    # Interview -> Applied is NOT a progression
    app.status = ApplicationStatus.INTERVIEW
    assert app.can_update_status(ApplicationStatus.APPLIED) is False
    
    # Interview -> Offer is a progression
    assert app.can_update_status(ApplicationStatus.OFFER) is True

def test_process_new_application_creates_company(processor, session):
    processor._find_existing_application = MagicMock(return_value=(None, False))
    session.exec.return_value.first.return_value = None # No existing company
    
    data = ApplicationData(
        company_name="NewCorp",
        position="Engineer",
        status=ApplicationStatus.APPLIED,
        summary="Summary",
        is_rejection=False
    )
    email_meta = {
        'subject': 'Sub',
        'id': 'msg1',
        'sender_email': 'hr@newcorp.com'
    }
    timestamp = datetime.now()
    
    processor.process_extraction(data, email_meta, timestamp)
    
    # Check that company creation was attempted
    company_calls = [call for call in session.add.call_args_list if isinstance(call.args[0], Company)]
    assert len(company_calls) > 0
    assert company_calls[0].args[0].name == "NewCorp"

def test_process_extraction_upgrades_unknown_name(processor, session):
    # Existing app with name "Unknown"
    existing_app = JobApplication(
        id=1, company_name="Unknown", status=ApplicationStatus.APPLIED, 
        email_subject="Sub", year=2026, month=1, day=1, is_active=True
    )
    processor._find_existing_application = MagicMock(return_value=(existing_app, True))
    
    # Data with actual name
    data = ApplicationData(
        company_name="RealCorp",
        position="Engineer",
        status=ApplicationStatus.COMMUNICATION,
        summary="Summary",
        is_rejection=False
    )
    email_meta = {'subject': 'Sub', 'id': 'msg2', 'sender_email': 'hr@realcorp.com'}
    
    processor.process_extraction(data, email_meta, datetime.now())
    
    assert existing_app.company_name == "RealCorp"
