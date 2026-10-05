#!/usr/bin/env python3
"""Launcher so PDF Desk can be started with: python pdfdesk.py [files...]"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pdfdesk.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
