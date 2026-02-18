from sqlmodel import SQLModel, create_engine, Session
from app.core.config import settings
from app.services.milestone_migration import migrate_milestones_to_tables
import logging

logger = logging.getLogger(__name__)

# Create Engine
engine = create_engine(settings.database_url, pool_pre_ping=True)

def init_db():
    """Initializes the database and runs necessary migrations."""
    logger.info("Initializing database...")
    try:
        SQLModel.metadata.create_all(engine)
        
        # Run internal data migrations
        with Session(engine) as session:
            migrate_milestones_to_tables(session)
            
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.critical(f"Database initialization failed: {e}")
        raise e

def get_session():
    """Dependency for getting a DB session."""
    with Session(engine) as session:
        yield session
