import os
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML, CSS

def generate_form16_pdf(user_data, ledger_data, tax_data, employer_data, output_filename="Form16_Final.pdf"):
    """
    100% Dynamic PDF Generator.
    Takes data from your FastAPI database and renders the exact replica.
    """
    env = Environment(loader=FileSystemLoader('templates'))
    template = env.get_template('form16_template.html')

    # Data payload inject karna (Exactly HTML variables ke naam par)
    template_vars = {
        "financial_year": "2025-26",
        "assessment_year": "2026-2027",
        
        "employee_name": user_data.name,
        "designation": getattr(user_data, 'designation', 'CLERK'),
        "office_name": getattr(user_data, 'office_school_name', 'UTKRAMIT +2 HIGH SCHOOL, TUBIL'),
        "pan": user_data.pan,
        "father_name": getattr(user_data, 'father_name', 'SHANKAR KUMAR MALLICK'),
        
        "employer_name_address": f"{employer_data.employer_name if employer_data else 'K.B. +2 High School'}, {employer_data.employer_address if employer_data else 'Arki, Khunti'}",
        "tan": employer_data.tan if employer_data else "RANC12345E",
        "employer_pan": employer_data.pan if employer_data else "",
        
        "ledger_data": ledger_data,
        "tax_data": tax_data, # For Page 3 and 4 deductions
        **tax_data  # 🟢 NAYA FIX: Ye Page 1 aur 2 ke liye salary_amount, gross_salary sabko bahar nikal dega
    }

    rendered_html = template.render(template_vars)

    HTML(string=rendered_html).write_pdf(
        output_filename,
        stylesheets=[CSS(string='''
            @page {
                size: A4 portrait;
                margin: 0.5in;
            }
            @page landscape_page {
                size: A4 landscape;
                margin: 0.5in;
            }
            .landscape-section {
                page: landscape_page;
            }
            body { font-family: 'Helvetica', sans-serif; font-size: 11px; }
            table { width: 100%; border-collapse: collapse; margin-bottom: 15px; }
            th, td { border: 1px solid black; padding: 4px; }
            .no-border { border: none; }
            .text-right { text-align: right; }
            .text-center { text-align: center; }
            .bold { font-weight: bold; }
            .page-break { page-break-before: always; }
        ''')]
    )
    return output_filename
