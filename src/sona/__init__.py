"""Sona desktop application."""


def main() -> None:
    # Importing storage and domain modules must not initialize the GUI toolkit.
    from .app import main as run_app

    run_app()
