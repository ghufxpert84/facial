"""Generates a bcrypt hash for DASHBOARD_PASSWORD_HASH.

Usage: docker compose run --rm dashboard python hash_password.py 'your-password'
"""
import sys

from passlib.context import CryptContext

if len(sys.argv) != 2:
    raise SystemExit("Usage: python hash_password.py 'your-password'")

pwd_context = CryptContext(schemes=["bcrypt"])
print(pwd_context.hash(sys.argv[1]))
