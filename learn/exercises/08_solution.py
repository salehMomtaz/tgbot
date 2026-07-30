"""Lesson 08 — solution

Run: python3 08_solution.py
"""

def format_progress(current: int, total: int) -> str:
    if total <= 0:
        return "[] 0%"
    pct = min(current / total, 1.0)
    bar_len = int(pct * 20)
    bar = "█" * bar_len + "░" * (20 - bar_len)
    return f"[{bar}] {int(pct * 100)}%"

print(format_progress(45, 100))    # [██████████░░░░░░░░░░] 45%
print(format_progress(90, 100))    # [██████████████████░░] 90%
print(format_progress(100, 100))   # [████████████████████] 100%
print(format_progress(0, 100))     # [░░░░░░░░░░░░░░░░░░░░] 0%
print(format_progress(50, 0))      # [] 0%  (total guard)
