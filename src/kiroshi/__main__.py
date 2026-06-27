"""Allow ``python -m kiroshi ...`` as an entrypoint.

Windows services launch most reliably via an absolute interpreter path plus
``-m kiroshi`` (no dependency on the ``Scripts`` dir being on the service's
PATH), so the service installer uses this form.
"""
from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
