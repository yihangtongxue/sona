"""Select only the current platform's application replacement implementation."""

import sys

if sys.platform == "win32":
    from .windows import apply_update, extract_app, installed_bundle, prepare_runner, signing_requirement, validate_app
    APP_DIRECTORY = "Sona"
else:
    from .macos import apply_update, extract_app, installed_bundle, prepare_runner, signing_requirement, validate_app
    APP_DIRECTORY = "Sona.app"
