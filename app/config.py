from __future__ import annotations

import os
from dataclasses import dataclass, field

# SERVER_ROLE が取りうる値。空文字は「web と worker の両方を提供する」を意味し、
# ローカル開発と、分離前の構成での互換動作に使う。
SERVER_ROLE_BOTH = ""
SERVER_ROLE_WEB = "web"
SERVER_ROLE_WORKER = "worker"
_SERVER_ROLES = (SERVER_ROLE_BOTH, SERVER_ROLE_WEB, SERVER_ROLE_WORKER)


@dataclass
class Config:
    service_url: str
    # server_role はこのプロセスが担う役割。空なら両方を提供する。
    server_role: str
    port: str
    project_id: str
    location_id: str
    queue_id: str
    worker_url: str
    task_audience_url: str
    service_account_email: str
    gcs_bucket: str
    gcs_output_prefix: str
    slack_webhook_url: str
    whisper_model: str
    # video合成モードのWhisperモデル。曲全体を1本で強制アライメントするため、
    # Cloud Runのメモリと処理時間に収まる小さめのモデルを既定にしている。
    video_whisper_model: str
    # video合成モード(source_type=video)でap-mv/ap-musicの成果物を参照するバケット名
    ap_mv_bucket: str
    ap_music_bucket: str
    # OAuth2
    google_client_id: str
    google_client_secret: str
    session_secret: str
    # YouTube Data API
    youtube_refresh_token: str
    allowed_emails: list[str] = field(default_factory=list)
    allowed_domains: list[str] = field(default_factory=list)
    # M2M (サーバー間通信) 用OIDC許可リスト
    allowed_m2m_service_accounts: list[str] = field(default_factory=list)

    def serves_web(self) -> bool:
        """この役割がWeb面(/, /jobs, OAuth)を提供するかを返す。"""
        return self.server_role in (SERVER_ROLE_BOTH, SERVER_ROLE_WEB)

    def serves_worker(self) -> bool:
        """この役割がWorker面(/tasks/*)を提供するかを返す。"""
        return self.server_role in (SERVER_ROLE_BOTH, SERVER_ROLE_WORKER)

    def is_secure_url(self) -> bool:
        """サービスURLがHTTPSかどうかを返す。"""
        return self.service_url.startswith("https://")

    def default_output_prefix(self) -> str:
        """設定済みのGCSバケットと出力接頭辞からデフォルト出力先を作る。"""
        return f"gs://{self.gcs_bucket}/{self.gcs_output_prefix}" if self.gcs_bucket else ""

    @classmethod
    def from_env(cls) -> Config:
        """環境変数からアプリケーション設定を読み込む。"""
        server_role = os.getenv("SERVER_ROLE", "").strip().lower()
        if server_role not in _SERVER_ROLES:
            raise RuntimeError(f"SERVER_ROLE ({server_role!r}) は 'web', 'worker', または未設定である必要があります")
        service_url = os.getenv("SERVICE_URL", "http://localhost:8080").rstrip("/")
        worker_url = os.getenv("WORKER_URL", "").strip()
        if not worker_url:
            worker_url = f"{service_url}/tasks/generate"
        task_audience_url = os.getenv("TASK_AUDIENCE_URL", "").strip() or service_url
        return cls(
            service_url=service_url,
            server_role=server_role,
            port=os.getenv("PORT", "8080"),
            project_id=os.getenv("GCP_PROJECT_ID", ""),
            location_id=os.getenv("GCP_LOCATION_ID", "asia-northeast1"),
            queue_id=os.getenv("CLOUD_TASKS_QUEUE_ID", ""),
            worker_url=worker_url,
            task_audience_url=task_audience_url,
            service_account_email=os.getenv("SERVICE_ACCOUNT_EMAIL", ""),
            gcs_bucket=os.getenv("GCS_BUCKET", ""),
            gcs_output_prefix=os.getenv("GCS_OUTPUT_PREFIX", "lyric-video/output"),
            slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL", ""),
            whisper_model=os.getenv("WHISPER_MODEL", "large-v3"),
            video_whisper_model=os.getenv("VIDEO_WHISPER_MODEL", "small"),
            ap_mv_bucket=os.getenv("AP_MV_BUCKET", "ap-mv"),
            ap_music_bucket=os.getenv("AP_MUSIC_BUCKET", "ap-music"),
            google_client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
            google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
            # Worker面だけを提供するプロセスはセッションもOAuthも使わないため、
            # 使いもしない認証情報へのアクセス権を配らずに済むよう必須にしない。
            session_secret=(
                _require_env("SESSION_SECRET")
                if server_role in (SERVER_ROLE_BOTH, SERVER_ROLE_WEB)
                else os.getenv("SESSION_SECRET", "")
            ),
            youtube_refresh_token=os.getenv("YOUTUBE_REFRESH_TOKEN", ""),
            allowed_emails=_split_env("ALLOWED_EMAILS"),
            allowed_domains=_split_env("ALLOWED_DOMAINS"),
            allowed_m2m_service_accounts=_split_env("ALLOWED_M2M_SERVICE_ACCOUNTS"),
        )


def _require_env(key: str) -> str:
    """必須環境変数を取得し、未設定なら例外を送出する。"""
    value = os.getenv(key, "").strip()
    if not value:
        raise ValueError(f"{key} environment variable is required")
    return value


def _split_env(key: str) -> list[str]:
    """カンマ区切りの環境変数を小文字のリストへ変換する。"""
    return [v.strip().lower() for v in os.getenv(key, "").split(",") if v.strip()]
