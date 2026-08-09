"""ジョブ全体の実行時間に上限を設けるための締切。

なぜ要るか — タイムアウトの三段
--------------------------------

フリート共通の約束として、タイムアウトは 3 つあり、この大小関係を守る。

    PIPELINE_TIMEOUT  <  dispatch_deadline  <=  Cloud Run の timeout
       (アプリ)             (タスク)                 (サービス)

実効上限を決めるのは**いちばん小さい値**で、他は飾りになる。ここで
PIPELINE_TIMEOUT を**いちばん短く**取るのが要点で、**アプリが自分で先に諦める**ことで
meta.json に failed を書き、Slack へ通知してから終われる。

逆順にすると、先に Cloud Tasks がリクエストを打ち切る。打ち切りはプロセスごと
SIGTERM なので**失敗ハンドラも Slack 通知も走らない**。lyric-video-queue は
max_attempts = 1 なので再試行も来ず、meta.json は status: running のまま取り残される。
「ログに Task failed が出ないまま running で止まっているジョブ」はこれが原因。

この経路は実際に踏んでいる。source_type=images（キーフレームからスライドショーを
作る経路）の検証で 1 本打ち切られた。内訳は Whisper large-v3 のアライメントが
約 8 分半、残り 20 分以上がカラオケ字幕の PNG 描画。

Python 版には長らくこの最上段が無かった。ffmpeg の各ステップには個別のタイムアウトが
あるが（video_merge._SUBPROCESS_TIMEOUT_SEC）、**合計が上限に収まる保証は無い**。
個別の上限をいくら足しても「ジョブ全体の上限」にはならない、というのがこのモジュールの
存在理由。

なぜ signal.alarm ではないか
----------------------------

signal.alarm はメインスレッドでしか発火しない。Flask のリクエストハンドラは
gunicorn のワーカースレッド上で動くため、そこで仕掛けても効かない。
代わりに「締切を持ち回り、各サブプロセスの待ち時間を残り時間で頭打ちにする」方式にした。
どのスレッドから呼んでも同じように効き、打ち切りが例外として上がるので
worker.py の既存の except がそのまま failed の記録と Slack 通知を担当できる。
"""

from __future__ import annotations

import time


class PipelineTimeout(RuntimeError):
    """ジョブ全体の制限時間を使い切ったことを表す。

    RuntimeError を継承しているのは、worker.py の except Exception が拾って
    meta.json へ failed を書き Slack に通知する既存の経路にそのまま乗せるため。
    """


class Deadline:
    """生成時点から timeout_sec 後を締切とし、残り時間を配る。

    time.monotonic を使う。system clock だと NTP の補正で締切が前後しうる。
    """

    def __init__(self, timeout_sec: float) -> None:
        self._timeout_sec = timeout_sec
        # 0 以下は「上限なし」。ローカルでの長時間デバッグ用の逃げ道で、
        # 本番では config が既定値を入れるため通らない。
        self._expires_at = time.monotonic() + timeout_sec if timeout_sec > 0 else None

    @property
    def unlimited(self) -> bool:
        return self._expires_at is None

    def remaining(self) -> float | None:
        """残り秒数。上限なしなら None。使い切っていれば 0 以下の値を返す。"""
        if self._expires_at is None:
            return None
        return self._expires_at - time.monotonic()

    def check(self, step: str) -> None:
        """残り時間が尽きていれば PipelineTimeout を送出する。

        工程と工程の境目で呼ぶ。サブプロセスを起動する直前に残りが 0 なら、
        起動せずここで落ちるほうが速いし、メッセージも分かりやすい。
        """
        remaining = self.remaining()
        if remaining is not None and remaining <= 0:
            raise PipelineTimeout(f"pipeline timed out after {self._timeout_sec:.0f}s (before {step})")

    def clamp(self, preferred_sec: float, step: str) -> float:
        """個別ステップの上限を、ジョブ全体の残り時間で頭打ちにする。

        ステップ側の上限（例: Whisper の 900s）をそのまま使うと、合計がジョブ全体の
        上限を超えうる。短いほうを採る。
        """
        self.check(step)
        remaining = self.remaining()
        if remaining is None:
            return preferred_sec
        return min(preferred_sec, remaining)
