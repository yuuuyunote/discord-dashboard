"""
discord_rest.py
Discord REST APIをBotトークンで直接叩くための薄いラッパー。

Render側のダッシュボードはGateway接続（discord.Client / bot.start()）を持たない
設計にしたため、これまでdiscord.pyのメンバーキャッシュやクライアント経由で
やっていたこと（管理者ロール判定・DM送信）をhttpxでの直接REST呼び出しに
置き換える。auth.py が既にOAuthトークンで同じ流儀のREST呼び出しをしている。
"""

import os

import httpx

DISCORD_API = "https://discord.com/api/v10"
BOT_TOKEN = os.getenv("DISCORD_TOKEN", "")


class DiscordRestError(Exception):
    """2xx以外のレスポンス（404/400を除く。それらはNoneや個別ハンドリングで表現する）"""

    def __init__(self, status_code: int, body: str = ""):
        super().__init__(f"Discord API error {status_code}: {body}")
        self.status_code = status_code
        self.body = body


def _headers() -> dict:
    return {"Authorization": f"Bot {BOT_TOKEN}"}


async def get_guild_member(guild_id: str, user_id: str) -> dict | None:
    """ギルドメンバー情報（rolesを含む）を取得する。メンバーでなければNone。"""
    async with httpx.AsyncClient() as client:
        res = await client.get(
            f"{DISCORD_API}/guilds/{guild_id}/members/{user_id}",
            headers=_headers(),
        )
    if res.status_code == 404:
        return None
    if res.status_code != 200:
        raise DiscordRestError(res.status_code, res.text)
    return res.json()


async def get_user(user_id: str) -> dict | None:
    """ユーザーの公開情報（username等）を取得する。存在しなければNone。"""
    async with httpx.AsyncClient() as client:
        res = await client.get(f"{DISCORD_API}/users/{user_id}", headers=_headers())
    if res.status_code == 404:
        return None
    if res.status_code != 200:
        raise DiscordRestError(res.status_code, res.text)
    return res.json()


async def send_dm(user_id: str, content: str) -> dict:
    """
    指定ユーザーにDMを送信し、送信されたメッセージのJSONを返す。
    DMチャンネルを開く→送信の2ステップ。相手がDMを拒否している場合は
    メッセージ送信の方が403で返ってくることが多い。
    """
    async with httpx.AsyncClient() as client:
        dm_res = await client.post(
            f"{DISCORD_API}/users/@me/channels",
            headers=_headers(),
            json={"recipient_id": user_id},
        )
        if dm_res.status_code != 200:
            raise DiscordRestError(dm_res.status_code, dm_res.text)
        channel_id = dm_res.json()["id"]

        msg_res = await client.post(
            f"{DISCORD_API}/channels/{channel_id}/messages",
            headers=_headers(),
            json={"content": content},
        )
        if msg_res.status_code != 200:
            raise DiscordRestError(msg_res.status_code, msg_res.text)
        return msg_res.json()
