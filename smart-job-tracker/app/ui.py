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
from app.services.report import generate_pdf_report, generate_word_report
from app.models import JobApplication, ApplicationStatus, ProcessedEmail, ApplicationEventLog

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
    skip_emails = config.get('skip_emails', [])
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
    messages = []
    
    with st.spinner("Fetching message list..."):
        try:
            request = service.users().messages().list(userId='me', q=query)
            while request is not None:
                response = request.execute()
                messages.extend(response.get('messages', []))
                request = service.users().messages().list_next(request, response)
        except Exception as e:
            st.error(f"Failed to fetch messages: {e}")
            logger.error(f"Failed to fetch messages: {e}")
            return
    
    if not messages:
        st.info(f"No messages found.")
        logger.info("No messages found.")
        return
    
    # Sort messages by internalDate ascending so we process the history in order
    messages.reverse()

    extractor = EmailExtractor(config=config)
    new_emails_count = 0
    errors_count = 0
    
    progress_bar = st.progress(0)
    status_text = st.empty()
    total_msgs = len(messages)
    
    skip_domains = config.get('skip_domains', [])
    
    logger.info(f"Processing {total_msgs} messages")
    
    for i, msg in enumerate(messages):
        msg_id = msg['id']
        
        try:
            with Session(engine) as session:
                # Check if this specific email has been processed
                processed = session.get(ProcessedEmail, msg_id)
                if processed:
                    progress_bar.progress((i + 1) / total_msgs)
                    continue
                
                logger.info(f"Processing message ID: {msg_id}")
                full_msg = get_message_body(service, msg_id)
                if not full_msg:
                    logger.warning(f"Could not retrieve body for message {msg_id}")
                    continue

                # Skip restricted senders or domains
                sender_full = full_msg.get('sender', '')
                sender_name = ""
                sender_email = sender_full
                
                if '<' in sender_full:
                    match = re.match(r'^(.*?)\s*<(.+)>', sender_full)
                    if match:
                        sender_name, sender_email = match.groups()
                        sender_name = sender_name.strip().replace('"', '')
                        sender_email = sender_email.strip()

                should_skip = False
                if any(email.lower() in sender_email.lower() for email in skip_emails):
                    should_skip = True
                elif any(domain.lower() in sender_email.lower() for domain in skip_domains):
                    should_skip = True
                    
                if should_skip:
                    session.add(ProcessedEmail(email_id=msg_id, company_name="Skipped"))
                    session.commit()
                    progress_bar.progress((i + 1) / total_msgs)
                    logger.info(f"Skipped {sender_email}")
                    continue

                # Parse Date - Use internalDate if available for precision
                email_dt = datetime.now()
                if full_msg.get('internalDate'):
                    try:
                        email_dt = datetime.fromtimestamp(int(full_msg['internalDate']) / 1000.0)
                    except: pass
                elif full_msg.get('date'):
                    try:
                        email_dt = parser.parse(full_msg['date']).replace(tzinfo=None)
                    except: pass

                # Extract Data
                data = extractor.extract(full_msg['subject'], full_msg['sender'], full_msg['text'], full_msg['html'])
                
                # Check if data is valid ApplicationData object
                if not data or data.company_name == "Unknown":
                    session.add(ProcessedEmail(email_id=msg_id, company_name="Unknown"))
                    session.commit()
                    progress_bar.progress((i + 1) / total_msgs)
                    logger.info(f"Unknown company for message {msg_id}")
                    continue

                # --- USE PROCESSOR HERE ---
                processor = ApplicationProcessor(session, config=config)
                email_meta = {
                    'subject': full_msg['subject'],
                    'year': email_dt.year,
                    'month': email_dt.month,
                    'day': email_dt.day,
                    'sender_name': sender_name,
                    'sender_email': sender_email,
                    'snippet': full_msg.get('snippet'),
                    'id': msg_id # Pass ID for the new field
                }
                processor.process_extraction(data, email_meta, email_timestamp=email_dt)
                
                # Mark email as processed
                session.add(ProcessedEmail(email_id=msg_id, company_name=data.company_name))
                session.commit()
                new_emails_count += 1
                
                status_text.text(f"Processing: {data.company_name}...")
                progress_bar.progress((i + 1) / total_msgs)
                logger.info(f"Successfully processed {data.company_name}")
                
        except Exception as e:
            logger.error(f"Error processing message {msg_id}: {e}", exc_info=True)
            errors_count += 1
            progress_bar.progress((i + 1) / total_msgs)
            continue
    
    status_text.empty()
    if errors_count > 0:
        st.warning(f"Sync complete with {errors_count} errors. Processed {new_emails_count} new emails.")
    else:
        st.success(f"Sync complete. Processed {new_emails_count} new emails.")
    logger.info(f"Sync complete. {new_emails_count} new, {errors_count} errors.")

def main():
    st.set_page_config(page_title="Job Tracker", page_icon="💼", layout="wide")
    st.title("💼 Smart Job Application Tracker")
    
    # Database Initialization
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
            if st.button("🧹 Clear Cache"):
                try:
                    with Session(engine) as session:
                        session.exec(delete(ProcessedEmail))
                        session.commit()
                    st.success("Cache cleared!")
                except Exception as e:
                    st.error(f"Error: {e}")

        with col_c2:
            if st.button("⚠️ Reset DB"):
                try:
                    with Session(engine) as session:
                        # 1. Delete dependents and commit to free up FK constraints
                        session.exec(delete(ApplicationEventLog)) 
                        session.commit()
                        
                        # 2. Delete main records
                        session.exec(delete(JobApplication))
                        session.exec(delete(ProcessedEmail))
                        session.commit()
                    st.warning("DB cleared!")
                except Exception as e:
                    st.error(f"Error: {e}")

    # Metrics & UI
    with Session(engine) as session:
        # Fetch active applications by default, or provide a toggle
        show_all = st.checkbox("Show Inactive/Rejected Applications", value=False)
        query = select(JobApplication)
        if not show_all:
            query = query.where(JobApplication.is_active == True)
        
        apps = session.exec(query).all()
        
    if not apps:
        st.info("No applications found. Click 'Sync' to start.")
        return

    df = pd.DataFrame([a.model_dump() for a in apps])
    
    # --- TABS SELECTION ---
    tab_dash, tab_kanban = st.tabs(["📊 Dashboard", "📋 Kanban Board"])

    with tab_dash:
        # Display Stats
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Active Applications", len(df))
        c2.metric("Pending", len(df[df['status'] == ApplicationStatus.PENDING]))
        c3.metric("Interviews", len(df[df['status'] == ApplicationStatus.INTERVIEW]))
        c4.metric("Offers", len(df[df['status'] == ApplicationStatus.OFFER]))
        c5.metric("Rejections", len(df[df['status'] == ApplicationStatus.REJECTED]))

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
        
        # Display main table
        selection = st.dataframe(
            df_display[['Applied Date', 'company_name', 'position', 'status', 'Last Update', 'summary']].sort_values(by='Last Update', ascending=False),
            width='stretch',
            hide_index=True,
            height=400,
            selection_mode="single-row",
            on_select="rerun" 
        )

    with tab_kanban:
        st.subheader("Visual Pipeline")
        
        # Define Kanban columns (exclude UNKNOWN for clean view)
        kanban_statuses = [
            ApplicationStatus.APPLIED,
            ApplicationStatus.PENDING,
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

    # --- DRILL DOWN / HISTORY VIEW ---
    st.divider()
    st.subheader("🔎 Application Details & History")
    
    # Determine initial selection from table click (if in dashboard tab)
    company_to_show = None
    if 'selection' in locals() and selection and selection.selection["rows"]:
        selected_row_idx = selection.selection["rows"][0]
        sorted_df = df_display[['Applied Date', 'company_name', 'position', 'status', 'Last Update', 'summary']].sort_values(by='Last Update', ascending=False)
        company_to_show = sorted_df.iloc[selected_row_idx]['company_name']

    options = [""] + sorted(df['company_name'].unique().tolist())
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
                        new_company = st.text_input("Company Name", value=app_details.company_name)
                        new_position = st.text_input("Position", value=app_details.position)
                        
                        # Status Selection
                        status_options = [s.value for s in ApplicationStatus]
                        current_idx = 0
                        if app_details.status.value in status_options:
                            current_idx = status_options.index(app_details.status.value)
                        
                        new_status_val = st.selectbox("Status", options=status_options, index=current_idx, key="edit_status_select")

                        if st.button("Save Changes"):
                            with Session(engine) as edit_session:
                                db_app = edit_session.get(JobApplication, app_details.id)
                                if db_app:
                                    # Update basic fields
                                    db_app.company_name = new_company
                                    db_app.position = new_position
                                    
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
                
                with hd2:
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


if __name__ == "__main__":
    main()