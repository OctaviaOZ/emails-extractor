from typing import Optional, List
from datetime import datetime
from sqlmodel import Field, SQLModel, Relationship
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
    # REMOVED: unique=True constraint to allow multiple apps per company
    company_name: str = Field(index=True) 
    position: Optional[str] = Field(default=None)
    status: ApplicationStatus = Field(default=ApplicationStatus.UNKNOWN)
    
    # Tracking distinct processes
    is_active: bool = Field(default=True) # False if Rejected/Withdrawn
    
    # Sender details
    sender_name: Optional[str] = None
    sender_email: Optional[str] = None
    
    # Dates
    applied_at: datetime = Field(default_factory=datetime.utcnow) # First email date
    last_updated: datetime = Field(default_factory=datetime.utcnow) # Latest email date
    
    # Latest email details
    email_subject: str
    email_snippet: Optional[str] = None
    summary: Optional[str] = None # Short AI or heuristic summary
    
    # Metadata for reporting
    year: int
    month: int
    day: int
    
    # Relationship to history
    history: List["ApplicationEvent"] = Relationship(back_populates="application")

class ApplicationEvent(SQLModel, table=True):
    """Stores the start-to-end history of an application."""
    id: Optional[int] = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="jobapplication.id")
    
    event_date: datetime = Field(default_factory=datetime.utcnow)
    old_status: Optional[ApplicationStatus] = None
    new_status: ApplicationStatus
    summary: str # The specific update from this email
    email_subject: str
    
    application: Optional[JobApplication] = Relationship(back_populates="history")

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