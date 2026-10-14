"""Application services receive a factory; transactions are never shared."""

from repositories.interfaces.unit_of_work import UnitOfWorkFactory


class TransactionalService:
    def __init__(self, uow_factory: UnitOfWorkFactory | None = None):
        self._uow_factory = uow_factory
