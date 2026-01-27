import pandas as pd
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from datetime import datetime
import os
from models import ApplicationStatus

def map_status_to_german(status):
    if status == ApplicationStatus.APPLIED:
        return "Beworben"
    elif status == ApplicationStatus.REJECTED:
        return "Absage"
    elif status in [ApplicationStatus.INTERVIEW, ApplicationStatus.ASSESSMENT, ApplicationStatus.OFFER]:
        return "im Prozess"
    elif status == ApplicationStatus.UNKNOWN:
        return "Unbekannt"
    else:
        return str(status)

def generate_pdf_report(applications, output_filename):
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
    
    # Group by company to aggregate data
    for company, group in df.groupby('company_name'):
        # Latest entry (first in sorted group) determines the current status and last contact
        latest = group.iloc[0]
        
        # Calculate Interview Count
        # Count number of emails where status was specifically INTERVIEW
        interview_count = group[group['status'] == ApplicationStatus.INTERVIEW].shape[0]
        
        german_status = map_status_to_german(latest['status'])
        
        report_data.append({
            "Firma": company,
            "Status": german_status,
            "Interview": interview_count,
            "Letzter Kontakt": latest['last_updated'].strftime('%d.%m.%Y'),
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
    title = Paragraph(f"Bewerbungs-Statistik - {datetime.now().strftime('%d.%m.%Y')}", styles['Title'])
    elements.append(title)
    elements.append(Spacer(1, 12))

    # Table Data
    # Headers
    table_data = [['Firma', 'Status', 'Interview', 'Letzter Kontakt']]
    # Rows
    for _, row in report_df.iterrows():
        table_data.append([
            row['Firma'], 
            row['Status'], 
            str(row['Interview']), 
            row['Letzter Kontakt']
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
