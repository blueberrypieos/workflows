# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
import importlib
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"

sys.path.insert(0, str(TOOLS))

@pytest.fixture
def tool():
    return lambda name: vars(importlib.reload(importlib.import_module("pielib.commands." + name)))


def pytest_addoption(parser):
    parser.addoption('--pietoolchain', help='Path to a pietoolchain checkout for copyright merge parity')
    parser.addoption('--clean-matrix-dsc', help='Source package for existing matrix cases on a host with the SDK')
