import streamlit as st
import pandas as pd
from sqlmodel import Session, select, create_engine, SQLModel, delete
from datetime import datetime
import plotly.express as px
import os
import yaml
import re
import logging
from dotenv import load_dotenv
from dateutil import parser
import resource
import sys

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
        print(f"[System Safety] Memory limit set to {max_mem_mb} MB to prevent freezing.")
    except Exception as e:
        print(f"[System Safety] Warning: Could not set memory limit: {e}")

# Apply 6GB limit on startup - 4GB was too tight for Llama 3.2 3B + App overhead
set_memory_limit(6144)

# --- Setup logging ---
base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
log_file_path = os.path.join(base_dir, "persistent_sync.log")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file_path),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

from app.services.gmail import get_gmail_service, get_message_body
from app.services.extractor import EmailExtractor, ApplicationData
from app.services.processor import ApplicationProcessor
from app.services.sync import SyncService
from app.services.report import generate_pdf_report, generate_word_report
from app.services.milestone_migration import migrate_milestones_to_tables
from app.services.merge import merge_applications
from app.models import JobApplication, ApplicationStatus, ProcessedEmail, ApplicationEventLog, Company, ProcessingLog, CompanyEmail, Interview, Assessment, Offer

# --- Load Environment Variables ---
load_dotenv(os.path.join(base_dir, ".env"))

def load_config():
    config_path = os.path.join(os.path.dirname(base_dir), "config.yaml")
    if os.path.exists(config_path):
        with open(config_path, 'r') as f:
            return yaml.safe_load(f)
    return {}

def create_db_and_tables(engine):
    try:
        SQLModel.metadata.create_all(engine)
        # Perform one-time migration of legacy flags to new tables
        with Session(engine) as session:
            migrate_milestones_to_tables(session)
    except Exception as e:
        st.error(f"Database connection error: {e}")
        st.stop()

# --- Sync Logic ---
def sync_emails(engine):
    st.toast("Starting Sync...", icon="🔄")
    logger.info("Sync started")
    
    config = load_config()
    start_date = config.get('start_date', '2025-01-01')
    label_name = config.get('label_name', 'apply')
    scopes = config.get('scopes', [])
    
    gmail_date = start_date.replace('-', '/')
    creds_path = os.path.join(base_dir, "credentials.json")
    token_path = os.path.join(base_dir, "token.pickle")
    
    try:
        service = get_gmail_service(credentials_path=creds_path, token_path=token_path, scopes=scopes)
    except Exception as e:
        st.error(f"Authentication failed: {e}")
        logger.error(f"Authentication failed: {e}")
        return

    query = f"label:{label_name} after:{gmail_date}"
    
    progress_bar = st.progress(0)
    status_text = st.empty()

    def progress_callback(ratio, message):
        progress_bar.progress(ratio)
        status_text.text(message)

    try:
        extractor = EmailExtractor(config=config)
        with Session(engine) as session:
            sync_service = SyncService(session, config, extractor)
            new_count, err_count = sync_service.run_sync(
                service=service, 
                query=query, 
                progress_callback=progress_callback
            )
            
            status_text.empty()
            if err_count > 0:
                st.warning(f"Sync complete with {err_count} errors. Processed {new_count} new emails.")
            else:
                st.success(f"Sync complete. Processed {new_count} new emails.")
            
    except Exception as e:
        st.error(f"Sync failed: {e}")
        logger.error(f"Sync failed: {e}", exc_info=True)


def save_config(config):
    config_path = os.path.join(os.path.dirname(base_dir), "config.yaml")
    with open(config_path, 'w') as f:
        yaml.safe_dump(config, f, default_flow_style=False)

def main():
    st.set_page_config(page_title="Job Tracker", page_icon="💼", layout="wide")
    st.title("💼 Smart Job Application Tracker")
    
    # ... (Database Initialization same as before) ...
    db_url = os.environ.get("DATABASE_URL", "postgresql:///job_tracker")
    engine = create_engine(db_url, pool_pre_ping=True)
    create_db_and_tables(engine)

    with st.sidebar:
        st.header("Actions")
        if st.button("🔄 Sync with Gmail", type="primary"):
            sync_emails(engine)
            
        st.divider()
        st.subheader("Reports")
        
        # Date Range Picker
        today = datetime.now()
        start_of_year = datetime(today.year, 1, 1)
        
        date_range = st.date_input(
            "Report Period",
            value=(start_of_year, today),
            format="DD.MM.YYYY"
        )
        
        col_r1, col_r2 = st.columns(2)
        
        with col_r1:
            if st.button("📄 PDF Report"):
                with Session(engine) as session:
                    apps = session.exec(select(JobApplication)).all()
                    if apps:
                        report_path = os.path.join(base_dir, "report.pdf")
                        config = load_config()
                        
                        start_date = None
                        end_date = None
                        if isinstance(date_range, tuple):
                            if len(date_range) > 0: start_date = date_range[0]
                            if len(date_range) > 1: end_date = date_range[1]
                            
                        generated_file = generate_pdf_report(apps, report_path, start_date=start_date, end_date=end_date, config=config)
                        if generated_file:
                            st.success("PDF Generated!")
                            with open(generated_file, "rb") as file:
                                st.download_button("Download PDF", data=file, file_name="Bewerbungs_Statistik.pdf")

        with col_r2:
            if st.button("📝 Word Report"):
                with Session(engine) as session:
                    apps = session.exec(select(JobApplication)).all()
                    if apps:
                        report_path = os.path.join(base_dir, "report.docx")
                        config = load_config()
                        
                        start_date = None
                        end_date = None
                        if isinstance(date_range, tuple):
                            if len(date_range) > 0: start_date = date_range[0]
                            if len(date_range) > 1: end_date = date_range[1]
                        
                        generated_file = generate_word_report(apps, report_path, start_date=start_date, end_date=end_date, config=config)
                        if generated_file:
                            st.success("DOCX Generated!")
                            with open(generated_file, "rb") as file:
                                st.download_button("Download DOCX", data=file, file_name="Bewerbungs_Bericht.docx")
        
        if st.button("📥 Download Full History (CSV)"):
            with Session(engine) as session:
                # Fetch all events joined with application data for context
                stmt = select(ApplicationEventLog, JobApplication).join(JobApplication)
                results = session.exec(stmt).all()
                
                if results:
                    csv_data = []
                    for event, app in results:
                        csv_data.append({
                            "Company": app.company_name,
                            "Position": app.position,
                            "Event Date": event.event_date.strftime('%Y-%m-%d %H:%M:%S'),
                            "Old Status": event.old_status,
                            "New Status": event.new_status,
                            "Summary": event.summary,
                            "Email Subject": event.email_subject
                        })
                    
                    full_df = pd.DataFrame(csv_data)
                    csv_file = full_df.to_csv(index=False).encode('utf-8')
                    
                    st.download_button(
                        label="Click to Download CSV",
                        data=csv_file,
                        file_name=f"full_application_history_{datetime.now().strftime('%Y%m%d')}.csv",
                        mime='text/csv'
                    )
                else:
                    st.info("No history found.")
        
        st.divider()
        
        col_c1, col_c2 = st.columns(2)
        with col_c1:
            with st.popover("🧹 Clear Cache"):
                st.warning("This will re-process emails on the next sync.")
                if st.button("Confirm Clear"):
                    try:
                        with Session(engine) as session:
                            session.exec(delete(ProcessedEmail))
                            session.commit()
                        st.toast("Cache cleared!", icon="🧹")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error: {e}")

        with col_c2:
            with st.popover("⚠️ Reset DB"):
                st.error("PERMANENTLY DELETE ALL DATA?")
                if st.button("YES, DELETE EVERYTHING"):
                    try:
                        with Session(engine) as session:
                            # Delete in order to respect Foreign Key constraints
                            session.exec(delete(ApplicationEventLog)) 
                            session.exec(delete(JobApplication))
                            session.exec(delete(CompanyEmail)) # Delete emails before company
                            session.exec(delete(Company))
                            session.exec(delete(ProcessedEmail))
                            session.exec(delete(ProcessingLog))
                            session.commit()
                        st.toast("Database cleared!", icon="⚠️")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error: {e}")

    # Metrics & UI
    if "show_all" not in st.session_state:
        st.session_state.show_all = False

    with Session(engine) as session:
        # Fetch all applications for global stats (metrics use this)
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
            query = query.where(JobApplication.is_active == True)
        
        apps = session.exec(query).all()

        # Detailed metrics from tables
        total_interviews = session.exec(select(Interview)).all()
        unique_interview_apps = len(set(i.application_id for i in total_interviews))
        
        total_assessments = session.exec(select(Assessment)).all()
        unique_assessment_apps = len(set(a.application_id for a in total_assessments))
        
        total_offers = session.exec(select(Offer)).all()
        unique_offer_apps = len(set(o.application_id for o in total_offers))
    
    # DataFrames
    df_all = pd.DataFrame([a.model_dump() for a in all_apps]) if all_apps else pd.DataFrame()
    df = pd.DataFrame([a.model_dump() for a in apps]) if apps else pd.DataFrame()
        
    # --- TABS SELECTION ---
    tab_dash, tab_kanban, tab_settings = st.tabs(["📊 Dashboard", "📋 Kanban Board", "⚙️ Settings"])

    with tab_dash:
        if df_all.empty:
            st.info("No applications found. Click 'Sync' to start.")
        else:
            # Display Stats
            c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
            c1.metric("Total Apps", len(df_all))
            c2.metric("Active", len(df_all[df_all['is_active'] == True]))
            
            # Achievement Metrics (Now linked to tables)
            c3.metric("Interviews 🏆", unique_interview_apps, help="Total companies you have interviewed with")
            c4.metric("Assessments 📝", unique_assessment_apps, help="Total companies with assessments")
            
            # Current Ending Statuses
            c5.metric("Offers 🎊", unique_offer_apps, help="Total companies that extended an offer")
            c6.metric("Rejected", len(df_all[df_all['status'] == ApplicationStatus.REJECTED]))
            c7.metric("Pending", len(df_all[df_all['status'] == ApplicationStatus.PENDING]))

            # --- NIT: Link Metrics to Data (Explore section) ---
            with st.expander("🔍 Explore Milestones (Companies)"):
                ec1, ec2, ec3 = st.columns(3)
                
                with ec1:
                    st.markdown("**🤝 Interviews**")
                    # Combine companies from table + companies from flag (migration might not have run if user just started)
                    table_ids = set(i.application_id for i in total_interviews)
                    flag_apps = [a.company_name for a in all_apps if a.reached_interview and a.id not in table_ids]
                    table_apps = [a.company_name for a in all_apps if a.id in table_ids]
                    unique_names = sorted(list(set(table_apps + flag_apps)))
                    for name in unique_names:
                        st.write(f"- {name}")
                
                with ec2:
                    st.markdown("**📝 Assessments**")
                    table_ids_a = set(a.application_id for a in total_assessments)
                    flag_apps_a = [a.company_name for a in all_apps if a.reached_assessment and a.id not in table_ids_a]
                    table_apps_a = [a.company_name for a in all_apps if a.id in table_ids_a]
                    unique_names_a = sorted(list(set(table_apps_a + flag_apps_a)))
                    for name in unique_names_a:
                        st.write(f"- {name}")
                
                with ec3:
                    st.markdown("**🎊 Offers**")
                    table_ids_o = set(o.application_id for o in total_offers)
                    flag_apps_o = [a.company_name for a in all_apps if a.status == ApplicationStatus.OFFER and a.id not in table_ids_o]
                    table_apps_o = [a.company_name for a in all_apps if a.id in table_ids_o]
                    unique_names_o = sorted(list(set(table_apps_o + flag_apps_o)))
                    for name in unique_names_o:
                        st.write(f"- {name}")

            # Dashboard Charts
            col_a, col_b = st.columns(2)
            
            with col_a:
                st.subheader("Process Status")
                if not df.empty:
                    fig = px.pie(df, names='status', hole=0.4)
                    st.plotly_chart(fig, width='stretch')

            with col_b:
                st.subheader("Activity Timeline")
                if not df.empty:
                    df['date'] = pd.to_datetime(df['last_updated']).dt.date
                    timeline = df.groupby('date').size().reset_index(name='count')
                    fig2 = px.bar(timeline, x='date', y='count')
                    st.plotly_chart(fig2, width='stretch')

            st.subheader("Application Pipeline")
            status_options = ["All"] + sorted(df['status'].unique().tolist())
            filter_status = st.selectbox("Filter Table by Status", options=status_options)
            
            df_display = df.copy()
            if filter_status != "All":
                df_display = df_display[df_display['status'] == filter_status]
                
            df_display['Applied Date'] = pd.to_datetime(df_display['created_at']).dt.strftime('%Y-%m-%d')
            df_display['Last Update'] = pd.to_datetime(df_display['last_updated']).dt.strftime('%Y-%m-%d')
            
            # --- Master-Detail View ---
            col_table, col_quick_edit = st.columns([0.65, 0.35])

            with col_table:
                st.dataframe(
                    df_display[['id', 'Applied Date', 'company_name', 'position', 'status', 'Last Update', 'summary', 'notes']].sort_values(by='Last Update', ascending=False),
                    width='stretch',
                    hide_index=True,
                    height=500,
                    column_config={
                        "id": None, # Hide ID column
                        "notes": st.column_config.TextColumn("Notes", width="medium"),
                        "Applied Date": st.column_config.Column(disabled=True),
                        "company_name": st.column_config.Column("Company", disabled=True),
                        "position": st.column_config.Column("Position", disabled=True),
                        "status": st.column_config.Column("Status", disabled=True),
                        "Last Update": st.column_config.Column(disabled=True),
                        "summary": st.column_config.Column("Summary", disabled=True),
                    },
                    key="pipeline_editor",
                    on_select="rerun",
                    selection_mode="single-row"
                )

            with col_quick_edit:
                # Handle selection logic for the quick edit panel
                selected_app_id = None
                editor_state = st.session_state.get("pipeline_editor")
                if editor_state and editor_state.get("selection") and editor_state["selection"].get("rows"):
                    idx = editor_state["selection"]["rows"][0]
                    sorted_df = df_display.sort_values(by='Last Update', ascending=False)
                    if idx < len(sorted_df):
                        selected_app_id = int(sorted_df.iloc[idx]['id'])

                if selected_app_id:
                    with Session(engine) as edit_session:
                        app_to_edit = edit_session.get(JobApplication, selected_app_id)
                        if app_to_edit:
                            st.markdown(f"#### 📝 Quick Edit: {app_to_edit.company_name}")
                            with st.form("quick_edit_form", border=True):
                                new_notes = st.text_area("Notes", value=app_to_edit.notes or "", height=200)
                                
                                # Status update
                                status_vals = [s.value for s in ApplicationStatus]
                                current_status_idx = status_vals.index(app_to_edit.status.value)
                                new_status_val = st.selectbox("Current Status", options=status_vals, index=current_status_idx)
                                
                                if st.form_submit_button("Save Changes", use_container_width=True):
                                    app_to_edit.notes = new_notes
                                    new_status_enum = ApplicationStatus(new_status_val)
                                    
                                    if app_to_edit.status != new_status_enum:
                                        old_s = app_to_edit.status
                                        app_to_edit.status = new_status_enum
                                        app_to_edit.last_updated = datetime.now()
                                        
                                        # Track milestones
                                        if app_to_edit.status == ApplicationStatus.INTERVIEW:
                                            app_to_edit.reached_interview = True
                                        if app_to_edit.status == ApplicationStatus.ASSESSMENT:
                                            app_to_edit.reached_assessment = True
                                            
                                        # Log event
                                        event = ApplicationEventLog(
                                            application_id=app_to_edit.id,
                                            old_status=old_s,
                                            new_status=new_status_enum,
                                            summary="Status manually updated via Dashboard Quick Edit",
                                            email_subject="Manual Update",
                                            event_date=app_to_edit.last_updated
                                        )
                                        edit_session.add(event)
                                    
                                    edit_session.add(app_to_edit)
                                    edit_session.commit()
                                    st.toast("Updated successfully!")
                                    st.rerun()
                else:
                    st.info("💡 Click a row in the table to edit notes or status.")

            st.caption("💡 Tip: Select a row to see full history below.")

    with tab_kanban:
        if not apps:
            st.info("No applications found.")
        else:
            # ... (rest of kanban logic) ...
            st.subheader("Visual Pipeline")
            
            # Define Kanban columns (exclude UNKNOWN for clean view)
            kanban_statuses = [
                ApplicationStatus.APPLIED,
                ApplicationStatus.PENDING,
                ApplicationStatus.COMMUNICATION,
                ApplicationStatus.ASSESSMENT,
                ApplicationStatus.INTERVIEW,
                ApplicationStatus.OFFER,
                ApplicationStatus.REJECTED
            ]
            
            cols = st.columns(len(kanban_statuses))
            
            for i, status in enumerate(kanban_statuses):
                with cols[i]:
                    st.markdown(f"### {status.value}")
                    # Filter apps for this column
                    status_apps = [a for a in apps if a.status == status]
                    
                    for app in status_apps:
                        with st.container(border=True):
                            st.markdown(f"**{app.company_name}**")
                            st.caption(f"{app.position}")
                            if app.summary:
                                st.markdown(f"<small>{app.summary[:60]}...</small>", unsafe_allow_html=True)
                            
                            # Status change trigger
                            new_status_val = st.selectbox(
                                "Move to:",
                                options=[s.value for s in kanban_statuses],
                                index=[s.value for s in kanban_statuses].index(status.value),
                                key=f"move_{app.id}_{app.status}"
                            )
                            
                            if new_status_val != status.value:
                                # Trigger update
                                with Session(engine) as session:
                                    db_app = session.get(JobApplication, app.id)
                                    if db_app:
                                        old_s = db_app.status
                                        db_app.status = ApplicationStatus(new_status_val)
                                        db_app.last_updated = datetime.utcnow()
                                        
                                        # Track milestones
                                        if db_app.status == ApplicationStatus.INTERVIEW:
                                            db_app.reached_interview = True
                                        if db_app.status == ApplicationStatus.ASSESSMENT:
                                            db_app.reached_assessment = True
                                            
                                        # Log manual event
                                        event = ApplicationEventLog(
                                            application_id=db_app.id,
                                            old_status=old_s,
                                            new_status=db_app.status,
                                            summary="Status manually updated by user",
                                            email_subject="Manual Update",
                                            event_date=db_app.last_updated
                                        )
                                        session.add(db_app)
                                        session.add(event)
                                        session.commit()
                                        st.rerun()

    with tab_settings:
        st.subheader("⚙️ Application Settings")
        config = load_config()
        
        with st.form("settings_form"):
            new_label = st.text_input("Gmail Label to Sync", value=config.get('label_name', 'apply'))
            new_start_date = st.date_input("Sync Start Date", value=parser.parse(config.get('start_date', '2025-01-01')).date())
            
            skip_domains = config.get('skip_domains', [])
            new_skip_domains_str = st.text_area("Domains to Skip (one per line)", value="\n".join(skip_domains))
            
            if st.form_submit_button("Save Settings"):
                config['label_name'] = new_label
                config['start_date'] = new_start_date.strftime('%Y-%m-%d')
                config['skip_domains'] = [d.strip() for d in new_skip_domains_str.split("\n") if d.strip()]
                save_config(config)
                st.success("Settings saved successfully!")
                st.rerun()

        st.divider()
        st.subheader("🛠️ Data Tools")
        
        with st.expander("🔄 Merge Duplicate Applications"):
            st.info("Select the 'Target' application you want to keep. The 'Source' application will be merged into it (history, interviews, assessments, offers moved) and then deleted.")
            
            with Session(engine) as session:
                all_apps_merge = session.exec(select(JobApplication).order_by(JobApplication.company_name)).all()
                app_options = {f"{app.company_name} ({app.position or 'No Position'}) - ID: {app.id}": app.id for app in all_apps_merge}
                
                c_m1, c_m2 = st.columns(2)
                
                with c_m1:
                    target_label = st.selectbox("Target (Keep this)", options=list(app_options.keys()), key="merge_target")
                    target_id = app_options[target_label] if target_label else None
                    
                with c_m2:
                    # Filter out the selected target from source options
                    source_options = [k for k in app_options.keys() if app_options[k] != target_id]
                    source_label = st.selectbox("Source (Merge & Delete)", options=source_options, key="merge_source")
                    source_id = app_options[source_label] if source_label else None
                
                if st.button("Merge Applications", type="primary", disabled=not (target_id and source_id)):
                    try:
                        merge_applications(session, source_id, target_id)
                        st.success(f"Successfully merged '{source_label}' into '{target_label}'!")
                        st.rerun()
                    except ValueError as ve:
                        st.error(f"Merge failed: {ve}")
                    except Exception as e:
                        st.error(f"An error occurred: {e}")

    # --- DRILL DOWN / HISTORY VIEW ---
    st.divider()
    st.subheader("🔎 Application Details & History")
    
    # Determine initial selection from table click (if in dashboard tab)
    company_to_show = None
    if not df.empty and 'company_name' in df.columns:
        # st.data_editor selection is stored in st.session_state[key]["selection"]["rows"]
        editor_state = st.session_state.get("pipeline_editor")
        if editor_state and editor_state.get("selection") and editor_state["selection"].get("rows"):
            selected_row_idx = editor_state["selection"]["rows"][0]
            sorted_df = df_display.sort_values(by='Last Update', ascending=False)
            if selected_row_idx < len(sorted_df):
                company_to_show = sorted_df.iloc[selected_row_idx]['company_name']

    options = [""]
    if not df.empty and 'company_name' in df.columns:
        options += sorted(df['company_name'].unique().tolist())
    
    index_to_select = 0
    if company_to_show and company_to_show in options:
        index_to_select = options.index(company_to_show)

    selected_company = st.selectbox("Select Company to View History", options=options, index=index_to_select)
    
    if selected_company:
        # Fetch full details including history
        with Session(engine) as session:
            app_details = session.exec(select(JobApplication).where(JobApplication.company_name == selected_company)).first()
            if app_details:
                # History
                history = session.exec(select(ApplicationEventLog).where(ApplicationEventLog.application_id == app_details.id).order_by(ApplicationEventLog.event_date.desc())).all()
                
                hd1, hd2 = st.columns([1, 2])
                with hd1:
                    st.markdown(f"**Company:** {app_details.company_name}")
                    st.markdown(f"**Position:** {app_details.position}")
                    st.markdown(f"**Status:** {app_details.status.value}")
                    st.markdown(f"**Last Updated:** {app_details.last_updated.strftime('%Y-%m-%d')}")
                    
                    st.divider()
                    with st.expander("✏️ Edit Details"):
                        with st.form("edit_app_form"):
                            new_company = st.text_input("Company Name", value=app_details.company_name)
                            new_position = st.text_input("Position", value=app_details.position)
                            new_notes = st.text_area("User Notes", value=app_details.notes or "")
                            
                            # Status Selection
                            status_options = [s.value for s in ApplicationStatus]
                            current_idx = 0
                            if app_details.status.value in status_options:
                                current_idx = status_options.index(app_details.status.value)
                            
                            new_status_val = st.selectbox("Status", options=status_options, index=current_idx)

                            if st.form_submit_button("Save Changes"):
                                with Session(engine) as edit_session:
                                    db_app = edit_session.get(JobApplication, app_details.id)
                                    if db_app:
                                        # Update basic fields
                                        db_app.company_name = new_company
                                        db_app.position = new_position
                                        db_app.notes = new_notes
                                        
                                        # Handle status change
                                        new_status_enum = ApplicationStatus(new_status_val)
                                        if db_app.status != new_status_enum:
                                            old_status = db_app.status
                                            db_app.status = new_status_enum
                                            db_app.last_updated = datetime.now()
                                            
                                            # Log event
                                            event = ApplicationEventLog(
                                                application_id=db_app.id,
                                                old_status=old_status,
                                                new_status=new_status_enum,
                                                summary="Status manually updated via Edit Details",
                                                email_subject="Manual Update",
                                                event_date=db_app.last_updated
                                            )
                                            edit_session.add(event)
                                        
                                        edit_session.add(db_app)
                                        edit_session.commit()
                                        st.success("Updated!")
                                        st.rerun()
                        
                        st.divider()
                        with st.popover("⚠️ Danger Zone"):
                            st.error("This will permanently delete THIS application and all its history, interviews, assessments, and offers.")
                            confirm_delete = st.checkbox(f"I confirm I want to delete {app_details.company_name} process", key=f"conf_del_{app_details.id}")
                            if st.button("🗑️ Delete Entire Process", type="primary", disabled=not confirm_delete):
                                with Session(engine) as del_session:
                                    db_app_to_del = del_session.get(JobApplication, app_details.id)
                                    if db_app_to_del:
                                        del_session.delete(db_app_to_del)
                                        del_session.commit()
                                        st.toast(f"Deleted {app_details.company_name} successfully!")
                                        st.rerun()
                
                with hd2:
                    dt_hist, dt_interviews, dt_assessments, dt_offers = st.tabs(["📜 History", "🤝 Interviews", "📝 Assessments", "🎊 Offers"])
                    
                    with dt_hist:
                        st.markdown("### Event Log")
                        if history:
                            history_data = []
                            for h in history:
                                history_data.append({
                                    "Date": h.event_date.strftime('%Y-%m-%d %H:%M'),
                                    "Event": f"{h.old_status} -> {h.new_status}" if h.old_status else f"New Application ({h.new_status})",
                                    "Summary": h.summary,
                                    "Subject": h.email_subject
                                })
                            history_df = pd.DataFrame(history_data)
                            st.dataframe(history_df, width="stretch", hide_index=True)
                            
                            csv = history_df.to_csv(index=False).encode('utf-8')
                            st.download_button(
                                label="📥 Download History (CSV)",
                                data=csv,
                                file_name=f"{app_details.company_name}_history.csv",
                                mime='text/csv',
                            )
                        else:
                            st.info("No history events recorded.")
                    
                    with dt_interviews:
                        st.markdown("### Interviews")
                        # Fetch interviews
                        interviews = session.exec(select(Interview).where(Interview.application_id == app_details.id).order_by(Interview.interview_date.desc())).all()
                        
                        # Add new interview
                        with st.popover("➕ Add Interview"):
                            with st.form("add_interview_form"):
                                i_date = st.date_input("Date")
                                i_time = st.time_input("Time")
                                i_interviewer = st.text_input("Interviewer")
                                i_location = st.text_input("Location")
                                i_notes = st.text_area("Notes")
                                if st.form_submit_button("Add Interview"):
                                    new_i = Interview(
                                        application_id=app_details.id,
                                        interview_date=datetime.combine(i_date, i_time),
                                        interviewer=i_interviewer,
                                        location=i_location,
                                        notes=i_notes
                                    )
                                    session.add(new_i)
                                    # Update milestone flag
                                    db_app = session.get(JobApplication, app_details.id)
                                    if db_app:
                                        db_app.reached_interview = True
                                        session.add(db_app)
                                    session.commit()
                                    st.success("Interview added!")
                                    st.rerun()
                        
                        if interviews:
                            for interview in interviews:
                                with st.expander(f"Interview on {interview.interview_date.strftime('%Y-%m-%d %H:%M')}"):
                                    with st.form(f"edit_interview_{interview.id}"):
                                        e_date = st.date_input("Date", value=interview.interview_date.date())
                                        e_time = st.time_input("Time", value=interview.interview_date.time())
                                        e_interviewer = st.text_input("Interviewer", value=interview.interviewer or "")
                                        e_location = st.text_input("Location", value=interview.location or "")
                                        e_notes = st.text_area("Notes", value=interview.notes or "")
                                        
                                        c1, c2 = st.columns(2)
                                        if c1.form_submit_button("Save Changes"):
                                            interview.interview_date = datetime.combine(e_date, e_time)
                                            interview.interviewer = e_interviewer
                                            interview.location = e_location
                                            interview.notes = e_notes
                                            session.add(interview)
                                            session.commit()
                                            st.success("Updated!")
                                            st.rerun()
                                        if c2.form_submit_button("🗑️ Delete"):
                                            session.delete(interview)
                                            session.commit()
                                            st.warning("Deleted!")
                                            st.rerun()
                        else:
                            st.info("No interviews recorded.")

                    with dt_assessments:
                        st.markdown("### Assessments")
                        assessments = session.exec(select(Assessment).where(Assessment.application_id == app_details.id).order_by(Assessment.due_date.desc())).all()
                        
                        with st.popover("➕ Add Assessment"):
                            with st.form("add_assessment_form"):
                                a_date = st.date_input("Due Date")
                                a_type = st.text_input("Type (e.g. Take-home)")
                                a_notes = st.text_area("Notes")
                                if st.form_submit_button("Add Assessment"):
                                    new_a = Assessment(
                                        application_id=app_details.id,
                                        due_date=datetime.combine(a_date, datetime.min.time()),
                                        type=a_type,
                                        notes=a_notes
                                    )
                                    session.add(new_a)
                                    session.commit()
                                    st.success("Assessment added!")
                                    st.rerun()
                        
                        if assessments:
                            for assessment in assessments:
                                title = f"{assessment.type or 'Assessment'} - Due: {assessment.due_date.strftime('%Y-%m-%d') if assessment.due_date else 'N/A'}"
                                with st.expander(title):
                                    with st.form(f"edit_assessment_{assessment.id}"):
                                        e_date = st.date_input("Due Date", value=assessment.due_date.date() if assessment.due_date else datetime.now().date())
                                        e_type = st.text_input("Type", value=assessment.type or "")
                                        e_notes = st.text_area("Notes", value=assessment.notes or "")
                                        
                                        c1, c2 = st.columns(2)
                                        if c1.form_submit_button("Save Changes"):
                                            assessment.due_date = datetime.combine(e_date, datetime.min.time())
                                            assessment.type = e_type
                                            assessment.notes = e_notes
                                            session.add(assessment)
                                            session.commit()
                                            st.success("Updated!")
                                            st.rerun()
                                        if c2.form_submit_button("🗑️ Delete"):
                                            session.delete(assessment)
                                            session.commit()
                                            st.warning("Deleted!")
                                            st.rerun()
                        else:
                            st.info("No assessments recorded.")

                    with dt_offers:
                        st.markdown("### Offers")
                        offers = session.exec(select(Offer).where(Offer.application_id == app_details.id).order_by(Offer.offer_date.desc())).all()
                        
                        with st.popover("➕ Add Offer"):
                            with st.form("add_offer_form"):
                                o_date = st.date_input("Offer Date")
                                o_salary = st.text_input("Salary")
                                o_benefits = st.text_area("Benefits")
                                o_deadline = st.date_input("Deadline")
                                o_notes = st.text_area("Notes")
                                if st.form_submit_button("Add Offer"):
                                    new_o = Offer(
                                        application_id=app_details.id,
                                        offer_date=datetime.combine(o_date, datetime.min.time()),
                                        salary=o_salary,
                                        benefits=o_benefits,
                                        deadline=datetime.combine(o_deadline, datetime.min.time()),
                                        notes=o_notes
                                    )
                                    session.add(new_o)
                                    session.commit()
                                    st.success("Offer added!")
                                    st.rerun()
                                    
                        if offers:
                            for offer in offers:
                                with st.expander(f"Offer from {offer.offer_date.strftime('%Y-%m-%d')}"):
                                    with st.form(f"edit_offer_{offer.id}"):
                                        e_date = st.date_input("Offer Date", value=offer.offer_date.date())
                                        e_salary = st.text_input("Salary", value=offer.salary or "")
                                        e_benefits = st.text_area("Benefits", value=offer.benefits or "")
                                        e_deadline = st.date_input("Deadline", value=offer.deadline.date() if offer.deadline else datetime.now().date())
                                        e_notes = st.text_area("Notes", value=offer.notes or "")
                                        
                                        c1, c2 = st.columns(2)
                                        if c1.form_submit_button("Save Changes"):
                                            offer.offer_date = datetime.combine(e_date, datetime.min.time())
                                            offer.salary = e_salary
                                            offer.benefits = e_benefits
                                            offer.deadline = datetime.combine(e_deadline, datetime.min.time())
                                            offer.notes = e_notes
                                            session.add(offer)
                                            session.commit()
                                            st.success("Updated!")
                                            st.rerun()
                                        if c2.form_submit_button("🗑️ Delete"):
                                            session.delete(offer)
                                            session.commit()
                                            st.warning("Deleted!")
                                            st.rerun()
                        else:
                            st.info("No offers recorded.")


if __name__ == "__main__":
    main()