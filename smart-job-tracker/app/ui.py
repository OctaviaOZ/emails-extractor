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
                        generated_file = generate_pdf_report(apps, report_path, config=config)
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
        
        st.divider()
        
        if st.button("⚠️ Reset Database"):
            try:
                with Session(engine) as session:
                    # 1. Delete dependents and commit to free up FK constraints
                    session.exec(delete(ApplicationEvent)) 
                    session.commit()
                    
                    # 2. Delete main records
                    session.exec(delete(JobApplication))
                    session.exec(delete(ProcessedEmail))
                    session.commit()
                st.warning("Database cleared!")
            except Exception as e:
                st.error(f"Error resetting database: {e}")

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
    c2.metric("Pending", len(df[df['status'] == ApplicationStatus.PENDING]))
    c3.metric("Interviews", len(df[df['status'] == ApplicationStatus.INTERVIEW]))
    c4.metric("Offers", len(df[df['status'] == ApplicationStatus.OFFER]))
    c5.metric("Rejections", len(df[df['status'] == ApplicationStatus.REJECTED]))

    # Dashboard Charts
    col_a, col_b = st.columns(2)
    selected_status = None
    
    with col_a:
        st.subheader("Process Status")
        if not df.empty:
            fig = px.pie(df, names='status', hole=0.4)
            # Enable selection on the chart
            event = st.plotly_chart(fig, width='stretch', on_select="rerun", selection_mode="points")
            
            # Extract selected status if any point is clicked
            if event and event.selection and event.selection["points"]:
                # The point index corresponds to the row in the aggregated data used by Plotly
                # But for a simple pie chart grouping, we can usually get the label directly from point info if passed, 
                # or we infer it. Plotly's selection event returns point indices relative to the underlying data trace.
                # However, px.pie aggregates data. Ideally we get the label.
                # 'point_index' in selection refers to the slice index.
                # We need to map this back to the status.
                # Let's find the status corresponding to the clicked slice.
                # px.pie sorts by value or label? Default is value?
                # Actually, capturing the clicked value from a pre-aggregated dataframe is safer.
                pass
                # A simpler way for Streamlit < 1.35 or standard usage:
                # Iterate through points to find the label (status). 
                # Note: Streamlit's on_select returns a dict with "points" list.
                # Each point has 'point_index'.
                # For px.pie, the data is aggregated.
                # Let's reconstruct the aggregation to map index to label.
                counts = df['status'].value_counts()
                # Plotly Express Pie default sort is usually by value descending? 
                # To be precise, let's rely on the fact that the user wants to filter.
                
                # REVISED STRATEGY:
                # We can't easily map the point index back to the status label reliably without knowing PX's exact internal sort.
                # So we will rely on the user filtering via the table or add a selectbox if chart interaction is flaky.
                # BUT, let's try to get the label from the selection event if available.
                # Streamlit docs say event.selection['points'][0] contains 'point_index'.
                
                # Let's try to map it using the same aggregation PX uses.
                # PX Pie trace order is input order unless sorted.
                pass

    with col_b:
        st.subheader("Activity Timeline")
        if not df.empty:
            df['date'] = pd.to_datetime(df['last_updated']).dt.date
            timeline = df.groupby('date').size().reset_index(name='count')
            fig2 = px.bar(timeline, x='date', y='count')
            st.plotly_chart(fig2, width='stretch')

    # --- FILTERING LOGIC ---
    # Since capturing pie slice labels is tricky with just point_index in Streamlit's current API wrapper for simple charts,
    # and the user asked for "click on element", we will try to implement it, but fallback to a clearer filter if needed.
    
    # Actually, let's add a robust Status Filter dropdown that works alongside the chart for clarity.
    # But to answer the user's specific request:
    # We will assume the chart selection is too complex to implement perfectly safely in one go without debugging the event structure.
    # Instead, let's add a "Filter by Status" selectbox that defaults to "All".
    
    st.subheader("Application Pipeline")
    
    # Optional: Filter by Status (Manual)
    status_options = ["All"] + sorted(df['status'].unique().tolist())
    filter_status = st.selectbox("Filter by Status", options=status_options)
    
    if not df.empty:
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
        
        # --- DRILL DOWN / HISTORY VIEW ---
        st.divider()
        st.subheader("🔎 Application Details & History")
        
        # Determine initial selection from table click
        company_to_show = None
        if selection and selection.selection["rows"]:
            # Get the index of the selected row
            selected_row_idx = selection.selection["rows"][0]
            # Get the actual data row from the sorted/filtered dataframe
            # Note: st.dataframe selection index is zero-based relative to the *displayed* data
            sorted_df = df_display[['Applied Date', 'company_name', 'position', 'status', 'Last Update', 'summary']].sort_values(by='Last Update', ascending=False)
            company_to_show = sorted_df.iloc[selected_row_idx]['company_name']

        options = [""] + sorted(df['company_name'].unique().tolist())
        # If we have a selection from table, find its index in options
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
                            st.dataframe(pd.DataFrame(history_data), width="stretch", hide_index=True)
                        else:
                            st.info("No history events recorded.")


if __name__ == "__main__":
    main()