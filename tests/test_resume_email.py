from __future__ import annotations

from jobforge.email.imap import classify, extract_code
from jobforge.matching.resume import merge_profiles, parse_resume
from jobforge.models import Profile


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
    assert p.education[0]["courses"] == ["Data Structures", "Operating Systems"]
    assert p.facts


def test_education_course_list_survives_line_wrap():
    p = parse_resume(
        """Jane Doe
jane@example.com | +1 (555) 123-4567
Education
Example University
August 2022 - June 2026
Bachelor of Science in Computer Science
New York, NY
• Relevant Courses: Data Structures, Operating
  Systems, Database Management
Experience
Acme Corp
June 2024 - August 2024
Engineer
• Built things
""",
        fallback_name="jane-doe",
    )
    assert p.education[0]["courses"] == [
        "Data Structures",
        "Operating Systems",
        "Database Management",
    ]


def test_reimport_preserves_curated_courses_when_parser_misses_them():
    base = Profile(
        education=[
            {
                "institution": "Example University",
                "degree": "Bachelor of Science",
                "major": "Computer Science",
                "courses": ["Databases", "Compilers"],
            }
        ]
    )
    parsed = Profile(
        education=[
            {
                "institution": "Example University",
                "degree": "Bachelor of Science",
                "major": "Computer Science",
                "start": "August 2022",
                "end": "June 2026",
            }
        ]
    )

    merged = merge_profiles(base, parsed)

    assert merged.education[0]["courses"] == ["Databases", "Compilers"]
    assert merged.education[0]["start"] == "August 2022"


def test_reimport_updates_courses_when_the_resume_has_them():
    base = Profile(
        education=[
            {
                "institution": "Example University",
                "degree": "Bachelor of Science",
                "major": "Computer Science",
                "courses": ["Old Course"],
            }
        ]
    )
    parsed = parse_resume(RESUME, fallback_name="jane-doe")

    merged = merge_profiles(base, parsed)

    assert merged.education[0]["courses"] == ["Data Structures", "Operating Systems"]


def test_email_code_and_classification():
    assert extract_code("Your verification code", "Use 482913 to continue") == "482913"
    assert classify("Interview invitation", "Let's schedule a call") == "interview"
    assert classify("Your application", "Unfortunately we are not moving forward") == "rejection"
    assert classify("Welcome", "Your job offer letter is attached") == "offer"
    assert classify("Verify your email", "Your security code is 111222") == "verification"


def test_education_courses_fall_back_to_the_source_resume(tmp_path):
    from jobforge.matching.resume import education_with_resume_courses
    from jobforge.models import Profile

    resume = tmp_path / "base.md"
    resume.write_text(RESUME, encoding="utf-8")
    profile = Profile(
        resume_path=str(resume),
        education=[
            {
                "institution": "Example University",
                "degree": "Bachelor of Science",
                "major": "Computer Science",
            }
        ],
    )

    rows = education_with_resume_courses(profile)

    assert rows[0]["courses"] == ["Data Structures", "Operating Systems"]
    # The fallback is read-only; the stored profile is unchanged.
    assert not profile.education[0].get("courses")
