# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
import json
import os
import sys

PRIVATE_TMPFS = {'/tmp': '1777', '/var/tmp': '1777', '/run': '755'}


def require_private_tmpfs(traces):
    for trace in traces:
        try:
            with open(os.path.join(trace, 'meta.json')) as stream:
                metadata = json.load(stream)
        except (OSError, ValueError):
            metadata = None
        if not isinstance(metadata, dict) or metadata.get('private_tmpfs') != PRIVATE_TMPFS:
            sys.exit('pie-prove: %s does not record private mounts on /tmp, /var/tmp and /run; '
                     'rebuild it without PIE_PROVE_CHECK_ONLY or PIE_PROVE_RESUME' % trace)
