#!/usr/bin/env python3
"""
Retired. Use build.py instead.

This script used to package the app with PyInstaller. It is kept as a hard stop
rather than deleted, because it shipped two things it should not have and a CI
job or a shell history entry still pointing at it must fail loudly rather than
quietly produce another leaking build.

What it did wrong:

  --add-data=config   swept the whole config directory into the bundle, which
                      included config/service_account.json -- a live Google
                      Cloud private key -- as a plainly readable file.

  --add-data=src      shipped every one of our .py files verbatim as data. Not
                      compiled, not even bytecode: the source.

The replacement compiles with Nuitka, names each bundled file explicitly, and
refuses to run if a credential is anywhere in the build tree.

    python build.py
"""

import sys

print(__doc__, file=sys.stderr)
sys.exit(1)
