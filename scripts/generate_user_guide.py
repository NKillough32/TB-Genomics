#!/usr/bin/env python3
"""
Generate a comprehensive step-by-step PDF user guide for TB Genomics PoC
"""

from reportlab.lib.pagesizes import letter, A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak,
    KeepTogether
)
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
from datetime import datetime

# Create PDF
import os; os.makedirs("docs", exist_ok=True)
pdf_path = "docs/TB_Genomics_PoC_User_Guide.pdf"
doc = SimpleDocTemplate(pdf_path, pagesize=letter, topMargin=0.5*inch, bottomMargin=0.5*inch)
story = []
styles = getSampleStyleSheet()

# Custom styles
title_style = ParagraphStyle(
    'CustomTitle',
    parent=styles['Heading1'],
    fontSize=24,
    textColor=colors.HexColor('#003D5C'),
    spaceAfter=12,
    alignment=TA_CENTER,
    fontName='Helvetica-Bold'
)

heading_style = ParagraphStyle(
    'CustomHeading',
    parent=styles['Heading2'],
    fontSize=14,
    textColor=colors.HexColor('#003D5C'),
    spaceAfter=8,
    spaceBefore=8,
    fontName='Helvetica-Bold',
    borderColor=colors.HexColor('#003D5C'),
    borderWidth=1,
    borderPadding=4,
    leftIndent=6
)

step_style = ParagraphStyle(
    'Step',
    parent=styles['Normal'],
    fontSize=11,
    spaceAfter=6,
    textColor=colors.HexColor('#1a1a1a'),
    leftIndent=20
)

code_style = ParagraphStyle(
    'Code',
    parent=styles['Normal'],
    fontSize=9,
    spaceAfter=6,
    fontName='Courier',
    textColor=colors.HexColor('#333333'),
    leftIndent=20,
    backColor=colors.HexColor('#F5F5F5'),
    borderColor=colors.HexColor('#CCCCCC'),
    borderWidth=1,
    borderPadding=4
)

# Title Page
story.append(Spacer(1, 1*inch))
story.append(Paragraph("NI TB GENOMIC SURVEILLANCE", title_style))
story.append(Paragraph("Proof of Concept System", styles['Heading2']))
story.append(Spacer(1, 0.5*inch))
story.append(Paragraph("<b>Step-by-Step User Guide</b>", styles['Heading3']))
story.append(Spacer(1, 0.3*inch))
story.append(Paragraph(f"<i>Generated: {datetime.now().strftime('%d %B %Y')}</i>", styles['Normal']))
story.append(Spacer(1, 0.5*inch))
story.append(Paragraph(
    "This guide provides current instructions for deploying, configuring, and operating "
    "the TB Genomic Surveillance PoC system on Windows using the updated workflow.",
    styles['Normal']
))

story.append(PageBreak())

# Table of Contents
story.append(Paragraph("TABLE OF CONTENTS", heading_style))
story.append(Spacer(1, 0.2*inch))

toc_items = [
    "1. System Requirements",
    "2. Installation & Setup",
    "3. Database Configuration",
    "4. Backend Server Configuration",
    "5. Frontend Server Setup",
    "6. Generating Synthetic Data",
    "7. Running Analysis Jobs",
    "8. Interpreting Results",
    "9. Troubleshooting"
]

for item in toc_items:
    story.append(Paragraph(f"• {item}", styles['Normal']))

story.append(PageBreak())

# Section 1: System Requirements
story.append(Paragraph("1. SYSTEM REQUIREMENTS", heading_style))
story.append(Spacer(1, 0.15*inch))

reqs = [
    ("Operating System", "Windows 10 or later"),
    ("Python", "Python 3.10 or later"),
    ("PostgreSQL", "PostgreSQL 12 or later"),
    ("RAM", "Minimum 4 GB (8 GB recommended)"),
    ("Disk Space", "500 MB for application + 1 GB for data"),
    ("Network", "Localhost access only (development mode)")
]

for title, requirement in reqs:
    story.append(Paragraph(f"<b>{title}:</b> {requirement}", step_style))

story.append(PageBreak())

# Section 2: Installation & Setup
story.append(Paragraph("2. INSTALLATION & SETUP", heading_style))
story.append(Spacer(1, 0.15*inch))

story.append(Paragraph("<b>Step 2.1: Clone or Extract Project</b>", styles['Heading3']))
story.append(Spacer(1, 0.1*inch))
story.append(Paragraph("Extract the TB-Genomics project folder to your desktop.", step_style))
story.append(Paragraph("<font face='Courier' size='9' color='#333333'>C:\\Users\\YourUsername\\Desktop\\TB-Genomics-main</font>", code_style))

story.append(Spacer(1, 0.2*inch))
story.append(Paragraph("<b>Step 2.2: Create Python Virtual Environment</b>", styles['Heading3']))
story.append(Spacer(1, 0.1*inch))
story.append(Paragraph(
    "Open PowerShell in the project directory and create a Python virtual environment:",
    step_style
))
story.append(Paragraph("<font face='Courier' size='9' color='#333333'>python -m venv .venv</font>", code_style))

story.append(Spacer(1, 0.2*inch))
story.append(Paragraph("<b>Step 2.3: Activate Virtual Environment</b>", styles['Heading3']))
story.append(Spacer(1, 0.1*inch))
story.append(Paragraph("Activate the virtual environment in PowerShell:", step_style))
story.append(Paragraph(r"<font face='Courier' size='9' color='#333333'>.\.venv\Scripts\Activate.ps1</font>", code_style))
story.append(Paragraph("You should see (.venv) at the start of the command line.", step_style))

story.append(Spacer(1, 0.2*inch))
story.append(Paragraph("<b>Step 2.4: Install Python Dependencies</b>", styles['Heading3']))
story.append(Spacer(1, 0.1*inch))
story.append(Paragraph("With the virtual environment active, install required packages:", step_style))
story.append(Paragraph("<font face='Courier' size='9' color='#333333'>pip install -r backend/requirements.txt</font>", code_style))
story.append(Paragraph("This installs FastAPI, SQLAlchemy, matplotlib, networkx, scipy, reportlab, and other dependencies.", step_style))

story.append(Spacer(1, 0.2*inch))
story.append(Paragraph("<b>Step 2.5: Preferred Startup Method (One-Click .bat Files)</b>", styles['Heading3']))
story.append(Spacer(1, 0.1*inch))
story.append(Paragraph(
    "For most users, the easiest and recommended startup method is the provided Windows batch files:",
    step_style
))
story.append(Paragraph("<font face='Courier' size='9' color='#333333'>Setup-And-Start-Platform.bat</font>", code_style))
story.append(Paragraph("First-time setup: creates .venv (if needed), installs dependencies, starts backend + GUI.", step_style))
story.append(Paragraph("<font face='Courier' size='9' color='#333333'>Start-Platform.bat</font>", code_style))
story.append(Paragraph("Normal use: starts backend + GUI.", step_style))
story.append(Paragraph("<font face='Courier' size='9' color='#333333'>Start-Backend.bat</font>", code_style))
story.append(Paragraph("Backend-only mode for API testing or separate GUI hosting.", step_style))
story.append(Paragraph("Sections 4 and 5 below describe manual startup commands if batch launch is not available.", step_style))

story.append(PageBreak())

# Section 3: Database Configuration
story.append(Paragraph("3. DATABASE CONFIGURATION", heading_style))
story.append(Spacer(1, 0.15*inch))

story.append(Paragraph("<b>Step 3.1: Start PostgreSQL Service</b>", styles['Heading3']))
story.append(Spacer(1, 0.1*inch))
story.append(Paragraph(
    "Ensure PostgreSQL is running. Open Services (services.msc) and verify that "
    "'postgresql-x64' service is started.",
    step_style
))

story.append(Spacer(1, 0.2*inch))
story.append(Paragraph("<b>Step 3.2: Connect to PostgreSQL</b>", styles['Heading3']))
story.append(Spacer(1, 0.1*inch))
story.append(Paragraph("Open PowerShell and connect to PostgreSQL. If psql is not on PATH, use the full executable path:", step_style))
story.append(Paragraph("<font face='Courier' size='9' color='#333333'>&amp; \"C:\\Program Files\\PostgreSQL\\18\\bin\\psql.exe\" -U postgres -h localhost -d postgres</font>", code_style))
story.append(Paragraph("Enter your PostgreSQL password when prompted.", step_style))

story.append(Spacer(1, 0.2*inch))
story.append(Paragraph("<b>Step 3.3: Create Database User & Database</b>", styles['Heading3']))
story.append(Spacer(1, 0.1*inch))
story.append(Paragraph("In psql, run these commands:", step_style))
story.append(Paragraph(
    "<font face='Courier' size='9' color='#333333'>CREATE USER tb WITH PASSWORD 'tb';<br/>"
    "CREATE DATABASE tb_surveillance OWNER tb;<br/>"
    "GRANT ALL PRIVILEGES ON DATABASE tb_surveillance TO tb;</font>",
    code_style
))

story.append(Spacer(1, 0.2*inch))
story.append(Paragraph("<b>Step 3.4: Load Database Schema</b>", styles['Heading3']))
story.append(Spacer(1, 0.1*inch))
story.append(Paragraph("Exit psql (type \\q) and load the schema. In PowerShell use the -f flag instead of shell redirection:", step_style))
story.append(Paragraph(
    "<font face='Courier' size='9' color='#333333'>&amp; \"C:\\Program Files\\PostgreSQL\\18\\bin\\psql.exe\" -U tb -h localhost -d tb_surveillance -f .\\db\\schema.sql</font>",
    code_style
))
story.append(Paragraph("When prompted for password, enter 'tb'.", step_style))
story.append(Paragraph("If psql is already on PATH, you can remove the full executable path and keep the same arguments.", step_style))

story.append(PageBreak())

# Section 4: Backend Server
story.append(Paragraph("4. BACKEND SERVER CONFIGURATION", heading_style))
story.append(Spacer(1, 0.15*inch))
story.append(Paragraph("If you started the platform via Start-Platform.bat or Start-Backend.bat, you can skip this section.", step_style))

story.append(Paragraph("<b>Step 4.1: Start Backend Server</b>", styles['Heading3']))
story.append(Spacer(1, 0.1*inch))
story.append(Paragraph(
    "In PowerShell (with virtual environment active), start the FastAPI backend server:",
    step_style
))
story.append(Paragraph(
    "<font face='Courier' size='9' color='#333333'>uvicorn backend.app:app --host 0.0.0.0 --port 8000 --reload</font>",
    code_style
))
story.append(Paragraph("You should see output indicating the server is running on http://0.0.0.0:8000", step_style))

story.append(Spacer(1, 0.2*inch))
story.append(Paragraph("<b>Step 4.2: Verify Backend Health</b>", styles['Heading3']))
story.append(Spacer(1, 0.1*inch))
story.append(Paragraph("Open your browser and visit:", step_style))
story.append(Paragraph("<font face='Courier' size='9' color='#333333'>http://localhost:8000/docs</font>", code_style))
story.append(Paragraph("You should see the FastAPI interactive API documentation page.", step_style))

story.append(Spacer(1, 0.2*inch))
story.append(Paragraph("<b>Note:</b> Leave this terminal running. Open a new PowerShell window for the next steps.", step_style))

story.append(PageBreak())

# Section 5: Frontend Server
story.append(Paragraph("5. FRONTEND SERVER SETUP", heading_style))
story.append(Spacer(1, 0.15*inch))
story.append(Paragraph("If you started the platform via Start-Platform.bat, the GUI is already running and this section can be skipped.", step_style))

story.append(Paragraph("<b>Step 5.1: Navigate to GUI Directory</b>", styles['Heading3']))
story.append(Spacer(1, 0.1*inch))
story.append(Paragraph("In a new PowerShell window, navigate to the GUI directory:", step_style))
story.append(Paragraph(
    "<font face='Courier' size='9' color='#333333'>cd C:\\Users\\YourUsername\\Desktop\\TB-Genomics-main\\gui</font>",
    code_style
))

story.append(Spacer(1, 0.2*inch))
story.append(Paragraph("<b>Step 5.2: Start Frontend Server</b>", styles['Heading3']))
story.append(Spacer(1, 0.1*inch))
story.append(Paragraph("Start the Python HTTP server on port 8081:", step_style))
story.append(Paragraph(
    "<font face='Courier' size='9' color='#333333'>python -m http.server 8081</font>",
    code_style
))

story.append(Spacer(1, 0.2*inch))
story.append(Paragraph("<b>Step 5.3: Access the Application</b>", styles['Heading3']))
story.append(Spacer(1, 0.1*inch))
story.append(Paragraph("Open your browser and visit:", step_style))
story.append(Paragraph(
    "<font face='Courier' size='9' color='#333333'>http://localhost:8081</font>",
    code_style
))
story.append(Paragraph(
    "You should see the NI TB Genomic Surveillance interface with workflow steps for ingest, analysis, search, and governance.",
    step_style
))

story.append(PageBreak())

# Section 6: Synthetic Data
story.append(Paragraph("6. GENERATING SYNTHETIC DATA", heading_style))
story.append(Spacer(1, 0.15*inch))

story.append(Paragraph("<b>Safety note:</b> synthetic seeding is disabled by default to protect operational use.", styles['Heading3']))
story.append(Paragraph("To enable demonstration mode in the backend PowerShell session, set:", step_style))
story.append(Paragraph("<font face='Courier' size='9' color='#333333'>$env:TB_ENABLE_SYNTHETIC_SEEDING=\"1\"</font>", code_style))
story.append(Paragraph("If you also need to run full analysis/reporting on synthetic data, set:", step_style))
story.append(Paragraph("<font face='Courier' size='9' color='#333333'>$env:TB_ALLOW_NON_OPERATIONAL_ACTIONS=\"1\"</font>", code_style))
story.append(Paragraph("For real operational use, leave these variables unset.", step_style))

story.append(Paragraph("<b>Step 6.1: Using the Web Interface</b>", styles['Heading3']))
story.append(Spacer(1, 0.1*inch))
story.append(Paragraph("In the browser on http://localhost:8081:", step_style))
story.append(Paragraph("1. Navigate to <b>Step 2 — Upload data</b>", step_style))
story.append(Paragraph("2. Set <b>Cases: 250</b> (or desired number)", step_style))
story.append(Paragraph("3. Set <b>Seed: 42</b> (for reproducible results)", step_style))
story.append(Paragraph("4. Check <b>Reset existing synthetic data first</b>", step_style))
story.append(Paragraph("5. Click <b>Generate synthetic dataset</b>", step_style))
story.append(Paragraph("6. Wait for confirmation message", step_style))

story.append(Spacer(1, 0.2*inch))
story.append(Paragraph("<b>What Happens:</b>", styles['Heading3']))
story.append(Paragraph(
    "• The system fetches current TB incidence data from the World Bank API<br/>"
    "• 250 realistic synthetic TB cases are generated using country-weighted distribution<br/>"
    "• Each case is assigned lineage, resistance profile, and location<br/>"
    "• Cases are organized into 4-6 outbreak clusters<br/>"
    "• All data is pseudonymised and stored in the database",
    step_style
))

story.append(PageBreak())

# Section 7: Running Analysis
story.append(Paragraph("7. RUNNING ANALYSIS JOBS", heading_style))
story.append(Spacer(1, 0.15*inch))

story.append(Paragraph("<b>Step 7.1: Run Clustering Analysis</b>", styles['Heading3']))
story.append(Spacer(1, 0.1*inch))
story.append(Paragraph("In <b>Step 3 — Run analysis</b>:", step_style))
story.append(Paragraph("1. Click <b>Run clustering</b>", step_style))
story.append(Paragraph("2. A progress bar will appear and update as the job runs", step_style))
story.append(Paragraph("3. When complete, status shows <b>Completed</b>", step_style))
story.append(Paragraph(
    "Result: Cases are analyzed for genetic similarity and grouped into clusters.",
    step_style
))

story.append(Spacer(1, 0.2*inch))
story.append(Paragraph("<b>Step 7.2: Generate Outbreaker2 Inputs</b>", styles['Heading3']))
story.append(Spacer(1, 0.1*inch))
story.append(Paragraph("In <b>Step 3 — Run analysis</b>:", step_style))
story.append(Paragraph("1. Click <b>Generate outbreaker2 inputs</b>", step_style))
story.append(Paragraph("2. Wait for completion", step_style))
story.append(Paragraph(
    "Result: Case data and DNA sequences are prepared for outbreak investigation analysis.",
    step_style
))

story.append(Spacer(1, 0.2*inch))
story.append(Paragraph("<b>Step 7.3: Run Outbreaker2 Analysis</b>", styles['Heading3']))
story.append(Spacer(1, 0.1*inch))
story.append(Paragraph("In <b>Step 3 — Run analysis</b>:", step_style))
story.append(Paragraph("1. Click <b>Run outbreaker2</b>", step_style))
story.append(Paragraph("2. This may take 30-60 seconds", step_style))
story.append(Paragraph("3. Status will show when analysis completes", step_style))
story.append(Paragraph(
    "Result: Outbreak analysis completes and generates diagnostic plots, an enhanced transmission network, and a PDF outbreak report.",
    step_style
))
story.append(Paragraph(
    "Note: If R/outbreaker2 is unavailable, the system falls back to a mock report generator so the workflow remains usable for demos.",
    step_style
))

story.append(PageBreak())

# Section 8: Interpreting Results
story.append(Paragraph("8. INTERPRETING RESULTS", heading_style))
story.append(Spacer(1, 0.15*inch))

story.append(Paragraph("<b>Step 8.1: View Case Summary</b>", styles['Heading3']))
story.append(Spacer(1, 0.1*inch))
story.append(Paragraph("In <b>Step 4 — Results</b>:", step_style))
story.append(Paragraph("1. Click <b>View cases</b>", step_style))
story.append(Paragraph("2. A raw JSON view of all cases is displayed", step_style))
story.append(Paragraph("Include fields: case ID, specimen date, region, lineage, resistance profile", step_style))

story.append(Spacer(1, 0.2*inch))
story.append(Paragraph("<b>Step 8.2: View Outbreak Analysis Plots</b>", styles['Heading3']))
story.append(Spacer(1, 0.1*inch))
story.append(Paragraph("In <b>Step 4 — Results</b>:", step_style))
story.append(Paragraph("1. Click <b>View outbreaker2 summary</b>", step_style))
story.append(Paragraph("2. The results panel displays:", step_style))
story.append(Paragraph(
    "• <b>Case Summary:</b> Total cases, clustered cases, unclustered cases<br/>"
    "• <b>Outbreak Analysis:</b> MCMC analysis status and likelihood statistics<br/>"
    "• <b>Transmission Network Insights:</b> Node count, high-confidence links, and ranked priority spreaders<br/>"
    "• <b>Diagnostic Plots:</b><br/>"
    "&nbsp;&nbsp;- <b>MCMC Trace:</b> Shows convergence of likelihood estimate<br/>"
    "&nbsp;&nbsp;- <b>Posterior Distributions:</b> 4-panel histogram of key parameters<br/>"
    "&nbsp;&nbsp;- <b>Transmission Tree:</b> Network diagram showing inferred transmission chains<br/>"
    "&nbsp;&nbsp;- <b>Phylogenetic Tree:</b> Genetic similarity clustering of isolates<br/>"
    "&nbsp;&nbsp;- <b>Resistance Heatmap:</b> Drug resistance pattern overview across cases",
    step_style
))

story.append(Spacer(1, 0.2*inch))
story.append(Paragraph("<b>Step 8.3: Download the Outbreak Report PDF</b>", styles['Heading3']))
story.append(Spacer(1, 0.1*inch))
story.append(Paragraph("In <b>Step 4 — Results</b>:", step_style))
story.append(Paragraph("1. Click <b>Download outbreak report (PDF)</b>", step_style))
story.append(Paragraph("2. Open the generated report", step_style))
story.append(Paragraph("3. The updated report preserves image aspect ratios so plots are not stretched or squashed", step_style))

story.append(Spacer(1, 0.2*inch))
story.append(Paragraph("<b>Step 8.4: View Governance & Audit Trail</b>", styles['Heading3']))
story.append(Spacer(1, 0.1*inch))
story.append(Paragraph("In <b>Step 5 — Governance & Compliance</b>:", step_style))
story.append(Paragraph("1. Click <b>View audit trail</b>", step_style))
story.append(Paragraph("2. A detailed log of all system actions is displayed with timestamps", step_style))
story.append(Paragraph(
    "This provides accountability for all data processing and analysis operations.",
    step_style
))

story.append(PageBreak())

# Section 9: Troubleshooting
story.append(Paragraph("9. TROUBLESHOOTING", heading_style))
story.append(Spacer(1, 0.15*inch))

issues = [
    ("Browser cannot connect to backend", 
     "Ensure backend is running (uvicorn command). Check firewall settings. Verify port 8000 is available."),
    ("Database connection error", 
     "Verify PostgreSQL is running. Check user 'tb' exists with password 'tb'. Ensure database 'tb_surveillance' was created."),
    ("psql command is not recognized", 
     "Use the full PostgreSQL executable path or add the PostgreSQL bin directory to PATH. In PowerShell, load schema with `psql -f .\\db\\schema.sql`, not `< db/schema.sql`."),
    ("Synthetic data generation fails", 
     "Check internet connection (World Bank API is called). If offline, system uses fallback values. Verify database is accessible."),
    ("Analysis jobs timeout or fail", 
     "Check disk space in exports/ folder. Verify all Python dependencies are installed. Try regenerating synthetic data."),
    ("Graphics not displaying", 
     "Ensure matplotlib is installed. Check exports/ folder contains PNG files. Try refreshing browser (Ctrl+F5)."),
    ("PDF images look distorted", 
     "Regenerate the outbreak report from the updated backend. The current report code scales images proportionally to preserve aspect ratio."),
]

for issue, solution in issues:
    story.append(Paragraph(f"<b>❌ {issue}</b>", styles['Heading3']))
    story.append(Paragraph(f"✓ {solution}", step_style))
    story.append(Spacer(1, 0.15*inch))

story.append(PageBreak())

# Final Notes
story.append(Paragraph("NEXT STEPS & SUPPORT", heading_style))
story.append(Spacer(1, 0.15*inch))

story.append(Paragraph("<b>After PoC Validation:</b>", styles['Heading3']))
story.append(Paragraph(
    "• Deploy to test environment<br/>"
    "• Connect to real TB genomic data sources<br/>"
    "• Configure authentication (LDAP/AD integration)<br/>"
    "• Set up database backup and recovery procedures<br/>"
    "• Deploy Outbreaker2 R package on analysis servers<br/>"
    "• Implement production monitoring and logging",
    step_style
))

story.append(Spacer(1, 0.3*inch))
story.append(Paragraph("<b>Support Resources:</b>", styles['Heading3']))
story.append(Paragraph(
    "• FastAPI Documentation: https://fastapi.tiangolo.com<br/>"
    "• Outbreaker2 Package: https://www.repidemicsconsortium.org/outbreaker2<br/>"
    "• PostgreSQL Documentation: https://www.postgresql.org/docs",
    step_style
))

# Build PDF
doc.build(story)
print(f"✓ PDF generated successfully: {pdf_path}")
print(f"  Location: {__file__.rsplit(chr(92), 1)[0]}\\{pdf_path}")
