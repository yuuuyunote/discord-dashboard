"""
bot/ui/report_flow.py
/report のUI部分。

1. CategoryConsentView — 通報者が見るephemeralメッセージ。カテゴリSelect Menu + 同意ボタン。
   target_typeに応じてカテゴリ選択肢を出し分ける。
2. ApprovalView — メンテナ専用チャンネルに投稿される承認/却下ボタン。
   target_typeに応じてgithub.py側の対応する保存先（users/servers/bots）へcommitする。
3. RejectReasonModal — 却下時の理由入力。
"""

import datetime
from typing import Optional

import discord

from bot.commands.categories import label_for, target_type_label
from bot.reports import db
from bot.reports.github import (
    ReportCommitError,
    SchemaValidationError,
    commit_record,
    get_existing_record,
    merge_records,
)

CONFIRMATION_TEXT = (
    "**送信前にご確認ください**\n"
    "① 虚偽・悪意のある通報を行った場合、Botの利用を制限する場合があります\n"
    "② 証拠画像は運営メンバーのみ閲覧可能な非公開チャンネルに保存されます\n"
    "③ 通報内容によっては却下される場合があり、却下理由は通報者にDMで通知されます"
)

_RELATED_ID_FIELD_LABEL = {"server": "作成者ID", "bot": "開発者ID"}


class CategorySelect(discord.ui.Select):
    def __init__(self, target_type: str):
        from bot.commands.categories import CATEGORY_LABELS_BY_TYPE

        labels = CATEGORY_LABELS_BY_TYPE.get(target_type, CATEGORY_LABELS_BY_TYPE["user"])
        options = [
            discord.SelectOption(label=label, value=category_id)
            for category_id, label in labels.items()
        ]
        super().__init__(
            placeholder="カテゴリを選択してください（複数選択可）",
            min_values=1,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "CategoryConsentView" = self.view  # type: ignore[assignment]
        view.selected_categories = list(self.values)
        await interaction.response.edit_message(content=view.render(), view=view)


class SubmitButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="同意して送信", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "CategoryConsentView" = self.view  # type: ignore[assignment]
        await view.on_submit(interaction)


class CategoryConsentView(discord.ui.View):
    def __init__(
        self,
        *,
        target_type: str,
        reporter: discord.abc.User,
        target_id: str,
        target_username: str,
        note: Optional[str],
        evidence_attachment: discord.Attachment,
        maintainer_channel: discord.abc.Messageable,
        related_id: Optional[str] = None,
        timeout: float = 600,
    ):
        super().__init__(timeout=timeout)
        self.target_type = target_type
        self.reporter = reporter
        self.target_id = target_id
        self.target_username = target_username  # user/bot: ユーザー名, server: サーバー名
        self.note = note
        self.evidence_attachment = evidence_attachment
        self.maintainer_channel = maintainer_channel
        self.related_id = related_id
        self.selected_categories: list[str] = []

        self.select = CategorySelect(target_type)
        self.add_item(self.select)
        self.add_item(SubmitButton())

    def render(self) -> str:
        chosen = (
            "、".join(label_for(c, self.target_type) for c in self.selected_categories)
            if self.selected_categories
            else "（未選択）"
        )
        return (
            f"通報対象（{target_type_label(self.target_type)}）: `{self.target_id}`（{self.target_username}）\n"
            f"選択中のカテゴリ: {chosen}\n\n"
            f"{CONFIRMATION_TEXT}"
        )

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.selected_categories:
            await interaction.response.send_message(
                "カテゴリを1つ以上選択してから送信してください。", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        for item in self.children:
            item.disabled = True

        try:
            report_id = await db.insert_pending_report(
                reporter_id=str(self.reporter.id),
                reporter_username=self.reporter.name,
                target_type=self.target_type,
                target_id=self.target_id,
                target_username=self.target_username,
                categories=self.selected_categories,
                note=self.note,
                creator_or_developer_id=self.related_id,
            )
        except NotImplementedError:
            await interaction.edit_original_response(
                content="（データベース未接続のため、この先の保存はまだ動作しません。GitHub連携部分の確認だけ進めています。）",
                view=None,
            )
            return

        evidence_file = await self.evidence_attachment.to_file()
        categories_label = "、".join(label_for(c, self.target_type) for c in self.selected_categories)
        embed = discord.Embed(
            title=f"新しい通報（{target_type_label(self.target_type)}）", color=discord.Color.orange()
        )
        embed.add_field(name="報告者", value=f"{self.reporter.mention}（`{self.reporter.id}`）", inline=False)
        embed.add_field(
            name=f"対象{target_type_label(self.target_type)}",
            value=f"{self.target_username}（`{self.target_id}`）",
            inline=False,
        )
        related_label = _RELATED_ID_FIELD_LABEL.get(self.target_type)
        if related_label and self.related_id:
            embed.add_field(name=related_label, value=f"`{self.related_id}`", inline=False)
        embed.add_field(name="カテゴリ", value=categories_label, inline=False)
        embed.add_field(name="補足", value=self.note or "（なし）", inline=False)
        embed.set_image(url=f"attachment://{evidence_file.filename}")

        approval_view = ApprovalView(report_id=report_id)
        sent = await self.maintainer_channel.send(embed=embed, file=evidence_file, view=approval_view)
        await db.set_maintainer_message_id(report_id, str(sent.id))

        await interaction.edit_original_response(
            content="送信しました。結果は追ってDMでお知らせします。", view=None
        )


class RejectReasonModal(discord.ui.Modal, title="却下理由"):
    reason = discord.ui.TextInput(
        label="却下理由（通報者にDMで送られます）",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500,
    )

    def __init__(self, approval_view: "ApprovalView"):
        super().__init__()
        self.approval_view = approval_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.approval_view.finalize_rejection(interaction, str(self.reason))


class ApprovalView(discord.ui.View):
    def __init__(self, *, report_id: int, timeout: Optional[float] = None):
        super().__init__(timeout=timeout)
        self.report_id = report_id

    @discord.ui.button(label="承認", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        for item in self.children:
            item.disabled = True

        try:
            report = await db.get_report(self.report_id)
        except NotImplementedError:
            await interaction.followup.send(
                "（データベース未接続のため承認処理はまだ完結できません。）", ephemeral=True
            )
            return

        today = datetime.date.today().isoformat()
        try:
            existing = await get_existing_record(report.target_type, report.target_id)
            merged = merge_records(
                report.target_type,
                existing,
                target_id=report.target_id,
                display_snapshot=report.target_username,
                categories=report.categories,
                note=report.note,
                today=today,
                creator_or_developer_id=report.creator_or_developer_id,
            )
            await commit_record(
                report.target_type,
                merged,
                commit_message=f"report: list {report.target_type}:{report.target_id} (report #{report.id})",
            )
        except SchemaValidationError as e:
            await interaction.followup.send(f"スキーマ検証に失敗しました: {e}", ephemeral=True)
            for item in self.children:
                item.disabled = False
            return
        except ReportCommitError as e:
            await interaction.followup.send(f"GitHubへのcommitに失敗しました: {e}", ephemeral=True)
            for item in self.children:
                item.disabled = False
            return

        await db.mark_merged(self.report_id)

        reporter = await interaction.client.fetch_user(int(report.reporter_id))
        try:
            await reporter.send(
                f"通報（対象: `{report.target_id}` / {target_type_label(report.target_type)}）が承認され、"
                "通報リストに反映されました。ご協力ありがとうございます。"
            )
        except discord.Forbidden:
            pass  # DM拒否設定。専用チャンネル側の表示のみで良しとする。

        await interaction.message.edit(
            content=f"✅ 承認済み（{interaction.user.mention}）", view=self
        )

    @discord.ui.button(label="却下", style=discord.ButtonStyle.secondary)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(RejectReasonModal(self))

    async def finalize_rejection(self, interaction: discord.Interaction, reason: str) -> None:
        await interaction.response.defer()
        for item in self.children:
            item.disabled = True

        try:
            report = await db.mark_rejected(self.report_id, reason)
        except NotImplementedError:
            await interaction.followup.send(
                "（データベース未接続のため却下処理はまだ完結できません。）", ephemeral=True
            )
            return

        reporter = await interaction.client.fetch_user(int(report.reporter_id))
        try:
            await reporter.send(
                f"通報（対象: `{report.target_id}` / {target_type_label(report.target_type)}）は却下されました。\n"
                f"理由: {reason}"
            )
        except discord.Forbidden:
            pass

        await interaction.message.edit(
            content=f"❌ 却下済み（{interaction.user.mention} / 理由: {reason}）", view=self
        )
