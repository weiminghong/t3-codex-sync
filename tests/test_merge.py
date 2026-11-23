"""Synthetic merge regression checks; never read personal sessions or databases."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from t3_codex_sync import ParsedMessage, compute_incremental_messages


def message(role, text, second):
    return ParsedMessage(role, text, f"2026-01-01T00:00:{second:02d}.000Z")


def rows(messages):
    return [(m.role, m.text, m.created_at) for m in messages]


class MergeTests(unittest.TestCase):
    def test_truncated_history_is_not_reimported(self):
        history = [message("user", "old omitted request", 1),
                   message("assistant", "old omitted response", 2),
                   message("user", "imported request", 3),
                   message("assistant", "imported response", 4)]
        new = message("user", "new request", 5)
        self.assertEqual(compute_incremental_messages(history + [new], rows(history[2:]), []), [new])

    def test_t3_branch_is_preserved_and_not_duplicated(self):
        imported = [message("user", "initial request", 1), message("assistant", "initial response", 2)]
        shared_run = [message("user", "T3 request", 3), message("assistant", "T3 response", 4)]
        t3_only = message("assistant", "response only present on T3 branch", 5)
        new = message("user", "new Codex request", 6)
        self.assertEqual(compute_incremental_messages(imported + shared_run + [new], rows(imported), rows(shared_run + [t3_only])), [new])

    def test_repeating_a_successful_merge_adds_nothing(self):
        imported = [message("user", "request", 1), message("assistant", "response", 2)]
        new = message("user", "follow up", 3)
        source = imported + [new]
        added = compute_incremental_messages(source, rows(imported), [])
        self.assertEqual(added, [new])
        self.assertEqual(compute_incremental_messages(source, rows(imported + added), []), [])

    def test_native_t3_run_can_anchor_codex_continuation(self):
        run = [message("user", "native request", 1), message("assistant", "native response", 2)]
        new = message("user", "continued in CLI", 3)
        self.assertEqual(compute_incremental_messages(run + [new], [], rows(run)), [new])


if __name__ == "__main__":
    unittest.main()
