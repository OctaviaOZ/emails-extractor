import streamlit as st
from datetime import datetime
from sqlmodel import Session, select, delete
from app.models import JobApplication, ApplicationEventLog, ProcessedEmail, Company, CompanyEmail, ProcessingLog
from app.services.report import generate_pdf_report, generate_word_report
from app.services.sync import SyncService
from app.services.extractor import EmailExtractor
from app.services.gmail import get_gmail_service
from app.core.config import settings
from app.core.database import engine
import logging

logger = logging.getLogger(__name__)

def render_sidebar():
    with st.sidebar:
        st.header("Actions")
        if st.button("🔄 Sync with Gmail", type="primary"):
            _sync_emails()
            
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
                _generate_pdf(date_range)

        with col_r2:
            if st.button("📝 Word Report"):
                _generate_word(date_range)
        
        if st.button("📥 Download Full History (CSV)"):
            _download_csv()
        
        st.divider()
        
        col_c1, col_c2 = st.columns(2)
        with col_c1:
            with st.popover("🧹 Clear Cache"):
                st.warning("This will re-process emails on the next sync.")
                if st.button("Confirm Clear"):
                    _clear_cache()

        with col_c2:
            with st.popover("⚠️ Reset DB"):
                st.error("PERMANENTLY DELETE ALL DATA?")
                if st.button("YES, DELETE EVERYTHING"):
                    _reset_db()

def _sync_emails():
    st.toast("Starting Sync...", icon="🔄")
    
    gmail_date = settings.start_date.replace('-', '/')
    
    try:
        service = get_gmail_service(
            credentials_path=str(settings.credentials_path), 
            token_path=str(settings.token_path), 
            scopes=settings.scopes
        )
    except Exception as e:
        st.error(f"Authentication failed: {e}")
        logger.error(f"Authentication failed: {e}")
        return

    query = f"label:{settings.label_name} after:{gmail_date}"
    
    progress_bar = st.progress(0)
    status_text = st.empty()

    def progress_callback(ratio, message):
        progress_bar.progress(ratio)
        status_text.text(message)

    try:
        # Extractor now uses singleton settings, no need to pass config
        extractor = EmailExtractor()
        with Session(engine) as session:
            sync_service = SyncService(session, extractor)
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

def _generate_pdf(date_range):
    with Session(engine) as session:
        apps = session.exec(select(JobApplication)).all()
        if apps:
            report_path = settings.base_dir / "report.pdf"
            
            start_date = None
            end_date = None
            if isinstance(date_range, tuple):
                if len(date_range) > 0:
                    start_date = date_range[0]
                if len(date_range) > 1:
                    end_date = date_range[1]
                
            # Convert settings to dict for compatibility or update generate_pdf_report to use settings
            # Using settings.model_dump() as config
            config_dict = settings.model_dump()
            
            generated_file = generate_pdf_report(apps, str(report_path), start_date=start_date, end_date=end_date, config=config_dict)
            if generated_file:
                st.success("PDF Generated!")
                with open(generated_file, "rb") as file:
                    st.download_button("Download PDF", data=file, file_name="Bewerbungs_Statistik.pdf")

def _generate_word(date_range):
    with Session(engine) as session:
        apps = session.exec(select(JobApplication)).all()
        if apps:
            report_path = settings.base_dir / "report.docx"
            
            start_date = None
            end_date = None
            if isinstance(date_range, tuple):
                if len(date_range) > 0:
                    start_date = date_range[0]
                if len(date_range) > 1:
                    end_date = date_range[1]
            
            config_dict = settings.model_dump()
            
            generated_file = generate_word_report(apps, str(report_path), start_date=start_date, end_date=end_date, config=config_dict)
            if generated_file:
                st.success("DOCX Generated!")
                with open(generated_file, "rb") as file:
                    st.download_button("Download DOCX", data=file, file_name="Bewerbungs_Bericht.docx")

def _download_csv():
    import pandas as pd
    with Session(engine) as session:
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

def _clear_cache():
    try:
        with Session(engine) as session:
            session.exec(delete(ProcessedEmail))
            session.commit()
        st.toast("Cache cleared!", icon="🧹")
        st.rerun()
    except Exception as e:
        st.error(f"Error: {e}")

def _reset_db():
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
