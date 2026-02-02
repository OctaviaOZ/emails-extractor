import pytest
from datetime import datetime, timedelta
from app.services.report import filter_applications_by_date, generate_word_report
from app.models import JobApplication, ApplicationStatus
import os

# Mock JobApplication for testing
class MockApp:
    def __init__(self, company, last_updated):
        self.company_name = company
        self.last_updated = last_updated
        self.status = ApplicationStatus.APPLIED
        self.position = "Dev"
        
    def model_dump(self):
        return {
            "company_name": self.company_name,
            "last_updated": self.last_updated,
            "status": self.status,
            "position": self.position
        }

def test_filter_applications_by_date():
    today = datetime.now()
    yesterday = today - timedelta(days=1)
    last_week = today - timedelta(days=7)
    
    apps = [
        MockApp("Company A", today),
        MockApp("Company B", yesterday),
        MockApp("Company C", last_week)
    ]
    
    # Test 1: All inclusive
    start = last_week - timedelta(days=1)
    filtered = filter_applications_by_date(apps, start_date=start, end_date=today)
    assert len(filtered) == 3
    
    # Test 2: Last 2 days
    start_recent = yesterday - timedelta(hours=1)
    filtered_recent = filter_applications_by_date(apps, start_date=start_recent, end_date=today)
    # Should include yesterday (because it's after start_recent?) 
    # start_recent is yesterday minus 1 hour. yesterday is yesterday. So yes.
    # Wait, yesterday = today - 1 day. start_recent = today - 1 day - 1 hour.
    # So yesterday > start_recent.
    assert len(filtered_recent) == 2 # Company A and B
    
    # Test 3: Specific single day (yesterday)
    # Start: Yesterday 00:00, End: Yesterday 23:59
    # My filter logic sets end date to 23:59:59 if passed.
    filtered_day = filter_applications_by_date(apps, start_date=yesterday, end_date=yesterday)
    assert len(filtered_day) == 1
    assert filtered_day[0].company_name == "Company B"

def test_generate_word_report_creates_file(tmp_path):
    apps = [
        MockApp("Company A", datetime.now()),
        MockApp("Company B", datetime.now())
    ]
    
    output_file = tmp_path / "test_report.docx"
    
    result = generate_word_report(apps, str(output_file))
    
    assert result is not None
    assert os.path.exists(result)
