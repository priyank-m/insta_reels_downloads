import mysql.connector
from mysql.connector import Error

from app.core.config import settings


def get_connection():
    try:
        connection = mysql.connector.connect(
            host=settings.db_host,
            database=settings.db_name,
            user=settings.db_user,
            password=settings.db_password,
        )
        if connection.is_connected():
            return connection
    except Error as exc:
        print("Error while connecting to MySQL", exc)
    return None
