from typing import Optional, List, Dict
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

# Progression order: higher rank updates lower rank
from app.core.constants import STATUS_RANK

class ApplicationEventLog(SQLModel, table=True):
    """Tracks the history of status changes for an application."""
    __tablename__ = "applicationevent"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="jobapplication.id")
    
    event_date: datetime = Field(default_factory=datetime.utcnow)
    old_status: Optional[ApplicationStatus] = None
    new_status: ApplicationStatus
    summary: str
    email_subject: str
    
    application: Optional["JobApplication"] = Relationship(back_populates="history")

    def __repr__(self) -> str:
        return f"<ApplicationEventLog(id={self.id}, application_id={self.application_id}, new_status={self.new_status})>"

class Company(SQLModel, table=True):
    """Represents an employer."""
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    domain: Optional[str] = Field(default=None, index=True)
    
    applications: List["JobApplication"] = Relationship(back_populates="company")
    emails: List["CompanyEmail"] = Relationship(back_populates="company")

    def __repr__(self) -> str:
        return f"<Company(id={self.id}, name={self.name})>"

class CompanyEmail(SQLModel, table=True):
    """Stores email addresses associated with a company to aid in identification."""
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    company_id: int = Field(foreign_key="company.id")
    
    company: Company = Relationship(back_populates="emails")

class JobApplication(SQLModel, table=True):
    """Main model for a job application."""
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    
    company_id: Optional[int] = Field(default=None, foreign_key="company.id")
    company_name: str = Field(index=True) 
    position: Optional[str] = Field(default="Unknown Position")
    status: ApplicationStatus = Field(default=ApplicationStatus.UNKNOWN)
    
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
    
    # Milestone Flags
    reached_assessment: bool = Field(default=False)
    reached_interview: bool = Field(default=False)
    
    # Metadata for reporting
    year: int
    month: int
    day: int
    
    # Relationships
    company: Optional[Company] = Relationship(back_populates="applications")
    history: List[ApplicationEventLog] = Relationship(
        back_populates="application", 
        sa_relationship_kwargs={"cascade": "all, delete-orphan", "order_by": "ApplicationEventLog.event_date.desc()"}
    )
    interviews: List["Interview"] = Relationship(back_populates="application", sa_relationship_kwargs={"cascade": "all, delete-orphan"})
    assessments: List["Assessment"] = Relationship(back_populates="application", sa_relationship_kwargs={"cascade": "all, delete-orphan"})
    offers: List["Offer"] = Relationship(back_populates="application", sa_relationship_kwargs={"cascade": "all, delete-orphan"})

    @property
    def status_rank(self) -> int:
        return STATUS_RANK.get(self.status, 0)

    def can_update_status(self, new_status: ApplicationStatus) -> bool:
        """Checks if the new status is a progression or a relevant lateral move."""
        new_rank = STATUS_RANK.get(new_status, 0)
        return new_rank > self.status_rank or new_status == ApplicationStatus.REJECTED

    def __repr__(self) -> str:
        return f"<JobApplication(id={self.id}, company={self.company_name}, status={self.status})>"

class Interview(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="jobapplication.id")
    interview_date: datetime
    interviewer: Optional[str] = None
    location: Optional[str] = None
    notes: Optional[str] = None
    
    application: Optional[JobApplication] = Relationship(back_populates="interviews")

class Assessment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="jobapplication.id")
    due_date: Optional[datetime] = None
    type: Optional[str] = None
    notes: Optional[str] = None
    
    application: Optional[JobApplication] = Relationship(back_populates="assessments")

class Offer(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="jobapplication.id")
    offer_date: datetime = Field(default_factory=datetime.utcnow)
    salary: Optional[str] = None
    benefits: Optional[str] = None
    deadline: Optional[datetime] = None
    notes: Optional[str] = None
    
    application: Optional[JobApplication] = Relationship(back_populates="offers")

class ProcessedEmail(SQLModel, table=True):
    """Prevents double processing of emails."""
    __table_args__ = {"extend_existing": True}
    email_id: str = Field(primary_key=True)
    company_name: str
    processed_at: datetime = Field(default_factory=datetime.utcnow)

class ProcessingLog(SQLModel, table=True):
    """Logs the results of sync runs."""
    __table_args__ = {"extend_existing": True}
    id: Optional[int] = Field(default=None, primary_key=True)
    run_date: datetime = Field(default_factory=datetime.utcnow)
    emails_processed: int
    emails_new: int
    errors: int