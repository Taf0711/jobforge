"""Skill lexicon and aliases used for JD/resume extraction."""

from __future__ import annotations

# Canonical names. Keep this list broad but not silly: every token here can
# become a scored requirement, so ambiguous words (e.g. "Go") are handled with
# word-boundary matching and a context check.
SKILLS: list[str] = [
    # languages
    "Python", "Java", "JavaScript", "TypeScript", "C", "C++", "C#", "Go", "Rust", "Ruby",
    "PHP", "Swift", "Kotlin", "Scala", "R", "MATLAB", "Perl", "Bash", "SQL", "HTML", "CSS",
    # frontend
    "React", "Next.js", "Vue", "Angular", "Svelte", "Redux", "Tailwind", "SASS", "Webpack",
    "Vite", "jQuery", "Accessibility", "Responsive Design",
    # backend / APIs
    "Node.js", "Express", "NestJS", "Django", "Flask", "FastAPI", "Spring", "Rails", "Gin",
    "REST APIs", "GraphQL", "gRPC", "WebSockets", "OpenAPI", "Microservices", "Distributed Systems",
    # data
    "PostgreSQL", "MySQL", "SQLite", "SQL Server", "Oracle", "MongoDB", "DynamoDB", "Redis",
    "Cassandra", "Snowflake", "Databricks", "Spark", "Kafka", "RabbitMQ", "Airflow", "dbt",
    "Pandas", "NumPy", "ETL", "Data Analysis", "Data Engineering",
    # cloud / infra
    "AWS", "GCP", "Azure", "Docker", "Kubernetes", "Terraform", "Ansible", "CloudFormation",
    "Helm", "Linux", "Unix", "Git", "GitHub Actions", "CI/CD", "Jenkins", "Prometheus",
    "Grafana", "Datadog", "OpenTelemetry", "Splunk", "Serverless", "Lambda", "S3", "EC2",
    # ML
    "Machine Learning", "Deep Learning", "Reinforcement Learning", "NLP", "Computer Vision",
    "LLM", "PyTorch", "TensorFlow", "scikit-learn", "Keras", "JAX", "XGBoost", "MLOps",
    # testing / quality
    "pytest", "JUnit", "Jest", "Playwright", "Selenium", "Cypress", "Unit Testing",
    "Integration Testing", "Code Review", "Debugging",
    # product / process
    "Agile", "Scrum", "Jira", "Confluence", "GitHub", "GitLab", "Figma", "Product Management",
    "Technical Writing", "Documentation", "Mentoring", "Cross-functional Collaboration",
    # security / other
    "OAuth", "JWT", "SSO", "Security", "Cryptography", "Networking", "TCP/IP", "HTTP",
    "Operating Systems", "Computer Architecture", "Algorithms", "Data Structures",
]

ALIASES: dict[str, str] = {
    "js": "JavaScript",
    "javascript": "JavaScript",
    "ts": "TypeScript",
    "typescript": "TypeScript",
    "golang": "Go",
    "go lang": "Go",
    "c/c++": "C++",
    "cpp": "C++",
    "c plus plus": "C++",
    "postgres": "PostgreSQL",
    "postgresql": "PostgreSQL",
    "mysql": "MySQL",
    "mongo": "MongoDB",
    "node": "Node.js",
    "nodejs": "Node.js",
    "node.js": "Node.js",
    "next": "Next.js",
    "nextjs": "Next.js",
    "react.js": "React",
    "reactjs": "React",
    "vue.js": "Vue",
    "vuejs": "Vue",
    "k8s": "Kubernetes",
    "kube": "Kubernetes",
    "amazon web services": "AWS",
    "google cloud platform": "GCP",
    "gcp": "GCP",
    "microsoft azure": "Azure",
    "rest": "REST APIs",
    "rest api": "REST APIs",
    "restful": "REST APIs",
    "graphql": "GraphQL",
    "ci/cd": "CI/CD",
    "cicd": "CI/CD",
    "machine learning": "Machine Learning",
    "ml": "Machine Learning",
    "artificial intelligence": "Machine Learning",
    "ai": "Machine Learning",
    "large language model": "LLM",
    "large language models": "LLM",
    "llms": "LLM",
    "natural language processing": "NLP",
    "computer vision": "Computer Vision",
    "sklearn": "scikit-learn",
    "scikit learn": "scikit-learn",
    "pytorch": "PyTorch",
    "tensorflow": "TensorFlow",
    "power bi": "Data Analysis",
    "tableau": "Data Analysis",
    "excel": "Data Analysis",
    "agile": "Agile",
    "scrum": "Scrum",
}

# Words that look like skills but are too generic to score alone.
STOP_SKILLS = {"api", "apis", "data", "software", "engineering", "development", "systems", "code"}
