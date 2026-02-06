from typing import Optional, List
from datetime import datetime
from sqlmodel import Field, SQLModel, Relationship
from enum import Enum

class ApplicationStatus(str, Enum):
    APPLIED = "APPLIED"
    INTERVIEW = "INTERVIEW"
    ASSESSMENT = "ASSESSMENT"
    PENDING = "PENDING"
    COMMUNICATION = "COMMUNICATION"
    OFFER = "OFFER"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def all_values(cls) -> List[str]:
        return [s.value for s in cls]

# Define the progression order - higher rank updates lower rank
STATUS_RANK = {
    ApplicationStatus.UNKNOWN: 0,
    ApplicationStatus.APPLIED: 1,
    ApplicationStatus.PENDING: 2,
    ApplicationStatus.COMMUNICATION: 2, # Same rank as Pending
    ApplicationStatus.ASSESSMENT: 3,
    ApplicationStatus.INTERVIEW: 4,
    ApplicationStatus.REJECTED: 5,
    ApplicationStatus.OFFER: 6
}

class ApplicationEventLog(SQLModel, table=True):
    """Stores the start-to-end history of an application."""
    __tablename__ = "applicationevent"
    __table_args__ = {"extend_existing": True}
    id: Optional[int] = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="jobapplication.id")
    
    event_date: datetime = Field(default_factory=datetime.utcnow)
    old_status: Optional[ApplicationStatus] = None
    new_status: ApplicationStatus
    summary: str # The specific update from this email
    email_subject: str
    
    application: Optional["JobApplication"] = Relationship(back_populates="history")

class Company(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    domain: Optional[str] = Field(default=None, index=True)
    
    applications: List["JobApplication"] = Relationship(back_populates="company")
    emails: List["CompanyEmail"] = Relationship(back_populates="company")

class CompanyEmail(SQLModel, table=True):
    """Remembers every email address associated with a company."""
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    company_id: int = Field(foreign_key="company.id")
    
    company: Company = Relationship(back_populates="emails")

class JobApplication(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}
    id: Optional[int] = Field(default=None, primary_key=True)
    
    company_id: Optional[int] = Field(default=None, foreign_key="company.id")
    company_name: str = Field(index=True) 
    position: Optional[str] = Field(default=None)
    status: ApplicationStatus = Field(default=ApplicationStatus.UNKNOWN)
    
    # ... rest of fields ...
    is_active: bool = Field(default=True)
    
    # Sender details
    sender_name: Optional[str] = None
    sender_email: Optional[str] = None
    
    # Dates
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_updated: datetime = Field(default_factory=datetime.utcnow)
    
    # Latest email details
    email_id: Optional[str] = Field(default=None)
    thread_id: Optional[str] = Field(default=None, index=True)
    email_subject: str
    email_snippet: Optional[str] = None
    summary: Optional[str] = None
    notes: Optional[str] = Field(default=None)
    
    # Metadata for reporting
    year: int
    month: int
    day: int
    
    # Relationships
    company: Optional[Company] = Relationship(back_populates="applications")
    history: List[ApplicationEventLog] = Relationship(back_populates="application")

class ProcessedEmail(SQLModel, table=True):
    """Tracks every email ID we have analyzed to prevent double processing."""
    __table_args__ = {"extend_existing": True}
    email_id: str = Field(primary_key=True)
    company_name: str # The company this email was attributed to
    processed_at: datetime = Field(default_factory=datetime.utcnow)

class ProcessingLog(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}
    id: Optional[int] = Field(default=None, primary_key=True)
    run_date: datetime = Field(default_factory=datetime.utcnow)
    emails_processed: int
    emails_new: int
    errors: int