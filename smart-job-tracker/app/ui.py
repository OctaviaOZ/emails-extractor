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
from app.services.report import generate_pdf_report
from app.models import JobApplication, ApplicationStatus, ProcessedEmail, ApplicationEvent

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
                processor = ApplicationProcessor(session)
                email_meta = {
                    'subject': full_msg['subject'],
                    'year': email_dt.year,
                    'month': email_dt.month,
                    'day': email_dt.day,
                    'sender_name': sender_name,
                    'sender_email': sender_email,
                    'snippet': full_msg.get('snippet')
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
            
        if st.button("📄 Generate Report"):
            with Session(engine) as session:
                apps = session.exec(select(JobApplication)).all()
                if apps:
                    report_path = os.path.join(base_dir, "report.pdf")
                    config = load_config()
                    generated_file = generate_pdf_report(apps, report_path, config=config)
                    if generated_file:
                        st.success("Report generated!")
                        with open(generated_file, "rb") as file:
                            st.download_button("Download PDF", data=file, file_name="Bewerbungs_Statistik.pdf")
        
        if st.button("⚠️ Reset Database"):
            with Session(engine) as session:
                session.exec(delete(JobApplication))
                session.exec(delete(ProcessedEmail))
                session.exec(delete(ApplicationEvent))
                session.commit()
            st.warning("Database cleared!")

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
    
    # Display Stats
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Active Applications", len(df))
    c2.metric("Communications", len(df[df['status'] == ApplicationStatus.COMMUNICATION]))
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
    if not df.empty:
        df['Applied Date'] = pd.to_datetime(df['created_at']).dt.strftime('%Y-%m-%d')
        df['Last Update'] = pd.to_datetime(df['last_updated']).dt.strftime('%Y-%m-%d')
        
        # Display main table
        st.dataframe(
            df[['Applied Date', 'company_name', 'position', 'status', 'Last Update', 'email_subject']].sort_values(by='Last Update', ascending=False),
            width='stretch',
            hide_index=True,
            height=400,
            selection_mode="single-row",
            on_select="rerun" 
            # Note: on_select is a newer Streamlit feature, if not available we use standard drill down below
        )
        
        # --- DRILL DOWN / HISTORY VIEW ---
        st.divider()
        st.subheader("🔎 Application Details & History")
        
        selected_company = st.selectbox("Select Company to View History", options=[""] + sorted(df['company_name'].unique().tolist()))
        
        if selected_company:
            # Fetch full details including history
            with Session(engine) as session:
                app_details = session.exec(select(JobApplication).where(JobApplication.company_name == selected_company)).first()
                if app_details:
                    # History
                    history = session.exec(select(ApplicationEvent).where(ApplicationEvent.application_id == app_details.id).order_by(ApplicationEvent.event_date.desc())).all()
                    
                    hd1, hd2 = st.columns([1, 2])
                    with hd1:
                        st.markdown(f"**Company:** {app_details.company_name}")
                        st.markdown(f"**Position:** {app_details.position}")
                        st.markdown(f"**Status:** {app_details.status.value}")
                        st.markdown(f"**Last Updated:** {app_details.last_updated.strftime('%Y-%m-%d')}")
                    
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
                            st.dataframe(pd.DataFrame(history_data), use_container_width=True, hide_index=True)
                        else:
                            st.info("No history events recorded.")


if __name__ == "__main__":
    main()