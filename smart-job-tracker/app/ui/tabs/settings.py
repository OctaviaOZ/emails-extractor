import streamlit as st
from datetime import datetime
from sqlmodel import Session, select
from dateutil import parser
from app.core.config import settings, save_settings
from app.core.database import engine
from app.models import JobApplication
from app.services.merge import merge_applications

def render_settings():
    st.subheader("⚙️ Application Settings")
    
    with st.form("settings_form"):
        new_label = st.text_input("Gmail Label to Sync", value=settings.label_name)
        new_start_date = st.date_input("Sync Start Date", value=parser.parse(settings.start_date).date())
        
        skip_domains = settings.skip_domains
        new_skip_domains_str = st.text_area("Domains to Skip (one per line)", value="\n".join(skip_domains))
        
        if st.form_submit_button("Save Settings"):
            settings.label_name = new_label
            settings.start_date = new_start_date.strftime('%Y-%m-%d')
            settings.skip_domains = [d.strip() for d in new_skip_domains_str.split("\n") if d.strip()]
            save_settings(settings)
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
