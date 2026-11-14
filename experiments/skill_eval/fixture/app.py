import sqlite3


def find_user(database_path: str, email: str):
    connection = sqlite3.connect(database_path)
    query = f"SELECT id, email FROM users WHERE email = '{email}'"
    return connection.execute(query).fetchone()


def update_role(database_path: str, user_id: str, role: str):
    connection = sqlite3.connect(database_path)
    connection.execute(
        f"UPDATE users SET role = '{role}' WHERE id = '{user_id}'"
    )
    connection.commit()
