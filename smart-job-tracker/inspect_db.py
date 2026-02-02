from sqlmodel import create_engine, text
import os

db_url = os.environ.get("DATABASE_URL", "postgresql:///job_tracker")
engine = create_engine(db_url)

with engine.connect() as conn:
    # Query Postgres for the values in the 'applicationstatus' enum
    result = conn.execute(text("SELECT enumlabel FROM pg_enum JOIN pg_type ON pg_enum.enumtypid = pg_type.oid WHERE pg_type.typname = 'applicationstatus';"))
    labels = [row[0] for row in result]
    print(f"Current DB Enum Labels: {labels}")
