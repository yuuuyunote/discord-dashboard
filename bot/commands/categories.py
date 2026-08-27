"""
bot/commands/categories.py
カテゴリID（categories配列に入る内部値）と日本語表示ラベルの対応表。
design-memo.mdのカテゴリ体系表と一致させること。
/report実装時のSelect Menuの選択肢もここを参照する想定。

user/server/bot の3種はカテゴリ体系が異なるため、target_type別に
テーブルを分けている。label_for は既存呼び出し（target_type省略）との
互換のため user 扱いをデフォルトにしている。

malicious-server-creator / malicious-bot-developer は、悪質サーバー/Bot通報の
承認時に作成者/開発者IDをuser側へ自動登録する際に使うカテゴリ（bot/ui/report_flow.py
のApprovalView.approve参照）。通常の/reportからも選択可能な通常カテゴリとして扱う。
"""

USER_CATEGORY_LABELS = {
    "scam": "詐欺",
    "phishing": "フィッシング",
    "impersonation": "なりすまし",
    "raid-spam": "荒らし・スパム",
    "bad-solicitation": "悪質な勧誘",
    "harassment": "嫌がらせ",
    "doxxing": "個人情報の暴露",
    "hate-speech": "差別的言動",
    "bot-abuse": "bot悪用",
    "malicious-server-creator": "悪質サーバーの作成者",
    "malicious-bot-developer": "悪質Botの開発者",
    "other": "その他",
}

SERVER_CATEGORY_LABELS = {
    "scam-phishing": "詐欺・フィッシング勧誘",
    "illegal-tos-content": "違法・規約違反コンテンツ",
    "raid-hub": "荒らし・レイド拠点",
    "other": "その他",
}

BOT_CATEGORY_LABELS = {
    "malware-token-grabber": "トークン窃取・マルウェア",
    "raid-spam": "スパム・荒らし",
    "scam-phishing": "詐欺・フィッシング",
    "impersonation": "なりすまし",
    "data-harvesting": "無断データ収集・監視",
    "other": "その他",
}

CATEGORY_LABELS_BY_TYPE = {
    "user": USER_CATEGORY_LABELS,
    "server": SERVER_CATEGORY_LABELS,
    "bot": BOT_CATEGORY_LABELS,
}

# 既存呼び出し（report_flow.py / check.py の label_for(c) 単独呼び出し）との
# 互換のため残す。中身はUSER_CATEGORY_LABELSと同じ辞書。
CATEGORY_LABELS = USER_CATEGORY_LABELS


def label_for(category_id: str, target_type: str = "user") -> str:
    labels = CATEGORY_LABELS_BY_TYPE.get(target_type, USER_CATEGORY_LABELS)
    return labels.get(category_id, category_id)


TARGET_TYPE_LABELS = {
    "user": "ユーザー",
    "server": "サーバー",
    "bot": "Bot",
}


def target_type_label(target_type: str) -> str:
    return TARGET_TYPE_LABELS.get(target_type, target_type)
