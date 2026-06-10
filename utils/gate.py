import json
import os
from config import SYSTEM_CREATOR_ID, DB_FILE

def load_authorized_users():
    if not os.path.exists(DB_FILE):
        with open(DB_FILE, 'w') as f:
            json.dump([], f)
        return []
    try:
        with open(DB_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return []

def save_authorized_users(users_list):
    with open(DB_FILE, 'w') as f:
        json.dump(users_list, f)

def is_authorized(user_id: int) -> bool:
    if user_id == SYSTEM_CREATOR_ID:
        return True
    authorized_users = load_authorized_users()
    return user_id in authorized_users

def add_user(user_id: int) -> bool:
    users = load_authorized_users()
    if user_id not in users:
        users.append(user_id)
        save_authorized_users(users)
        return True
    return False

def remove_user(user_id: int) -> bool:
    users = load_authorized_users()
    if user_id in users:
        users.remove(user_id)
        save_authorized_users(users)
        return True
    return False
