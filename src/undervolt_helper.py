#!/usr/bin/python3 -Es
# SPDX-License-Identifier: MIT
"""Installed root-owned entry point; keep logic root-owned, not in the user's GUI."""
from undervolt_control import main
if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        import sys
        print(str(exc), file=sys.stderr)
        sys.exit(1)
