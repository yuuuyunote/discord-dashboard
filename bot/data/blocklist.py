"""
bot/data/blocklist.py
xgomi-discordリポジトリ（GitHub側データリポジトリ）の dist/users.json を
取得・キャッシュする。/check はこのファイルだけを見る。

報告メタデータ（誰がいつ何を通報したか）はPostgres側の話で、ここでは扱わない
— そちらは/report実装時に別途。

キャッシュを挟むのは、/checkが連打されてもGitHubへ毎回リクエストを飛ばさない
ようにするため（Neonのコンピュート時間とは無関係——ここはPostgresを一切
使わない）。
"""

import os
import time
from typing import Optional

import aiohttp

DATA_REPO = os.getenv("BLOCKLIST_DATA_REPO", "")  # 例: "yourname/xgomi-discord"
DATA_BRANCH = os.getenv("BLOCKLIST_DATA_BRANCH", "main")
CACHE_TTL_SECONDS = 60


class BlocklistFetchError(Exception):
    pass


class BlocklistCache:
    """テストで差し替えやすいよう、キャッシュ状態をモジュール変数ではなくインスタンスに持たせる。"""

    def __init__(self) -> None:
        self._data: Optional[list] = None
        self._fetched_at: float = 0.0

    async def get(self, force_refresh: bool = False) -> list:
        if not DATA_REPO:
            raise BlocklistFetchError("BLOCKLIST_DATA_REPO が設定されていません。")

        now = time.monotonic()
        if not force_refresh and self._data is not None and (now - self._fetched_at) < CACHE_TTL_SECONDS:
            return self._data

        url = f"https://raw.githubusercontent.com/{DATA_REPO}/{DATA_BRANCH}/dist/users.json"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as res:
                if res.status != 200:
                    raise BlocklistFetchError(f"dist/users.json の取得に失敗しました（status: {res.status}）")
                data = await res.json(content_type=None)

        self._data = data
        self._fetched_at = now
        return data

    async def find(self, target_id: str, force_refresh: bool = False) -> Optional[dict]:
        data = await self.get(force_refresh=force_refresh)
        for entry in data:
            if entry.get("id") == target_id:
                return entry
        return None


# アプリ全体で1つのキャッシュを共有する（/checkの呼び出しごとに作り直さない）
blocklist_cache = BlocklistCache()
