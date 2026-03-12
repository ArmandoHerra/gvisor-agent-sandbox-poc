"""Shared test fixtures for the proxy test suite."""

import sys
from pathlib import Path

# Add project root to sys.path so tests can import proxy modules
sys.path.insert(0, str(Path(__file__).parent.parent))
