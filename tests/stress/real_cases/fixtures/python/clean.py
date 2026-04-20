"""Clean module: all imports used, no secrets, no dead code."""
import os
import sys


def get_cwd() -> str:
    return os.getcwd()


def print_args() -> None:
    for arg in sys.argv[1:]:
        print(arg)
