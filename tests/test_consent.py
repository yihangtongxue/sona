import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sona.api import AppApi
from sona.consent import AIUsageConsent


class ConsentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.consent = AIUsageConsent(self.root)

    def test_first_use_requires_explicit_confirmation_and_survives_restart(self):
        self.assertTrue(self.consent.required())
        with self.assertRaises(ValueError):
            self.consent.require()
        self.assertFalse(self.consent.path.exists())
        self.consent.require(True)
        restarted = AIUsageConsent(self.root)
        self.assertFalse(restarted.required())
        restarted.require()

    def test_invalid_or_future_acknowledgement_requires_confirmation(self):
        for record in ({"version": 2, "accepted": True}, {"version": True, "accepted": True},
                       {"version": 1, "accepted": 1}, None):
            self.consent.path.write_text(json.dumps(record))
            self.assertTrue(self.consent.required())
        self.consent.path.write_text("invalid")
        self.assertTrue(self.consent.required())
        with self.assertRaises(ValueError):
            self.consent.require("true")

    def test_failed_write_does_not_record_consent(self):
        with patch("sona.consent.os.fsync", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(ValueError, "无法保存"):
                self.consent.require(True)
        self.assertTrue(self.consent.required())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_bridge_blocks_both_create_and_retry_before_acknowledgement(self):
        manuscripts = Mock()
        api = AppApi(Mock(), Mock(), Mock(), manuscripts=manuscripts, consent=self.consent)
        for action in (api.optimize_transcription, api.retry_manuscript):
            with self.assertRaises(ValueError):
                action("audio-id")
        manuscripts.create.assert_not_called()
        manuscripts.retry.assert_not_called()
        api.optimize_transcription("audio-id", True)
        api.retry_manuscript("manuscript-id")
        manuscripts.create.assert_called_once_with("audio-id")
        manuscripts.retry.assert_called_once_with("manuscript-id")
