"""Generates a bcrypt hash for DASHBOARD_PASSWORD_HASH.

Usage: docker compose run --rm dashboard python hash_password.py 'your-password'
"""
import sys

import bcrypt

if len(sys.argv) != 2:
    raise SystemExit("Usage: python hash_password.py 'your-password'")

print(bcrypt.hashpw(sys.argv[1].encode("utf-8"), bcrypt.gensalt()).decode("utf-8"))
