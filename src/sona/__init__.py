"""Sona desktop application."""


def main() -> None:
    import multiprocessing

    multiprocessing.freeze_support()
    import sys

    # The trusted updater copy must never create a GUI or start background jobs.
    if len(sys.argv) == 3 and sys.argv[1] == "--sona-update-helper":
        from .updates.backend import apply_update

        apply_update(sys.argv[2])
        return
    # Codex and other MCP clients start this stdio bridge; it never opens a window.
    if len(sys.argv) == 2 and sys.argv[1] == "mcp":
        from .mcp_bridge import main as run_bridge

        sys.exit(run_bridge())
    # Release builds verify that the bundled MCP SDK and server actually work.
    if len(sys.argv) == 2 and sys.argv[1] == "--sona-self-test":
        from .self_test import main as run_self_test

        sys.exit(run_self_test())
    import logging

    from .logging_config import configure_logging

    configure_logging()
    # Importing storage and domain modules must not initialize the GUI toolkit.
    try:
        from .localization import configure_native_language

        configure_native_language()
        from .app import main as run_app

        run_app()
    except Exception:
        logging.getLogger(__name__).exception('应用启动或运行失败')
        raise
