import streamlit as st
import pandas as pd
from datetime import datetime
from sqlmodel import Session, select
from app.models import JobApplication, ApplicationEventLog, ApplicationStatus, Interview, Assessment, Offer
from app.core.database import engine

def render_history_view(df, df_display):
    st.divider()
    st.subheader("🔎 Application Details & History")
    
    # Determine initial selection from table click
    company_to_show = None
    if not df.empty and 'company_name' in df.columns:
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
        _render_company_details(selected_company)

def _render_company_details(selected_company):
    with Session(engine) as session:
        app_details = session.exec(select(JobApplication).where(JobApplication.company_name == selected_company)).first()
        if not app_details:
            return

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
                _render_edit_form(session, app_details)
            
            st.divider()
            with st.popover("⚠️ Danger Zone"):
                st.error("This will permanently delete THIS application and all its history, interviews, assessments, and offers.")
                confirm_delete = st.checkbox(f"I confirm I want to delete {app_details.company_name} process", key=f"conf_del_{app_details.id}")
                if st.button("🗑️ Delete Entire Process", type="primary", disabled=not confirm_delete):
                    session.delete(app_details)
                    session.commit()
                    st.toast(f"Deleted {app_details.company_name} successfully!")
                    st.rerun()
        
        with hd2:
            dt_hist, dt_interviews, dt_assessments, dt_offers = st.tabs(["📜 History", "🤝 Interviews", "📝 Assessments", "🎊 Offers"])
            
            with dt_hist:
                _render_event_log(history, app_details)
            
            with dt_interviews:
                _render_interviews(session, app_details)

            with dt_assessments:
                _render_assessments(session, app_details)

            with dt_offers:
                _render_offers(session, app_details)

def _render_edit_form(session, app_details):
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
            db_app = session.get(JobApplication, app_details.id)
            if db_app:
                db_app.company_name = new_company
                db_app.position = new_position
                db_app.notes = new_notes
                
                new_status_enum = ApplicationStatus(new_status_val)
                if db_app.status != new_status_enum:
                    old_status = db_app.status
                    db_app.status = new_status_enum
                    db_app.last_updated = datetime.now()
                    
                    event = ApplicationEventLog(
                        application_id=db_app.id,
                        old_status=old_status,
                        new_status=new_status_enum,
                        summary="Status manually updated via Edit Details",
                        email_subject="Manual Update",
                        event_date=db_app.last_updated
                    )
                    session.add(event)
                
                session.add(db_app)
                session.commit()
                st.success("Updated!")
                st.rerun()

def _render_event_log(history, app_details):
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

def _render_interviews(session, app_details):
    st.markdown("### Interviews")
    interviews = session.exec(select(Interview).where(Interview.application_id == app_details.id).order_by(Interview.interview_date.desc())).all()
    
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

def _render_assessments(session, app_details):
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

def _render_offers(session, app_details):
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
