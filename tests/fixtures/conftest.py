"""Fixture repositories are data, not tests.

Everything under ``tests/fixtures`` is consumed by other tests as input
(copied into a temp root, or fed to an eval runner). Some fixture repos
deliberately ship a ``test_*.py`` with a broken import, so pytest must
never collect this tree.
"""

collect_ignore_glob = ["*"]
