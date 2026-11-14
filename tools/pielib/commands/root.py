# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
"""usage: pie root build|path|hash|copy [--recipe FILE] [--cache DIR] [--tmpdir DIR] [--destination DIR]"""
import argparse
import os
from pathlib import Path
import sys
import subprocess

from .. import root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('build', 'path', 'hash', 'copy'))
    parser.add_argument('--recipe', type=Path, default=os.environ.get('PIE_ROOT_RECIPE', root.DEFAULT_RECIPE))
    parser.add_argument('--cache', type=Path, default=os.environ.get('PIE_ROOT_CACHE'))
    parser.add_argument('--tmpdir', type=Path, default=os.environ.get('PIE_ROOT_TMPDIR', '/var/tmp'))
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args()
    if (args.action == 'copy') != (args.destination is not None):
        parser.error('copy requires --destination; other actions do not accept it')
    try:
        recipe = root.Recipe(args.recipe)
        if args.action == 'hash':
            print(recipe.digest)
        else:
            cache = args.cache or root.default_cache()
            if args.action == 'copy':
                result = root.copy_working(recipe, cache, args.destination)
            else:
                result = root.build(recipe, cache, args.tmpdir) if args.action == 'build' else root.cached(recipe, cache)
            print(result)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as error:
        sys.exit('pie-root: ' + str(error))
