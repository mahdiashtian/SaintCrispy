from .administration import AUDIT, JOB, OUTBOX, REQUEST, SETTING
from .conversation import MODEL as CONVERSATION
from .file_expiry import MODEL as FILE_EXPIRY
from .group import MODEL as GROUP
from .group_event import MODEL as GROUP_EVENT
from .group_request_limit import MODEL as GROUP_REQUEST_LIMIT
from .link_history import MODEL as LINK_HISTORY
from .media_alias import MODEL as MEDIA_ALIAS
from .media_catalog import MODEL as MEDIA_CATALOG
from .media_file import MODEL as MEDIA_FILE
from .media_history import MODEL as MEDIA_HISTORY
from .membership import EVENT as MEMBERSHIP_EVENT
from .membership import MODEL as MEMBERSHIP
from .menu import MODEL as MENU
from .peer import MODEL as PEER
from .request_limit import MODEL as REQUEST_LIMIT
from .site_account import MODEL as SITE_ACCOUNT
from .user import MODEL as USER

MEDIA_TABLES = (
    MEDIA_FILE,
    SITE_ACCOUNT,
    REQUEST_LIMIT,
    LINK_HISTORY,
    MEDIA_HISTORY,
    MEDIA_CATALOG,
    MEDIA_ALIAS,
)
WORKFLOW_TABLES = (
    USER,
    CONVERSATION,
    MENU,
)
GROUP_TABLES = (GROUP, GROUP_EVENT, GROUP_REQUEST_LIMIT)
CONTROL_TABLES = (MEMBERSHIP, MEMBERSHIP_EVENT, SETTING, AUDIT, REQUEST, JOB, OUTBOX, PEER)
TABLES = MEDIA_TABLES + WORKFLOW_TABLES + GROUP_TABLES + (FILE_EXPIRY,) + CONTROL_TABLES
SCHEMA = "\n".join(table.schema for table in MEDIA_TABLES)
WORKFLOW_SCHEMA = "\n".join(table.schema for table in WORKFLOW_TABLES)
GROUP_SCHEMA = "\n".join(table.schema for table in GROUP_TABLES)
