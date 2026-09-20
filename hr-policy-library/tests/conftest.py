import os
import sys
import tempfile
from pathlib import Path

# Point the audit log at a temp file before the server module loads.
os.environ["HR_AUDIT_LOG"] = str(Path(tempfile.mkdtemp()) / "audit.jsonl")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
