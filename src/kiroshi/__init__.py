"""Kiroshi — a zero-broker, work-stealing mesh runner.

A **Fixer** (coordinator) hands **Gigs** (jobs) to **Runners** (worker nodes) that
pull work over HTTP and execute it on a local process pool. **Kiroshi** optics
(the dashboard) let you watch the whole fleet live.

See PLAN.md for the architecture.
"""

__version__ = "0.0.1"

__all__ = ["__version__"]
