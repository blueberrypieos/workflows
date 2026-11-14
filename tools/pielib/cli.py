# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
"""usage: pie COMMAND [ARGS]

Run pie --help to list commands. Run pie COMMAND with its usual arguments.
"""
import importlib
import pkgutil
import sys

from . import commands


def main():
    names = sorted(module.name.replace("_", "-") for module in pkgutil.iter_modules(commands.__path__)
                   if not module.name.startswith("_"))
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.rstrip() + "\n\nCommands:\n  " + "\n  ".join(names))
        return
    if args[0] not in names:
        sys.exit("pie: unknown command: " + args[0])
    name = args[0]
    sys.argv = ["pie-" + name] + args[1:]
    importlib.import_module(".commands." + name.replace("-", "_"), __package__).main()
