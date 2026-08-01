import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Config.from_env() は SESSION_SECRET を必須にしているため、テスト用のダミーを既定で入れる。
os.environ.setdefault("SESSION_SECRET", "test-session-secret")
