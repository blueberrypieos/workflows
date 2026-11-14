# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
import os
import sys


def test_binary_fixed_environment(tool, monkeypatch):
    source = tool('source')
    seen = {}
    monkeypatch.setattr(os, 'environ', dict(os.environ, HOME='/caller', DEB_BUILD_OPTIONS='nocheck',
                                         TAR_OPTIONS='--broken', PIE_QUIET='1'))
    monkeypatch.setattr(sys, 'argv', ['pie-source', 'binary', 'test.dsc'])
    monkeypatch.setitem(source['main'].__globals__, 'build_binary', lambda *args: seen.update(os.environ))
    source['main']()
    assert seen['HOME'] == '/nonexistent'
    assert seen['LC_ALL'] == 'C.UTF-8'
    assert seen['PIE_QUIET'] == '1'
    assert 'DEB_BUILD_OPTIONS' not in seen
    assert 'TAR_OPTIONS' not in seen
