import pandas as pd
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from datetime import datetime
import os
from app.models import ApplicationStatus

def get_status_label(status, mapping=None):
    """
    Returns the localized label for a status using the provided mapping.
    Defaults to the English enum value if no mapping is found.
    """
    if mapping:
        # Check for direct match (e.g., 'Applied')
        if status.value in mapping:
            return mapping[status.value]
        # Check for match by key (e.g., if mapping uses 'Applied' as key)
        if status in mapping:
            return mapping[status]
            
    # Default Fallback (English / Raw Value)
    return status.value

def generate_pdf_report(applications, output_filename, config=None):
    """
    Generates a PDF report from a list of JobApplication objects.
    """
    if not applications:
        return None

    # Convert list of JobApplication objects to DataFrame
    data = [app.model_dump() for app in applications]
    df = pd.DataFrame(data)

    if df.empty:
        return None

    # Ensure last_updated is datetime
    df['last_updated'] = pd.to_datetime(df['last_updated'])

    # Sort by date descending to prioritize latest entries
    df = df.sort_values(by='last_updated', ascending=False)

    report_data = []
    
    mapping = config.get('report_mapping', {}) if config else {}

    # Group by company to aggregate data
    # Note: With multi-process support, a company might appear multiple times.
    # This report aggregates by company, showing the latest status.
    for company, group in df.groupby('company_name'):
        # Latest entry (first in sorted group) determines the current status and last contact
        latest = group.iloc[0]
        
        # Calculate Interview Count
        # Count number of applications where status is specifically INTERVIEW
        # Note: This is a simplified metric. Ideally we would query ApplicationEvent.
        interview_count = group[group['status'] == ApplicationStatus.INTERVIEW].shape[0]
        
        status_label = get_status_label(latest['status'], mapping)
        
        report_data.append({
            "Company": company,
            "Status": status_label,
            "Interviews": interview_count,
            "Last Contact": latest['last_updated'].strftime('%d.%m.%Y'),
            "_sort_date": latest['last_updated'] # Helper for sorting
        })

    # Create DataFrame for Report
    report_df = pd.DataFrame(report_data)
    
    # Sort by Last Contact desc
    report_df = report_df.sort_values(by='_sort_date', ascending=False)

    # Generate PDF
    doc = SimpleDocTemplate(output_filename, pagesize=letter)
    elements = []
    
    styles = getSampleStyleSheet()
    title = Paragraph(f"Application Report - {datetime.now().strftime('%Y-%m-%d')}", styles['Title'])
    elements.append(title)
    elements.append(Spacer(1, 12))

    # Table Data
    # Headers - customizable via config could be an enhancement, hardcoded English for now
    headers = ['Company', 'Status', 'Interviews', 'Last Contact']
    table_data = [headers]
    
    # Rows
    for _, row in report_df.iterrows():
        table_data.append([
            row['Company'], 
            row['Status'], 
            str(row['Interviews']), 
            row['Last Contact']
        ])

    # Table Style
    table = Table(table_data)
    style = TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
        ('GRID', (0, 0), (-1, -1), 1, colors.black)
    ])
    table.setStyle(style)
    elements.append(table)

    try:
        doc.build(elements)
        return output_filename
    except Exception as e:
        print(f"Error generating PDF: {e}")
        return None