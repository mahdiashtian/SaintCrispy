"""An application operation owns a fresh async transaction."""

from contextlib import AbstractAsyncContextManager
from typing import Protocol

from .group import GroupRepository
from .media import MediaRepository
from .workflow import StateStorage, UserRepository


class WorkflowStore(StateStorage, UserRepository, Protocol):
    pass


class UnitOfWork(Protocol):
    workflow: WorkflowStore
    media: MediaRepository
    groups: GroupRepository


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[UnitOfWork]: ...
