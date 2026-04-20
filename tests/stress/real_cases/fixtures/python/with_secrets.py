"""Module with fake-but-realistic secrets for secret_scan testing."""
import os

AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"
AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"

OPENAI_API_KEY = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghij"

DATABASE_URL = "postgres://admin:SuperSecret123@db.internal:5432/prod"

GITHUB_TOKEN = "ghp_1234567890abcdefghijklmnopqrstuvwxyz00"


def load_config():
    # Intentionally legitimate-looking code that uses env vars.
    return {
        "aws_region": os.environ.get("AWS_REGION", "us-east-1"),
        "aws_key": AWS_ACCESS_KEY_ID,
    }
