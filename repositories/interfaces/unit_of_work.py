"""An application operation owns a fresh async transaction."""

from contextlib import AbstractAsyncContextManager
from typing import Protocol

from .media import MediaRepository
from .workflow import StateStorage, UserRepository


class WorkflowStore(StateStorage, UserRepository, Protocol):
    pass


class UnitOfWork(Protocol):
    workflow: WorkflowStore
    media: MediaRepository


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[UnitOfWork]: ...
