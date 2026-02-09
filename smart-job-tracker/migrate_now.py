import os
import sys
from sqlmodel import Session, create_engine, select
from dotenv import load_dotenv

# Add the current directory to sys.path to allow imports from app
sys.path.append(os.getcwd())

from app.services.milestone_migration import migrate_milestones_to_tables
from app.models import JobApplication

def main():
    load_dotenv()
    db_url = os.environ.get("DATABASE_URL", "postgresql:///job_tracker")
    engine = create_engine(db_url)
    
    print(f"Connecting to database...")
    with Session(engine) as session:
        migrate_milestones_to_tables(session)
    print("Migration execution finished.")

if __name__ == "__main__":
    main()
