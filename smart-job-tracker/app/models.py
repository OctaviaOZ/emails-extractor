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

# Progression logic: higher rank updates lower rank
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
    # CHANGED: Removed unique=True to allow multiple apps to the same company
    company_name: str = Field(index=True) 
    position: Optional[str] = Field(default="Unknown Position")
    status: ApplicationStatus = Field(default=ApplicationStatus.UNKNOWN)
    
    # NEW: Logic for active processes
    is_active: bool = Field(default=True) # Set to False if Rejected/Withdrawn
    
    # Dates
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_updated: datetime = Field(default_factory=datetime.utcnow)
    
    # NEW: Relationship to history
    history: List["ApplicationEvent"] = Relationship(back_populates="application")

class ApplicationEvent(SQLModel, table=True):
    """NEW: Stores the start-to-end history of an application."""
    id: Optional[int] = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="jobapplication.id")
    
    event_date: datetime = Field(default_factory=datetime.utcnow)
    old_status: Optional[ApplicationStatus] = None
    new_status: ApplicationStatus
    summary: str
    email_subject: str
    
    application: Optional[JobApplication] = Relationship(back_populates="history")

class ProcessedEmail(SQLModel, table=True):
    email_id: str = Field(primary_key=True)
    company_name: str
    processed_at: datetime = Field(default_factory=datetime.utcnow)

class ProcessingLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    run_date: datetime = Field(default_factory=datetime.utcnow)
    emails_processed: int
    emails_new: int
    errors: int
