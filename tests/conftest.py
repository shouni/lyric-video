import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Config.from_env() requires SESSION_SECRET, so a dummy value for tests is set by default.
os.environ.setdefault("SESSION_SECRET", "test-session-secret")
# SERVER_ROLE must be set explicitly. Tests that do not care about the role load it as both, the same as
# local development.
os.environ.setdefault("SERVER_ROLE", "both")
# A role that includes the worker face requires the allowlist (fail-closed).
os.environ.setdefault("ALLOWED_TASK_SERVICE_ACCOUNTS", "tasks@test-project.iam.gserviceaccount.com")
