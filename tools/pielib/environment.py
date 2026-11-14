# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
import os


def fixed_environment(keep=()):
    env = {k: os.environ[k] for k in ("PATH", "TMPDIR") + tuple(keep) if k in os.environ}
    env.update(LC_ALL="C.UTF-8", HOME="/nonexistent")
    os.environ.clear()
    os.environ.update(env)
