"""Bounded message batches preserving exact ledgers, counters and leave barriers."""

from collections import defaultdict


async def apply_messages(connection, bot_id, events):
    if not events or any(event.kind != "message" for event in events):
        return None
    chats = sorted({event.chat_id for event in events})
    rows = await connection.fetch(
        "SELECT chat_id,is_active,migrated_to FROM bot_groups WHERE telegram_account_id=$1 AND chat_id=ANY($2::bigint[]) ORDER BY chat_id FOR UPDATE",
        bot_id,
        chats,
    )
    groups = {row["chat_id"]: row for row in rows}
    if len(groups) != len(chats) or any(row["migrated_to"] is not None for row in rows):
        return None
    keys = sorted({(event.chat_id, event.user_id) for event in events})
    await connection.fetch(
        "SELECT user_id FROM bot_users WHERE telegram_account_id=$1 AND user_id=ANY($2::bigint[]) ORDER BY user_id FOR UPDATE",
        bot_id,
        sorted({event.user_id for event in events}),
    )
    rows = await connection.fetch(
        """SELECT m.* FROM group_memberships m JOIN unnest($2::bigint[],$3::bigint[]) k(chat_id,user_id)
        USING(chat_id,user_id) WHERE m.telegram_account_id=$1 ORDER BY m.chat_id,m.user_id FOR UPDATE OF m""",
        bot_id,
        [key[0] for key in keys],
        [key[1] for key in keys],
    )
    if len(rows) != len(keys):
        return None
    existing = {(row["chat_id"], row["user_id"]): row for row in rows}
    journal = await connection.fetch(
        """INSERT INTO membership_events(telegram_account_id,chat_id,user_id,event_key,kind,occurred_at,observed_admin)
        SELECT $1,chat,user_id,key,'message',stamp,eligible FROM unnest($2::bigint[],$3::bigint[],$4::text[],$5::timestamptz[],$6::bool[]) x(chat,user_id,key,stamp,eligible)
        ON CONFLICT DO NOTHING RETURNING chat_id,event_key""",
        bot_id,
        [e.chat_id for e in events],
        [e.user_id for e in events],
        [e.event_key for e in events],
        [e.occurred_at for e in events],
        [e.observed_admin for e in events],
    )
    if not journal:
        return 0
    by_key = {}
    for event in events:
        by_key.setdefault((event.chat_id, event.event_key), event)
    accepted = [by_key[(row["chat_id"], row["event_key"])] for row in journal]
    members = defaultdict(list)
    users, activity = {}, {}
    for event in accepted:
        members[(event.chat_id, event.user_id)].append(event)
        if event.user_id not in users or event.occurred_at >= users[event.user_id].occurred_at:
            users[event.user_id] = event
        activity[event.chat_id] = max(
            activity.get(event.chat_id, event.occurred_at), event.occurred_at
        )
    values = []
    for (chat, user), items in sorted(members.items()):
        row = existing[(chat, user)]
        stamp = max(event.occurred_at for event in items)
        weeks = defaultdict(int)
        for event in items:
            weeks[int(event.occurred_at.timestamp()) // 604800] += 1
        week = max(row["msg_week"], max(weeks))
        this = (row["msg_this_week"] if row["msg_week"] == week else 0) + weeks[week]
        last = (
            row["msg_last_week"]
            if row["msg_week"] == week
            else row["msg_this_week"]
            if row["msg_week"] == week - 1
            else 0
        ) + weeks[week - 1]
        active = True if groups[chat]["is_active"] is True else row["is_active"]
        values.append(
            (
                chat,
                user,
                stamp > row["state_changed_at"],
                active,
                stamp,
                len(items),
                week,
                this,
                last,
            )
        )
    arrays = [list(column) for column in zip(*values, strict=True)]
    await connection.execute(
        """UPDATE group_memberships m SET is_active=CASE WHEN x.newer THEN x.active ELSE m.is_active END,
        state_changed_at=CASE WHEN x.newer THEN x.stamp ELSE m.state_changed_at END,
        state_revision=CASE WHEN x.newer THEN nextval('membership_state_revision') ELSE m.state_revision END,
        last_seen_at=GREATEST(m.last_seen_at,x.stamp),message_count=m.message_count+x.n,
        msg_week=x.week,msg_this_week=x.this,msg_last_week=x.last,updated_at=now()
        FROM unnest($2::bigint[],$3::bigint[],$4::bool[],$5::bool[],$6::timestamptz[],$7::bigint[],$8::int[],$9::int[],$10::int[]) x(chat,user_id,newer,active,stamp,n,week,this,last)
        WHERE m.telegram_account_id=$1 AND m.chat_id=x.chat AND m.user_id=x.user_id""",
        bot_id,
        *arrays,
    )
    ordered = [users[key] for key in sorted(users)]
    await connection.execute(
        """UPDATE bot_users u SET username=CASE WHEN x.stamp>=u.last_seen_at THEN COALESCE(x.username,u.username) ELSE u.username END,
        first_name=CASE WHEN x.stamp>=u.last_seen_at THEN COALESCE(x.first,u.first_name) ELSE u.first_name END,
        last_name=CASE WHEN x.stamp>=u.last_seen_at THEN COALESCE(x.last,u.last_name) ELSE u.last_name END,
        account_kind='real',last_seen_at=GREATEST(u.last_seen_at,x.stamp),last_active_at=GREATEST(u.last_active_at,x.stamp)
        FROM unnest($2::bigint[],$3::text[],$4::text[],$5::text[],$6::timestamptz[]) x(user_id,username,first,last,stamp)
        WHERE u.telegram_account_id=$1 AND u.user_id=x.user_id""",
        bot_id,
        [e.user_id for e in ordered],
        [e.username for e in ordered],
        [e.first_name for e in ordered],
        [e.last_name for e in ordered],
        [e.occurred_at for e in ordered],
    )
    await connection.execute(
        """UPDATE bot_groups g SET last_activity_at=GREATEST(g.last_activity_at,x.stamp)
        FROM unnest($2::bigint[],$3::timestamptz[]) x(chat,stamp) WHERE g.telegram_account_id=$1 AND g.chat_id=x.chat""",
        bot_id,
        sorted(activity),
        [activity[key] for key in sorted(activity)],
    )
    return len(accepted)
