"""
bot/suggestions.py

匿名アイデア投稿パネル（ボタン→モーダル→匿名投稿 + 運営ログ + Notion登録）
------------------------------------------------------------------------
- 指定チャンネルの一番下に常に「アイデアを送る」ボタン付きパネル（Embed）を設置する
- ボタンを押すとモーダルが開く
    ・名前      … 自由記述（Notionの「アイデア名」になる）
    ・カテゴリ  … Discord / note / Notion / その他 から選択
    ・詳細      … 任意。長文OK（Notionではページ本文になる）
- 送信内容は次の3か所に送られる
    1. パネルを設置したチャンネル … 匿名で投稿（送信者情報は一切載せない）
    2. 運営専用ログチャンネル（.envで指定）… 送信者がわかる形で記録
    3. Notionデータベース（.envで指定）… 名前・カテゴリ・ステータス(構想中)で登録し、
       詳細があればそのページの本文に書き込む
       ※Notionには送信者情報は登録しない（データベースに該当列が無いため）
- チャンネルに新しいメッセージが投稿されるたびに、パネルを削除して
  末尾に再送信することで「常に最下部にパネルがある」状態を保つ

導入方法
--------
1. このファイルを bot/suggestions.py として配置する（既存ファイルを置き換える）
2. .env に以下を追加する

     SUGGESTION_LOG_CHANNEL_ID=1234567890123456   # 送信者がわかるログを流す運営専用チャンネルのID
     NOTION_API_KEY=secret_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx  # Notion Integration Token
     NOTION_DATABASE_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx     # 「アイデア」データベースのID（URLの32桁部分）
     SUGGESTION_COOLDOWN_SECONDS=60               # 任意。連続送信を防ぐ秒数（省略時60秒、0で無効化）

   Notion側の準備:
     - 「アイデア」データベースの右上「...」→「コネクト」から、作成したIntegrationを接続する
       （これをしないと object_not_found エラーになる）
     - データベースのプロパティ名は次の通りである必要がある（違う場合は下の NOTION_PROP_* を書き換える）
          アイデア名 : タイトル
          カテゴリ   : セレクト（Discord / note / Notion / その他）
          ステータス : ステータス（「構想中」を含む名前の選択肢が1つ以上あること）

3. bot_only.py 側は変更不要
     from bot.suggestions import setup_suggestions
     setup_suggestions(bot, tree)

4. Bot起動後、パネルを設置したいチャンネルで `/suggestion_board_setup` を実行する（管理者のみ）
   解除したい場合は `/suggestion_board_remove`

注意点
------
- Notion登録は「失敗してもDiscord側の投稿は止めない」設計。失敗時はログに warning が出る。
- ステータスの選択肢名（例:「💡 構想中」）は、起動後の初回登録時にNotionから
  データベース定義を取得し、「構想中」を含む選択肢を自動で探して使う。
  取得に失敗した場合は NOTION_STATUS_FALLBACK を使う。
- クールダウンはメモリ上のみで管理しているため、Bot再起動でリセットされる。
- 設置状態は data/suggestion_board.json に保存される。ホスティング側でファイルが
  初期化される環境では、再デプロイ後にもう一度 `/suggestion_board_setup` を実行すること。
- bot_only.py では setup_events / setup_commands の後に setup_suggestions を呼ぶこと（既存の on_ready / on_message に連結するため）。
- モーダル内のセレクトメニューは discord.py 2.6 以降が必要（requirements.txt は 2.6.4 なのでOK）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import discord
import httpx
from discord import app_commands

logger = logging.getLogger(__name__)

# ---- 設定 -----------------------------------------------------------------

SUGGESTION_LOG_CHANNEL_ID = int(os.environ.get("SUGGESTION_LOG_CHANNEL_ID", "0"))

# 同一ユーザーが連続送信できない秒数（荒らし・連投対策）。0にすると無効化される。
COOLDOWN_SECONDS = int(os.environ.get("SUGGESTION_COOLDOWN_SECONDS", "60"))

NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")
NOTION_VERSION = "2022-06-28"
NOTION_PAGES_URL = "https://api.notion.com/v1/pages"
NOTION_DATABASE_URL = "https://api.notion.com/v1/databases/{database_id}"

# Notionデータベース側のプロパティ名（実際の列名に合わせて書き換え可）
NOTION_PROP_TITLE = "アイデア名"      # タイトル
NOTION_PROP_CATEGORY = "カテゴリ"     # セレクト
NOTION_PROP_STATUS = "ステータス"     # ステータス（またはセレクト）

# 登録時に常に設定するステータス。選択肢名に絵文字が付いていても、
# このキーワードを含む選択肢を自動で探す。
NOTION_STATUS_KEYWORD = "構想中"
NOTION_STATUS_FALLBACK = "構想中"     # 自動取得に失敗した場合に使う選択肢名

CATEGORIES = ["Discord", "note", "Notion", "その他"]

PANEL_TITLE = "💡 アイデア募集"
PANEL_DESCRIPTION = "下のボタンから、匿名でアイデアを送信できます。"
PANEL_COLOR = discord.Color.blurple()

BUTTON_LABEL = "アイデアを送る（匿名）"
BUTTON_EMOJI = "📝"

MODAL_TITLE = "アイデアを送る"
NAME_LABEL = "名前"
NAME_PLACEHOLDER = "アイデアの名前（匿名で投稿されます）"
DETAIL_LABEL = "詳細（任意）"
DETAIL_PLACEHOLDER = "アイデアの詳細や背景など（匿名で投稿されます）"
DETAIL_MAX_LENGTH = 1000   # Notionのテキスト上限(2000文字)とDiscord Embed上限に収まる値
CATEGORY_LABEL = "カテゴリ"
CATEGORY_PLACEHOLDER = "カテゴリを選択"

BUTTON_CUSTOM_ID = "note_suggestion:open_modal"
MODAL_CUSTOM_ID = "note_suggestion:modal"
NAME_CUSTOM_ID = "note_suggestion:name"
CATEGORY_CUSTOM_ID = "note_suggestion:category"
DETAIL_CUSTOM_ID = "note_suggestion:detail"

_STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "suggestion_board.json"


# ---- 状態の保存/読み込み ----------------------------------------------------
# {"<channel_id>": <パネルのメッセージID>, ...}

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


# ---- 連投クールダウン --------------------------------------------------------
# メモリ上のみ。{user_id: 最後に送信が成功した time.monotonic() の値}
_last_submission: dict[int, float] = {}


def _remaining_cooldown(user_id: int) -> float:
    """あと何秒待てば送信できるかを返す（0以下なら送信可）"""
    if COOLDOWN_SECONDS <= 0:
        return 0.0
    last = _last_submission.get(user_id)
    if last is None:
        return 0.0
    remaining = COOLDOWN_SECONDS - (time.monotonic() - last)
    return remaining if remaining > 0 else 0.0


def _record_submission(user_id: int) -> None:
    _last_submission[user_id] = time.monotonic()


# ---- Notion登録 -------------------------------------------------------------

def _notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


# (プロパティ型, 選択肢名) をキャッシュ。成功したときだけ保存する。
_status_cache: tuple[str, str] | None = None


async def _resolve_status(client: httpx.AsyncClient) -> tuple[str, str]:
    """ステータス列の (型, 「構想中」を含む選択肢名) を返す。

    Notionのステータス型は登録時に存在しない選択肢名を指定するとエラーになるため、
    データベース定義から実際の選択肢名（絵文字付きの場合もある）を取得する。
    """
    global _status_cache
    if _status_cache is not None:
        return _status_cache

    try:
        resp = await client.get(
            NOTION_DATABASE_URL.format(database_id=NOTION_DATABASE_ID),
            headers=_notion_headers(),
        )
        if resp.status_code >= 400:
            logger.warning("Notionのデータベース定義の取得に失敗 (status=%s): %s", resp.status_code, resp.text[:300])
            return "status", NOTION_STATUS_FALLBACK

        prop = resp.json().get("properties", {}).get(NOTION_PROP_STATUS)
        if not prop:
            logger.warning("Notionに「%s」プロパティが見つかりません", NOTION_PROP_STATUS)
            return "status", NOTION_STATUS_FALLBACK

        prop_type = prop.get("type", "status")
        options = (prop.get(prop_type) or {}).get("options", [])
        for opt in options:
            if NOTION_STATUS_KEYWORD in opt.get("name", ""):
                _status_cache = (prop_type, opt["name"])
                return _status_cache

        logger.warning("ステータスに「%s」を含む選択肢が見つかりません", NOTION_STATUS_KEYWORD)
        return prop_type, NOTION_STATUS_FALLBACK
    except Exception:
        logger.exception("Notionのステータス定義の取得中に例外が発生しました")
        return "status", NOTION_STATUS_FALLBACK


async def _submit_to_notion(*, name: str, category: str, detail: str = "") -> None:
    """Notionデータベースに1レコード登録する。失敗しても例外は外に投げない。"""
    if not NOTION_API_KEY or not NOTION_DATABASE_ID:
        logger.warning("NOTION_API_KEY / NOTION_DATABASE_ID が未設定のため、Notion登録をスキップしました")
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            status_type, status_name = await _resolve_status(client)

            payload = {
                "parent": {"database_id": NOTION_DATABASE_ID},
                "properties": {
                    NOTION_PROP_TITLE: {"title": [{"text": {"content": name}}]},
                    NOTION_PROP_CATEGORY: {"select": {"name": category}},
                    NOTION_PROP_STATUS: {status_type: {"name": status_name}},
                },
            }
            if detail:
                # 詳細はページ本文（段落ブロック）として書き込む
                payload["children"] = [
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {"rich_text": [{"type": "text", "text": {"content": detail}}]},
                    }
                ]
            resp = await client.post(NOTION_PAGES_URL, headers=_notion_headers(), json=payload)

        if resp.status_code >= 400:
            logger.warning("Notionへの登録に失敗しました (status=%s): %s", resp.status_code, resp.text[:500])
    except Exception:
        logger.exception("Notionへの登録中に例外が発生しました")


# ---- モーダル(入力フォーム) --------------------------------------------------

class SuggestionModal(discord.ui.Modal, title=MODAL_TITLE):
    name_field = discord.ui.Label(
        text=NAME_LABEL,
        component=discord.ui.TextInput(
            style=discord.TextStyle.short,
            placeholder=NAME_PLACEHOLDER,
            max_length=100,
            custom_id=NAME_CUSTOM_ID,
        ),
    )
    detail_field = discord.ui.Label(
        text=DETAIL_LABEL,
        component=discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            placeholder=DETAIL_PLACEHOLDER,
            max_length=DETAIL_MAX_LENGTH,
            required=False,
            custom_id=DETAIL_CUSTOM_ID,
        ),
    )
    category_field = discord.ui.Label(
        text=CATEGORY_LABEL,
        component=discord.ui.Select(
            placeholder=CATEGORY_PLACEHOLDER,
            options=[discord.SelectOption(label=c, value=c) for c in CATEGORIES],
            custom_id=CATEGORY_CUSTOM_ID,
        ),
    )

    def __init__(self, bot: discord.Client):
        super().__init__(custom_id=MODAL_CUSTOM_ID, timeout=None)
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = str(self.name_field.component.value).strip()
        detail = str(self.detail_field.component.value or "").strip()
        selected = self.category_field.component.values
        category = selected[0] if selected else CATEGORIES[-1]

        if not name:
            await interaction.response.send_message("名前が空のため送信できません。", ephemeral=True)
            return

        remaining = _remaining_cooldown(interaction.user.id)
        if remaining > 0:
            await interaction.response.send_message(
                f"連続送信を防ぐため、あと{int(remaining) + 1}秒お待ちください。",
                ephemeral=True,
            )
            return

        # Notion登録を含めると3秒の応答制限に間に合わない可能性があるため、先にdeferする
        await interaction.response.defer(ephemeral=True)

        board_channel = interaction.channel
        channel_name = getattr(board_channel, "name", str(board_channel.id))

        log_channel = None
        if interaction.guild is not None and SUGGESTION_LOG_CHANNEL_ID:
            log_channel = interaction.guild.get_channel(SUGGESTION_LOG_CHANNEL_ID)

        # 1. 匿名投稿（送信者情報は含めない）
        embed = discord.Embed(title=name, description=detail or None, color=discord.Color.blurple())
        embed.set_author(name="📮 匿名のアイデア")
        embed.add_field(name="カテゴリ", value=category, inline=True)
        await board_channel.send(embed=embed)
        _record_submission(interaction.user.id)

        # 2. 運営専用ログ（送信者がわかる）
        if log_channel is not None:
            log_embed = discord.Embed(
                title=name,
                description=detail or None,
                color=discord.Color.dark_grey(),
                timestamp=discord.utils.utcnow(),
            )
            log_embed.set_author(
                name=f"{interaction.user} ({interaction.user.id})",
                icon_url=interaction.user.display_avatar.url,
            )
            log_embed.add_field(name="カテゴリ", value=category, inline=True)
            log_embed.set_footer(text=f"#{channel_name} への投稿")
            await log_channel.send(embed=log_embed)
        else:
            logger.warning("SUGGESTION_LOG_CHANNEL_ID のチャンネルが取得できません: %s", SUGGESTION_LOG_CHANNEL_ID)

        # 3. Notion登録（失敗しても上の2つはすでに完了している）
        await _submit_to_notion(name=name, category=category, detail=detail)

        await interaction.followup.send("アイデアを匿名で送信しました。ご協力ありがとうございます！", ephemeral=True)

        # 投稿後、パネルを最下部に貼り直す
        await _refresh_board_button(self.bot, board_channel)


# ---- ボタン(永続View) -------------------------------------------------------

class SuggestionBoardView(discord.ui.View):
    def __init__(self, bot: discord.Client):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label=BUTTON_LABEL,
        style=discord.ButtonStyle.primary,
        emoji=BUTTON_EMOJI,
        custom_id=BUTTON_CUSTOM_ID,
    )
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(SuggestionModal(self.bot))


# ---- パネルをチャンネル最下部に保つロジック ------------------------------------

def _build_panel_embed() -> discord.Embed:
    return discord.Embed(title=PANEL_TITLE, description=PANEL_DESCRIPTION, color=PANEL_COLOR)


async def _refresh_board_button(bot: discord.Client, channel: discord.abc.Messageable) -> None:
    """既存のパネルメッセージを削除し、チャンネル最下部に新しく貼り直す"""
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
            logger.exception("既存のパネルメッセージ削除に失敗しました")

    new_msg = await channel.send(embed=_build_panel_embed(), view=SuggestionBoardView(bot))
    state[key] = new_msg.id
    _save_state(state)


async def _remove_board_button(channel: discord.abc.Messageable) -> bool:
    """パネルを解除する。設置されていた場合は True を返す"""
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

def _chain_event(bot: discord.Client, name: str, handler) -> None:
    """discord.Client には add_listener が無く、イベントごとにハンドラが1つしか持てない。
    bot/events.py や bot/commands/__init__.py と同じく、既存ハンドラを保持したまま
    後ろに処理を連結する（上書きしない）。"""
    original = getattr(bot, name, None)

    async def _chained(*args, **kwargs) -> None:
        if original is not None:
            await original(*args, **kwargs)
        await handler(*args, **kwargs)

    setattr(bot, name, _chained)


def setup_suggestions(bot: discord.Client, tree: app_commands.CommandTree) -> None:
    """bot_only.py 等から一度だけ呼び出す（setup_events / setup_commands の後に呼ぶこと）。

    - 永続Viewの登録（再起動後もボタンを押せるようにする）
    - チャンネルへの新規投稿を監視し、パネルを最下部に保つ処理の連結
    - 管理者用のパネル設置/解除スラッシュコマンド登録
    """

    # 非async文脈では bot.loop 参照も View 生成もできないため、
    # 永続Viewの登録は on_ready（async上）で一度だけ行う
    view_registered = False

    async def _on_ready() -> None:
        nonlocal view_registered
        if view_registered:
            return
        bot.add_view(SuggestionBoardView(bot))
        view_registered = True
        logger.info("SuggestionBoardView を永続Viewとして登録しました")

    async def _on_message(message: discord.Message) -> None:
        if message.guild is None:
            return
        if bot.user is not None and message.author.id == bot.user.id:
            # パネル自身の再設置による無限ループを避ける
            return
        state = _load_state()
        if str(message.channel.id) not in state:
            return
        await _refresh_board_button(bot, message.channel)

    _chain_event(bot, "on_ready", _on_ready)
    _chain_event(bot, "on_message", _on_message)

    @tree.command(name="suggestion_board_setup", description="このチャンネルにアイデア送信パネルを設置します")
    @app_commands.checks.has_permissions(administrator=True)
    async def suggestion_board_setup(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await _refresh_board_button(bot, interaction.channel)
        await interaction.followup.send("このチャンネルにアイデア送信パネルを設置しました。", ephemeral=True)

    @tree.command(name="suggestion_board_remove", description="このチャンネルのアイデア送信パネルを解除します")
    @app_commands.checks.has_permissions(administrator=True)
    async def suggestion_board_remove(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        removed = await _remove_board_button(interaction.channel)
        msg = "パネルを解除しました。" if removed else "このチャンネルにはパネルが設置されていません。"
        await interaction.followup.send(msg, ephemeral=True)
