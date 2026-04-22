"""Fixture module with deliberate bugs exercised by the Watchdog E2E test.

Kept as valid Python so `ast.parse()` can parse both original and patched
versions — the intentional bugs are runtime, not syntactic. Each function
is small enough that the Watchdog's 50%-reduction guard is meaningful.
"""
from typing import Optional


class User:
    def __init__(self, email: Optional[str] = None):
        self.email = email


def get_user_email(user: Optional[User]) -> str:
    """BUG 1 (AttributeError) : si `user` est None, l'accès à .email casse."""
    return user.email


def format_user_lines(users: list[User]) -> list[str]:
    """BUG 2 (formatting) : formatte mal quand email est None. Fuzzy match
    target — le Watchdog doit retrouver ce bloc même si l'indentation a
    dérivé depuis le build du ContextPack."""
    result = []
    for u in users:
        line = "<" + u.email + ">"
        result.append(line)
    return result
