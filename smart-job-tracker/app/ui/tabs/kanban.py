import streamlit as st
from datetime import datetime
from sqlmodel import Session
from app.models import JobApplication, ApplicationStatus, ApplicationEventLog
from app.core.database import engine
from app.core.constants import KANBAN_STATUSES

def render_kanban(apps):
    if not apps:
        st.info("No applications found.")
        return

    st.subheader("Visual Pipeline")
    
    cols = st.columns(len(KANBAN_STATUSES))
    
    for i, status in enumerate(KANBAN_STATUSES):
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
                        options=[s.value for s in KANBAN_STATUSES],
                        index=[s.value for s in KANBAN_STATUSES].index(status.value),
                        key=f"move_{app.id}_{app.status}"
                    )
                    
                    if new_status_val != status.value:
                        _update_status(app, new_status_val)

def _update_status(app, new_status_val):
    with Session(engine) as session:
        db_app = session.get(JobApplication, app.id)
        if db_app:
            old_s = db_app.status
            db_app.status = ApplicationStatus(new_status_val)
            db_app.last_updated = datetime.now()
            
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
