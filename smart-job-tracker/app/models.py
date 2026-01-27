from typing import Optional
from datetime import datetime
from sqlmodel import Field, SQLModel
from enum import Enum

class ApplicationStatus(str, Enum):
    APPLIED = "Applied"
    INTERVIEW = "Interview"
    ASSESSMENT = "Assessment"
    COMMUNICATION = "Communication"
    OFFER = "Offer"
    REJECTED = "Rejected"
    UNKNOWN = "Unknown"

# Define the progression order - higher rank updates lower rank
STATUS_RANK = {
    ApplicationStatus.UNKNOWN: 0,
    ApplicationStatus.APPLIED: 1,
    ApplicationStatus.COMMUNICATION: 2,
    ApplicationStatus.ASSESSMENT: 3,
    ApplicationStatus.INTERVIEW: 4,
    ApplicationStatus.REJECTED: 5,
    ApplicationStatus.OFFER: 6
}

class JobApplication(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    company_name: str = Field(index=True, unique=True) # Unique company process
    position: Optional[str] = Field(default=None)
    status: ApplicationStatus = Field(default=ApplicationStatus.UNKNOWN)
    
    # Dates
    applied_at: datetime = Field(default_factory=datetime.utcnow) # First email date
    last_updated: datetime = Field(default_factory=datetime.utcnow) # Latest email date
    
    # Latest email details
    email_subject: str
    email_snippet: Optional[str] = None
    
    # Metadata for reporting
    year: int
    month: int
    day: int

class ProcessedEmail(SQLModel, table=True):
    """Tracks every email ID we have analyzed to prevent double processing."""
    email_id: str = Field(primary_key=True)
    company_name: str # The company this email was attributed to
    processed_at: datetime = Field(default_factory=datetime.utcnow)

class ProcessingLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    run_date: datetime = Field(default_factory=datetime.utcnow)
    emails_processed: int
    emails_new: int
    errors: int
