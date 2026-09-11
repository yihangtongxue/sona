"""Sona desktop application."""


def main() -> None:
    import multiprocessing

    multiprocessing.freeze_support()
    import logging

    from .logging_config import configure_logging

    configure_logging()
    # Importing storage and domain modules must not initialize the GUI toolkit.
    try:
        from .app import main as run_app

        run_app()
    except Exception:
        logging.getLogger(__name__).exception('应用启动或运行失败')
        raise
