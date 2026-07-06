import os

import psycopg2
from pgvector.psycopg2 import register_vector

DATABASE_URL = os.environ["DATABASE_URL"]


def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    register_vector(conn)
    return conn
