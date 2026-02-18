import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import datetime
from sqlmodel import Session, select
from app.models import JobApplication, ApplicationStatus, Interview, Assessment, Offer, ApplicationEventLog
from app.core.database import engine

def render_dashboard(apps, df_all, df):
    if df_all.empty:
        st.info("No applications found. Click 'Sync' to start.")
        return

    with Session(engine) as session:
        # Display Stats
        c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
        c1.metric("Total Apps", len(df_all))
        c2.metric("Active", len(df_all[df_all['is_active']]))
        
        # Achievement Metrics (Now linked to tables)
        total_interviews = session.exec(select(Interview)).all()
        unique_interview_apps = len(set(i.application_id for i in total_interviews))
        
        total_assessments = session.exec(select(Assessment)).all()
        unique_assessment_apps = len(set(a.application_id for a in total_assessments))
        
        total_offers = session.exec(select(Offer)).all()
        unique_offer_apps = len(set(o.application_id for o in total_offers))

        c3.metric("Interviews 🏆", unique_interview_apps, help="Total companies you have interviewed with")
        c4.metric("Assessments 📝", unique_assessment_apps, help="Total companies with assessments")
        
        # Current Ending Statuses
        c5.metric("Offers 🎊", unique_offer_apps, help="Total companies that extended an offer")
        c6.metric("Rejected", len(df_all[df_all['status'] == ApplicationStatus.REJECTED]))
        c7.metric("Pending", len(df_all[df_all['status'] == ApplicationStatus.PENDING]))

        # Explore Milestones
        with st.expander("🔍 Explore Milestones (Companies)"):
            ec1, ec2, ec3 = st.columns(3)
            
            # Helper to get company names
            all_apps_list = session.exec(select(JobApplication)).all()
            
            with ec1:
                st.markdown("**🤝 Interviews**")
                table_ids = set(i.application_id for i in total_interviews)
                flag_apps = [a.company_name for a in all_apps_list if a.reached_interview and a.id not in table_ids]
                table_apps = [a.company_name for a in all_apps_list if a.id in table_ids]
                unique_names = sorted(list(set(table_apps + flag_apps)))
                for name in unique_names:
                    st.write(f"- {name}")
            
            with ec2:
                st.markdown("**📝 Assessments**")
                table_ids_a = set(a.application_id for a in total_assessments)
                flag_apps_a = [a.company_name for a in all_apps_list if a.reached_assessment and a.id not in table_ids_a]
                table_apps_a = [a.company_name for a in all_apps_list if a.id in table_ids_a]
                unique_names_a = sorted(list(set(table_apps_a + flag_apps_a)))
                for name in unique_names_a:
                    st.write(f"- {name}")
            
            with ec3:
                st.markdown("**🎊 Offers**")
                table_ids_o = set(o.application_id for o in total_offers)
                flag_apps_o = [a.company_name for a in all_apps_list if a.status == ApplicationStatus.OFFER and a.id not in table_ids_o]
                table_apps_o = [a.company_name for a in all_apps_list if a.id in table_ids_o]
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
            _render_quick_edit(df_display)

        st.caption("💡 Tip: Select a row to see full history below.")

def _render_quick_edit(df_display):
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
