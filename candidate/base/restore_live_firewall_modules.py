#!/usr/bin/env python3
"""Retired beta2 recovery entry; its original source is not part of this release."""
import sys
if __name__ == '__main__':
    print('Stopped: legacy recovery is retired. Use the profile-bound offline image factory.', file=sys.stderr)
    raise SystemExit(2)
