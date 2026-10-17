"""Every admin callback is scoped to its private owner."""

from telethon import Button


def compact_id(value):
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    sign = "-" if value < 0 else ""
    value = abs(value)
    result = ""
    while value:
        value, remainder = divmod(value, 36)
        result = alphabet[remainder] + result
    return sign + (result or "0")


def button(owner, label, action, value="0"):
    data = f"adm:{owner}:{action}:{value}".encode("ascii")
    if len(data) > 64:
        raise ValueError("Administrator callback exceeds Telegram limit")
    return Button.inline(label, data=data)


def home(owner, root=False):
    rows = [
        [button(owner, "📊 آمار", "stats"), button(owner, "📅 آمار روزانه", "daily")],
        [button(owner, "👥 گروه‌ها", "groups"), button(owner, "👤 کاربران", "users")],
        [
            button(owner, "🔎 جست‌وجوی کاربر", "prompt", "lookup"),
            button(owner, "🔎 جست‌وجوی گروه", "prompt", "group_lookup"),
        ],
        [
            button(owner, "🧾 عملیات و پیشرفت", "jobs"),
            button(owner, "🩺 بررسی سلامت گروه‌ها", "health"),
        ],
    ]
    rows.append([button(owner, "🗄 وضعیت کش", "cache")])
    if root:
        rows += [
            [
                button(owner, "📁 خروجی کاربران", "exportu"),
                button(owner, "📁 خروجی گروه‌ها", "exportg"),
            ],
            [button(owner, "📥 ورود CSV کاربران", "prompt", "import_users")],
            [
                button(owner, "➕ مدیر", "prompt", "grant"),
                button(owner, "➖ مدیر", "prompt", "revoke"),
            ],
            [
                button(owner, "⛔️ مسدودکردن", "prompt", "ban"),
                button(owner, "✅ رفع مسدودی", "prompt", "unban"),
            ],
            [
                button(owner, "📨 ارسال به کاربران", "compose", "busers"),
                button(owner, "📨 ارسال به گروه‌ها", "compose", "bgroups"),
            ],
            [
                button(owner, "↪️ فوروارد کاربران", "compose", "fusers"),
                button(owner, "↪️ فوروارد گروه‌ها", "compose", "fgroups"),
            ],
            [
                button(owner, "⚙️ تنظیمات", "settings"),
                button(owner, "⏱ سقف گروه", "prompt", "limit"),
            ],
        ]
    return rows


def back(owner):
    # Reply menus already own navigation; read results need no extra home button.
    return []


def confirmation(owner, token):
    return [
        [
            button(owner, "❌ لغو", "dismiss"),
            button(owner, "✅ بله، مطمئنم", "confirm", token),
        ]
    ]
