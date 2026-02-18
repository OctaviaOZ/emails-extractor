import streamlit as st
import pandas as pd
from sqlmodel import Session, select
import sys
import resource
from app.core.config import settings
from app.core.logging import setup_logging
from app.core.database import init_db, engine
from app.models import JobApplication

# Import UI Components
from app.ui.components.sidebar import render_sidebar
from app.ui.tabs.dashboard import render_dashboard
from app.ui.tabs.kanban import render_kanban
from app.ui.tabs.settings import render_settings
from app.ui.tabs.history import render_history_view

# --- Memory Safety ---
def set_memory_limit(max_mem_mb):
    """
    Limits the virtual memory address space of the process to prevent system freezes.
    """
    if sys.platform != 'linux':
        return
    
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        max_mem_bytes = int(max_mem_mb * 1024 * 1024)
        
        if hard != resource.RLIM_INFINITY and max_mem_bytes > hard:
            max_mem_bytes = hard

        resource.setrlimit(resource.RLIMIT_AS, (max_mem_bytes, hard))
    except Exception as e:
        print(f"[System Safety] Warning: Could not set memory limit: {e}")

# Apply 6GB limit on startup
set_memory_limit(6144)

# Setup Logging
logger = setup_logging(settings.base_dir / "persistent_sync.log")

def main():
    st.set_page_config(page_title="Job Tracker", page_icon="💼", layout="wide")
    st.title("💼 Smart Job Application Tracker")
    
    # Initialize DB (run migrations)
    init_db()

    # Render Sidebar
    render_sidebar()

    # Fetch Data
    if "show_all" not in st.session_state:
        st.session_state.show_all = False

    with Session(engine) as session:
        # Fetch all applications for global stats
        all_apps = session.exec(select(JobApplication)).all()
        
        # UI Control for filtering
        st.session_state.show_all = st.checkbox(
            "Show Inactive/Rejected Applications", 
            value=st.session_state.show_all,
            help="Toggle between only active processes and the full history."
        )
        
        # Query for the display dataframe
        query = select(JobApplication)
        if not st.session_state.show_all:
            query = query.where(JobApplication.is_active)
        
        apps = session.exec(query).all()
    
    # DataFrames
    df_all = pd.DataFrame([a.model_dump() for a in all_apps]) if all_apps else pd.DataFrame()
    df = pd.DataFrame([a.model_dump() for a in apps]) if apps else pd.DataFrame()
        
    # --- TABS SELECTION ---
    tab_dash, tab_kanban, tab_settings = st.tabs(["📊 Dashboard", "📋 Kanban Board", "⚙️ Settings"])

    with tab_dash:
        render_dashboard(apps, df_all, df)

    with tab_kanban:
        render_kanban(apps)

    with tab_settings:
        render_settings()

    # --- HISTORY VIEW ---
    render_history_view(df, df)

if __name__ == "__main__":
    main()
