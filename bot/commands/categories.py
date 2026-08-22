"""
bot/commands/categories.py
カテゴリID（categories配列に入る内部値）と日本語表示ラベルの対応表。
design-memo.mdのカテゴリ体系表と一致させること。
/report実装時のSelect Menuの選択肢もここを参照する想定。
"""

CATEGORY_LABELS = {
    "scam": "詐欺",
    "phishing": "フィッシング",
    "impersonation": "なりすまし",
    "raid-spam": "荒らし・スパム",
    "dm-solicitation": "DM勧誘",
    "harassment": "嫌がらせ",
    "doxxing": "個人情報の暴露",
    "hate-speech": "差別的言動",
    "bot-abuse": "bot悪用",
    "other": "その他",
}


def label_for(category_id: str) -> str:
    return CATEGORY_LABELS.get(category_id, category_id)
