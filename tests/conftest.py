import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Config.from_env() は SESSION_SECRET を必須にしているため、テスト用のダミーを既定で入れる。
os.environ.setdefault("SESSION_SECRET", "test-session-secret")
# SERVER_ROLE は明示が必須。役割に関心のないテストはローカル開発と同じ both で読む。
os.environ.setdefault("SERVER_ROLE", "both")
# worker 面を含む役割では許可リストが必須（fail-closed）。
os.environ.setdefault("ALLOWED_TASK_SERVICE_ACCOUNTS", "tasks@test-project.iam.gserviceaccount.com")
