"""并行 eval runner 单测：--jobs N 参数解析。"""

import argparse

import pytest


class TestJobsArgument:
    def test_jobs_defaults_to_one(self):
        from src.cli import _add_eval_run_args

        parser = argparse.ArgumentParser()
        _add_eval_run_args(parser)
        args = parser.parse_args(["--all"])
        assert args.jobs == 1

    def test_jobs_can_be_set(self):
        from src.cli import _add_eval_run_args

        parser = argparse.ArgumentParser()
        _add_eval_run_args(parser)
        args = parser.parse_args(["--all", "--jobs", "4"])
        assert args.jobs == 4

    def test_jobs_short_flag(self):
        from src.cli import _add_eval_run_args

        parser = argparse.ArgumentParser()
        _add_eval_run_args(parser)
        args = parser.parse_args(["--all", "-j", "2"])
        assert args.jobs == 2

    def test_jobs_invalid_value_rejected(self):
        from src.cli import _add_eval_run_args

        parser = argparse.ArgumentParser()
        _add_eval_run_args(parser)
        with pytest.raises(SystemExit):
            parser.parse_args(["--all", "--jobs", "abc"])
