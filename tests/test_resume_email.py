from __future__ import annotations

from jobforge.email.imap import classify, extract_code
from jobforge.matching.resume import parse_resume


RESUME = """
Jane Doe
jane@example.com | +1 (555) 123-4567 | linkedin.com/in/janedoe | github.com/janedoe

Education
Example University
August 2022 - June 2026
Bachelor of Science in Computer Science
New York, NY
• Relevant Courses: Data Structures, Operating Systems

Experience
Acme Corp
June 2024 - August 2024
Software Engineer Intern
New York, NY
• Built a Python service that reduced latency by 40%
• Automated a reporting pipeline with SQL and Airflow

Projects
Widget | Python, FastAPI, PostgreSQL
• Built a REST API with FastAPI and PostgreSQL
• Added tests with pytest

Technical Skills
Languages: Python, TypeScript, SQL
Technologies: Docker, AWS, FastAPI
"""


def test_resume_parsing():
    p = parse_resume(RESUME, fallback_name="jane-doe")
    assert p.full_name == "Jane Doe"
    assert p.email == "jane@example.com"
    assert p.location == "New York, NY"
    assert p.experience[0]["title"] == "Software Engineer Intern"
    assert p.experience[0]["company"] == "Acme Corp"
    assert p.experience[0]["end"] == "August 2024"
    assert len(p.experience[0]["bullets"]) == 2
    assert "Python" in p.skills
    assert "Docker" in p.skills
    assert "Languages" not in p.skills
    assert p.education[0]["degree"] == "Bachelor of Science"
    assert p.education[0]["major"] == "Computer Science"
    assert p.facts


def test_email_code_and_classification():
    assert extract_code("Your verification code", "Use 482913 to continue") == "482913"
    assert classify("Interview invitation", "Let's schedule a call") == "interview"
    assert classify("Your application", "Unfortunately we are not moving forward") == "rejection"
    assert classify("Welcome", "Your job offer letter is attached") == "offer"
    assert classify("Verify your email", "Your security code is 111222") == "verification"
