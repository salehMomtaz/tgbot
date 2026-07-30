"""Lesson 05 — solution

Run: python3 05_solution.py
"""

bot_stats = {
    "downloads": 0,
    "errors": 0,
    "users": [],
}

def record_error():
    bot_stats["errors"] += 1

record_error()
record_error()
record_error()
print(bot_stats)  # {'downloads': 0, 'errors': 3, 'users': []}

if "dev" not in bot_stats["users"]:
    bot_stats["users"].append("dev")
print(bot_stats)