import streamlit as st
import pandas as pd
from sqlmodel import Session, select, create_engine, SQLModel, delete
from datetime import datetime
import plotly.express as px
import os
import yaml
from dotenv import load_dotenv
from dateutil import parser

from services.gmail import get_gmail_service, get_message_body
from services.extractor import EmailExtractor
from services.report import generate_pdf_report
from models import JobApplication, ApplicationStatus, ProcessedEmail, STATUS_RANK

# --- Load Environment Variables ---
base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
    
    config = load_config()
    start_date = config.get('start_date', '2025-01-01')
    label_name = config.get('label_name', 'apply')
    skip_emails = config.get('skip_emails', [])
    
    gmail_date = start_date.replace('-', '/')
    creds_path = os.path.join(base_dir, "credentials.json")
    token_path = os.path.join(base_dir, "token.pickle")
    
    try:
        service = get_gmail_service(credentials_path=creds_path, token_path=token_path)
    except Exception as e:
        st.error(f"Authentication failed: {e}")
        return

    query = f"label:{label_name} after:{gmail_date}"
    messages = []
    
    with st.spinner("Fetching message list..."):
        request = service.users().messages().list(userId='me', q=query)
        while request is not None:
            response = request.execute()
            messages.extend(response.get('messages', []))
            request = service.users().messages().list_next(request, response)
    
    if not messages:
        st.info(f"No messages found.")
        return
    
    # Sort messages by internalDate ascending so we process the history in order
    # (Simplified: Gmail returns newest first, so we reverse it)
    messages.reverse()

    extractor = EmailExtractor()
    new_emails_count = 0
    
    with Session(engine) as session:
        progress_bar = st.progress(0)
        total_msgs = len(messages)
        
        for i, msg in enumerate(messages):
            msg_id = msg['id']
            
            # Check if this specific email has been processed
            processed = session.get(ProcessedEmail, msg_id)
            if processed:
                progress_bar.progress((i + 1) / total_msgs)
                continue
            
            full_msg = get_message_body(service, msg_id)
            if not full_msg: continue

            # Skip restricted senders
            sender_email = full_msg.get('sender', '').lower()
            if any(email.lower() in sender_email for email in skip_emails):
                session.add(ProcessedEmail(email_id=msg_id, company_name="Skipped"))
                progress_bar.progress((i + 1) / total_msgs)
                continue

            # Parse Date
            email_dt = datetime.now()
            if full_msg.get('date'):
                try:
                    email_dt = parser.parse(full_msg['date']).replace(tzinfo=None)
                except: pass

            # Extract Data
            data = extractor.extract(full_msg['subject'], full_msg['sender'], full_msg['text'], full_msg['html'])
            
            if data.company_name == "Unknown":
                session.add(ProcessedEmail(email_id=msg_id, company_name="Unknown"))
                progress_bar.progress((i + 1) / total_msgs)
                continue

            # Process Tracking Logic
            app = session.exec(select(JobApplication).where(JobApplication.company_name == data.company_name)).first()
            
            if not app:
                # Create new process
                app = JobApplication(
                    company_name=data.company_name,
                    position=data.position,
                    status=data.status,
                    applied_at=email_dt,
                    last_updated=email_dt,
                    email_subject=full_msg['subject'],
                    email_snippet=full_msg['snippet'],
                    year=email_dt.year,
                    month=email_dt.month,
                    day=email_dt.day
                )
                session.add(app)
            else:
                # Update existing process status if the new status is "higher" in rank
                current_rank = STATUS_RANK.get(app.status, 0)
                new_rank = STATUS_RANK.get(data.status, 0)
                
                if new_rank >= current_rank:
                    app.status = data.status
                
                # Update last updated if this email is newer
                if email_dt > app.last_updated:
                    app.last_updated = email_dt
                    app.email_subject = full_msg['subject']
                    app.email_snippet = full_msg['snippet']

            # Mark email as processed
            session.add(ProcessedEmail(email_id=msg_id, company_name=data.company_name))
            new_emails_count += 1
            progress_bar.progress((i + 1) / total_msgs)
        
        session.commit()
    
    st.success(f"Sync complete. Processed {new_emails_count} new emails.")

def main():
    st.set_page_config(page_title="Job Tracker", page_icon="💼", layout="wide")
    st.title("💼 Smart Job Application Tracker")
    
    # Database Initialization
    db_url = os.environ.get("DATABASE_URL", "postgresql:///job_tracker")
    engine = create_engine(db_url)
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
                    generated_file = generate_pdf_report(apps, report_path)
                    if generated_file:
                        st.success("Report generated!")
                        with open(generated_file, "rb") as file:
                            st.download_button("Download PDF", data=file, file_name="Bewerbungs_Statistik.pdf")
        
        if st.button("⚠️ Reset Database"):
            with Session(engine) as session:
                session.exec(delete(JobApplication))
                session.exec(delete(ProcessedEmail))
                session.commit()
            st.warning("Database cleared!")

    # Metrics & UI
    with Session(engine) as session:
        apps = session.exec(select(JobApplication)).all()
        
    if not apps:
        st.info("No applications found. Click 'Sync' to start.")
        return

    df = pd.DataFrame([a.model_dump() for a in apps])
    
    # Display Stats
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Applications", len(df))
    c2.metric("Communications", len(df[df['status'] == ApplicationStatus.COMMUNICATION]))
    c3.metric("Interviews", len(df[df['status'] == ApplicationStatus.INTERVIEW]))
    c4.metric("Offers", len(df[df['status'] == ApplicationStatus.OFFER]))
    c5.metric("Rejections", len(df[df['status'] == ApplicationStatus.REJECTED]))

    # Dashboard Charts
    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("Process Status")
        fig = px.pie(df, names='status', hole=0.4)
        st.plotly_chart(fig, use_container_width=True)
    with col_b:
        st.subheader("Activity Timeline")
        df['date'] = pd.to_datetime(df['last_updated']).dt.date
        timeline = df.groupby('date').size().reset_index(name='count')
        fig2 = px.bar(timeline, x='date', y='count')
        st.plotly_chart(fig2, use_container_width=True)

    st.subheader("Application Pipeline")
    df['Applied Date'] = pd.to_datetime(df['applied_at']).dt.strftime('%Y-%m-%d')
    df['Last Email'] = pd.to_datetime(df['last_updated']).dt.strftime('%Y-%m-%d')
    
    st.dataframe(
        df[['Applied Date', 'company_name', 'status', 'Last Email', 'email_subject']].sort_values(by='Last Email', ascending=False),
        use_container_width=True,
        hide_index=True,
        height=600
    )

if __name__ == "__main__":
    main()
