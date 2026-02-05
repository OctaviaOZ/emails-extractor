import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch
from sqlmodel import Session
from app.services.processor import ApplicationProcessor
from app.services.extractor import ApplicationData
from app.models import JobApplication, ApplicationStatus, ApplicationEventLog

@pytest.fixture
def session():
    return MagicMock(spec=Session)

@pytest.fixture
def processor(session):
    return ApplicationProcessor(session=session, config={})

def test_process_new_application(processor, session):
    # Mocking that no existing app is found
    processor._find_existing_application = MagicMock(return_value=(None, False))
    
    data = ApplicationData(
        company_name="NewCorp",
        position="Engineer",
        status=ApplicationStatus.APPLIED,
        summary="Applied to NewCorp",
        is_rejection=False,
        next_step="Wait"
    )
    
    email_meta = {
        'subject': 'Thank you for applying',
        'id': 'msg_123',
        'sender_name': 'HR',
        'sender_email': 'hr@newcorp.com'
    }
    
    timestamp = datetime(2026, 1, 1)
    
    processor.process_extraction(data, email_meta, timestamp)
    
    # Verify session.add was called for JobApplication
    args, _ = session.add.call_args_list[0]
    new_app = args[0]
    assert isinstance(new_app, JobApplication)
    assert new_app.company_name == "NewCorp"
    assert new_app.status == ApplicationStatus.APPLIED

def test_process_status_update(processor, session):
    # Mocking existing active application with OLD date
    existing_app = JobApplication(
        id=1,
        company_name="OldCorp",
        status=ApplicationStatus.APPLIED,
        email_subject="Initial",
        last_updated=datetime(2025, 12, 31),
        year=2025, month=12, day=31
    )
    processor._find_existing_application = MagicMock(return_value=(existing_app, True))
    session.get.return_value = existing_app
    
    data = ApplicationData(
        company_name="OldCorp",
        position="Engineer",
        status=ApplicationStatus.INTERVIEW,
        summary="Interview invite",
        is_rejection=False,
        next_step="Schedule"
    )
    
    email_meta = {'subject': 'Interview Invite', 'id': 'msg_456'}
    timestamp = datetime(2026, 1, 2)
    
    processor.process_extraction(data, email_meta, timestamp)
    
    # Verify app status was updated
    assert existing_app.status == ApplicationStatus.INTERVIEW
    assert existing_app.last_updated == timestamp
