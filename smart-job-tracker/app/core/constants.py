from typing import List, Set, Dict
from app.models import ApplicationStatus

# --- Application Status ---
STATUS_RANK: Dict[ApplicationStatus, int] = {
    ApplicationStatus.UNKNOWN: 0,
    ApplicationStatus.APPLIED: 1,
    ApplicationStatus.PENDING: 2,
    ApplicationStatus.COMMUNICATION: 2,
    ApplicationStatus.ASSESSMENT: 3,
    ApplicationStatus.INTERVIEW: 4,
    ApplicationStatus.REJECTED: 5,
    ApplicationStatus.OFFER: 6
}

# --- Shared Platforms & Email Domains ---
SHARED_PLATFORMS: Set[str] = {
    'myworkdayjobs.com', 
    'successfactors.eu', 
    'successfactors.com', 
    'greenhouse.io', 
    'smartrecruiters.com', 
    'lever.co', 
    'ashby.io', 
    'jobvite.com', 
    'breezy.hr', 
    'recruitee.com', 
    'personio.de', 
    'personio.com',
    'workable.com'
}

GENERIC_DOMAINS: Set[str] = {
    'gmail.com', 
    'yahoo.com', 
    'outlook.com', 
    'hotmail.com', 
    'icloud.com', 
    'me.com', 
    'live.com', 
    'msn.com',
    'web.de',
    'gmx.de',
    't-online.de'
}

SHARED_EMAILS: Set[str] = {
    'notifications@smartrecruiters.com', 
    'no-reply@successfactors.com', 
    'noreply@myworkday.com',
    'no-reply@greenhouse.io',
    'notifications@ashby.io'
}

GENERIC_NAMES: Set[str] = {
    'hiring', 'team', 'recruiting', 'careers', 'jobs', 'notifications', 
    'via', 'bewerbermanagement', 'career', 'system', 'hr', 'human resources', 
    'talent acquisition', 'people team', 'recruitment'
}

PLATFORM_NAMES: Set[str] = {
    'Workday', 'Greenhouse', 'SmartRecruiters', 'Lever', 'Ashby', 'Jobvite', 
    'Breezy', 'Recruitee', 'Personio', 'Workable', 'SuccessFactors', 'SAP'
}

# --- Company Name Normalization Suffixes ---
COMPANY_SUFFIXES: List[str] = [
    'gmbh', 'ag', 'inc', 'ltd', 'co', 'kg', 'plc', 'se', 'corp', 'corporation', 
    'holding', 'group', 'germany', 'deutschland', 'berlin', 'europe', 'emea', 
    'international', 'solutions', 'systems', 'technology', 'technologies',
    'successfactors', 'workday', 'greenhouse'
]

# --- UI Defaults ---
KANBAN_STATUSES: List[ApplicationStatus] = [
    ApplicationStatus.APPLIED,
    ApplicationStatus.PENDING,
    ApplicationStatus.COMMUNICATION,
    ApplicationStatus.ASSESSMENT,
    ApplicationStatus.INTERVIEW,
    ApplicationStatus.OFFER,
    ApplicationStatus.REJECTED
]

GERMAN_STATUS_MAPPING: Dict[str, str] = {
    "APPLIED": "Beworben",
    "INTERVIEW": "Vorstellungsgespräch",
    "ASSESSMENT": "Eignungstest",
    "PENDING": "Laufend",
    "OFFER": "Vertragsangebot",
    "REJECTED": "Absage",
    "UNKNOWN": "Unbekannt",
    "COMMUNICATION": "Kommunikation"
}
