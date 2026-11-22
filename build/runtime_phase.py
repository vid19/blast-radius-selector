"""pytest plugin for the build: while test_execute_tpcds runs, coverage records lines under context
"runtime" whenever the Python executor is executing a plan, and "prep" otherwise. Process pools run
inline so everything is in one process."""
import coverage
import pytest
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from probe_runner_lib import patch_module  # noqa: E402


def pytest_configure(config):
    from sqlglot.executor import python as px
    cov = coverage.Coverage.current()
    orig = px.PythonExecutor.execute

    def execute(self, plan):
        cov.switch_context("runtime")
        try:
            return orig(self, plan)
        finally:
            cov.switch_context("prep")

    px.PythonExecutor.execute = execute
    cov.switch_context("prep")


def pytest_collection_modifyitems(session, config, items):
    for it in items:
        mod = getattr(it, "module", None)
        if mod is not None:
            patch_module(mod)
