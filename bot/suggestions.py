"""
bot/suggestions.py

note記事案・匿名サジェスチョンボード
------------------------------------
- 対象チャンネルの一番下に常に「記事案を送る」ボタンを設置する
- ボタンを押すとモーダルが開き、テキストを入力して送信できる
- 入力内容は対象チャンネルに「匿名」で投稿される（送信者情報は一切載せない）
- 同時に、運営専用ログチャンネルへ「送信者がわかる」形で記録する
- チャンネルに新しいメッセージが投稿されるたびに、ボタンメッセージを
  削除して末尾に再送信することで「常に最下部にボタンがある」状態を保つ

導入方法
--------
1. このファイルを bot/suggestions.py として配置する
2. .env に以下を追加する

     SUGGESTION_LOG_CHANNEL_ID=1234567890123456   # 送信者がわかるログを流す運営専用チャンネルのID

3. bot_only.py（または main.py）側で、tree = setup_commands(bot) の後に追記する

     from bot.suggestions import setup_suggestions
     setup_suggestions(bot, tree)

4. Bot起動後、ボードを設置したいチャンネルで
   `/suggestion_board_setup` を実行する（管理者権限が必要）
   → そのチャンネルの最下部にボタンが設置される
   解除したい場合は `/suggestion_board_remove` を実行する

注意点
------
- 匿名運用が目的のため、ログ以外の経路（Botのメッセージ削除権限ログ、
  監査ログ等）から送信者が推測されないよう、運営メンバーの運用にも
  注意してください。
- 設置状態は data/suggestion_board.json に保存される。Wispbyteなど
  一部のホスティングではデプロイ（再ビルド）のたびにファイルシステムが
  初期化される場合があるため、その場合は再デプロイ後にもう一度
  `/suggestion_board_setup` を実行するか、database.py 側の永続DBに
  保存先を差し替えてください（_load_state / _save_state を置き換えるだけで済むようにしてあります）。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import discord
from discord import app_commands

logger = logging.getLogger(__name__)

# ---- 設定 -----------------------------------------------------------------

SUGGESTION_LOG_CHANNEL_ID = int(os.environ.get("SUGGESTION_LOG_CHANNEL_ID", "0"))

BUTTON_CUSTOM_ID = "note_suggestion:open_modal"
MODAL_CUSTOM_ID = "note_suggestion:modal"
INPUT_CUSTOM_ID = "note_suggestion:input"

_STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "suggestion_board.json"


# ---- 状態の保存/読み込み ----------------------------------------------------
# 「どのチャンネルにボードが設置されていて、直近のボタンメッセージIDは何か」を保持する。
# {"<channel_id>": <message_id>, ...}

def _load_state() -> dict:
    if _STATE_PATH.exists():
        try:
            return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("suggestion_board.json の読み込みに失敗しました")
    return {}


def _save_state(state: dict) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---- モーダル(入力フォーム) --------------------------------------------------

class SuggestionModal(discord.ui.Modal, title="noteの記事案を送る"):
    idea = discord.ui.TextInput(
        label="記事案（匿名で投稿されます）",
        style=discord.TextStyle.paragraph,
        placeholder="読みたい記事のテーマ、扱ってほしい内容などを自由にどうぞ",
        max_length=1000,
        custom_id=INPUT_CUSTOM_ID,
    )

    def __init__(self, bot: discord.Client):
        super().__init__(custom_id=MODAL_CUSTOM_ID, timeout=None)
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        content = str(self.idea.value).strip()
        if not content:
            await interaction.response.send_message("空の内容は送信できません。", ephemeral=True)
            return

        board_channel = interaction.channel
        log_channel = None
        if interaction.guild is not None and SUGGESTION_LOG_CHANNEL_ID:
            log_channel = interaction.guild.get_channel(SUGGESTION_LOG_CHANNEL_ID)

        # 匿名投稿（送信者情報は含めない）
        embed = discord.Embed(
            description=content,
            color=discord.Color.blurple(),
        )
        embed.set_author(name="📮 匿名の記事案")
        await board_channel.send(embed=embed)

        # 運営専用ログ（送信者がわかる）
        if log_channel is not None:
            log_embed = discord.Embed(
                description=content,
                color=discord.Color.dark_grey(),
                timestamp=discord.utils.utcnow(),
            )
            log_embed.set_author(
                name=f"{interaction.user} ({interaction.user.id})",
                icon_url=interaction.user.display_avatar.url,
            )
            log_embed.set_footer(text=f"#{getattr(board_channel, 'name', board_channel.id)} への投稿")
            await log_channel.send(embed=log_embed)
        else:
            logger.warning(
                "SUGGESTION_LOG_CHANNEL_ID のチャンネルが取得できません: %s",
                SUGGESTION_LOG_CHANNEL_ID,
            )

        await interaction.response.send_message(
            "記事案を匿名で送信しました。ご協力ありがとうございます！",
            ephemeral=True,
        )

        # 投稿後、ボタンを最下部に貼り直す
        await _refresh_board_button(self.bot, board_channel)


# ---- ボタン(永続View) -------------------------------------------------------

class SuggestionBoardView(discord.ui.View):
    def __init__(self, bot: discord.Client):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="記事案を送る（匿名）",
        style=discord.ButtonStyle.primary,
        emoji="📝",
        custom_id=BUTTON_CUSTOM_ID,
    )
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(SuggestionModal(self.bot))


# ---- ボタンをチャンネル最下部に保つロジック ------------------------------------

async def _refresh_board_button(bot: discord.Client, channel: discord.abc.Messageable) -> None:
    """既存のボタンメッセージを削除し、チャンネル最下部に新しく貼り直す"""
    state = _load_state()
    key = str(channel.id)
    old_id = state.get(key)

    if old_id:
        try:
            old_msg = await channel.fetch_message(old_id)
            await old_msg.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException:
            logger.exception("既存のボタンメッセージ削除に失敗しました")

    new_msg = await channel.send(
        "下のボタンから、note記事の案を匿名で送信できます。",
        view=SuggestionBoardView(bot),
    )
    state[key] = new_msg.id
    _save_state(state)


async def _remove_board_button(channel: discord.abc.Messageable) -> bool:
    """ボードを解除する。設置されていた場合は True を返す"""
    state = _load_state()
    key = str(channel.id)
    old_id = state.pop(key, None)
    _save_state(state)

    if old_id:
        try:
            old_msg = await channel.fetch_message(old_id)
            await old_msg.delete()
        except discord.HTTPException:
            pass
        return True
    return False


# ---- セットアップ関数 --------------------------------------------------------

def setup_suggestions(bot: discord.Client, tree: app_commands.CommandTree) -> None:
    """bot_only.py 等から一度だけ呼び出す。

    - 永続Viewの登録（再起動後もボタンを押せるようにする）
    - チャンネルへの新規投稿を監視し、ボタンを最下部に保つリスナー登録
    - 管理者用のボード設置/解除スラッシュコマンド登録
    """

    bot.loop.create_task(_wait_and_register(bot))

    async def _on_message(message: discord.Message) -> None:
        if message.guild is None:
            return
        if bot.user is not None and message.author.id == bot.user.id:
            # ボタン自身の再設置による無限ループを避ける
            return
        state = _load_state()
        if str(message.channel.id) not in state:
            return
        await _refresh_board_button(bot, message.channel)

    bot.add_listener(_on_message, "on_message")

    @tree.command(name="suggestion_board_setup", description="このチャンネルにnote記事案の匿名投稿ボタンを設置します")
    @app_commands.checks.has_permissions(administrator=True)
    async def suggestion_board_setup(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await _refresh_board_button(bot, interaction.channel)
        await interaction.followup.send("このチャンネルに記事案ボタンを設置しました。", ephemeral=True)

    @tree.command(name="suggestion_board_remove", description="このチャンネルのnote記事案ボタンを解除します")
    @app_commands.checks.has_permissions(administrator=True)
    async def suggestion_board_remove(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        removed = await _remove_board_button(interaction.channel)
        msg = "ボタンを解除しました。" if removed else "このチャンネルにはボタンが設置されていません。"
        await interaction.followup.send(msg, ephemeral=True)


async def _wait_and_register(bot: discord.Client) -> None:
    await bot.wait_until_ready()
    bot.add_view(SuggestionBoardView(bot))
    logger.info("SuggestionBoardView を永続Viewとして登録しました")
