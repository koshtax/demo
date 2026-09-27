import os
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML, CSS

def generate_form16_pdf(user_data, ledger_data, tax_data, employer_data, output_filename="Form16_Final.pdf"):
    """
    100% Dynamic PDF Generator.
    Takes data from your FastAPI database and renders the exact replica.
    """
    # Jinja2 setup to load the HTML template
    env = Environment(loader=FileSystemLoader('templates'))
    template = env.get_template('form16_template.html')

    # Data payload inject karna (Ye sab DB se aayega)
    template_vars = {
        "financial_year": "2025-26",
        "assessment_year": "2026-2027",
        "employee": {
            "name": user_data.name,
            "designation": user_data.designation,
            "school": user_data.office_school_name,
            "pan": user_data.pan
        },
        "employer": {
            "name": employer_data.employer_name,
            "tan": employer_data.tan,
            "pan": employer_data.pan,
            "address": employer_data.employer_address
        },
        "tax_data": tax_data, # Tax calculations (gross, deductions, 87A rebate, etc.)
        "ledger": ledger_data # Array of 12 months data + arrears
    }

    # HTML render karna
    rendered_html = template.render(template_vars)

    # WeasyPrint se PDF create karna
    # CSS margin: 0.5in (Narrow margin)
    HTML(string=rendered_html).write_pdf(
        output_filename,
        stylesheets=[CSS(string='''
            @page {
                size: A4 portrait;
                margin: 0.5in; /* Narrow Margin */
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
