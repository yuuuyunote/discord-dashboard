"""
bot/data/blocklist.py
xgomi-discordリポジトリ（GitHub側データリポジトリ）の dist/{users,servers,bots}.json を
取得・キャッシュする。/check はこのファイルだけを見る。

報告メタデータ（誰がいつ何を通報したか）はPostgres側の話で、ここでは扱わない。

キャッシュを挟むのは、/checkが連打されてもGitHubへ毎回リクエストを飛ばさない
ようにするため（Neonのコンピュート時間とは無関係——ここはPostgresを一切
使わない）。

user/server/bot の3種は参照する dist ファイルが違うだけなので、
BlocklistCache に dist ファイル名を持たせて3インスタンス用意している。
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

    def __init__(self, dist_filename: str) -> None:
        self._dist_filename = dist_filename
        self._data: Optional[list] = None
        self._fetched_at: float = 0.0

    async def get(self, force_refresh: bool = False) -> list:
        if not DATA_REPO:
            raise BlocklistFetchError("BLOCKLIST_DATA_REPO が設定されていません。")

        now = time.monotonic()
        if not force_refresh and self._data is not None and (now - self._fetched_at) < CACHE_TTL_SECONDS:
            return self._data

        url = f"https://raw.githubusercontent.com/{DATA_REPO}/{DATA_BRANCH}/dist/{self._dist_filename}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as res:
                if res.status != 200:
                    raise BlocklistFetchError(f"dist/{self._dist_filename} の取得に失敗しました（status: {res.status}）")
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


# アプリ全体で1つずつ共有する（/checkの呼び出しごとに作り直さない）
user_blocklist_cache = BlocklistCache("users.json")
server_blocklist_cache = BlocklistCache("servers.json")
bot_blocklist_cache = BlocklistCache("bots.json")

# 既存呼び出し（check.py旧バージョン）との互換のため残すエイリアス
blocklist_cache = user_blocklist_cache

BLOCKLIST_CACHES_BY_TYPE: dict[str, BlocklistCache] = {
    "user": user_blocklist_cache,
    "server": server_blocklist_cache,
    "bot": bot_blocklist_cache,
}


def cache_for(target_type: str) -> BlocklistCache:
    cache = BLOCKLIST_CACHES_BY_TYPE.get(target_type)
    if cache is None:
        raise BlocklistFetchError(f"unknown target_type: {target_type}")
    return cache
