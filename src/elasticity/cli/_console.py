"""Shared Rich Console singletons for the Elasticity CLI.

Kept in a separate module so that both display.py and split_display.py
can import the same Console instances without circular imports.
"""

from rich.console import Console

console = Console()
error_console = Console(stderr=True)
