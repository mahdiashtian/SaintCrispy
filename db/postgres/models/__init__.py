from .conversation import MODEL as CONVERSATION
from .link_history import MODEL as LINK_HISTORY
from .media_alias import MODEL as MEDIA_ALIAS
from .media_catalog import MODEL as MEDIA_CATALOG
from .media_file import MODEL as MEDIA_FILE
from .media_history import MODEL as MEDIA_HISTORY
from .menu import MODEL as MENU
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
TABLES = MEDIA_TABLES + WORKFLOW_TABLES
SCHEMA = "\n".join(table.schema for table in MEDIA_TABLES)
WORKFLOW_SCHEMA = "\n".join(table.schema for table in WORKFLOW_TABLES)
