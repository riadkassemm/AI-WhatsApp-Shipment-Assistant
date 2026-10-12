import asyncio
from typing import Any

from app.support_repository import SupportRepository


class _TupleCursor:
    def __init__(self) -> None:
        self._reaction_query = False

    async def __aenter__(self) -> "_TupleCursor":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def execute(self, sql: str, _params: tuple[Any, ...]) -> None:
        self._reaction_query = "support_message_reactions" in sql

    async def fetchall(self) -> tuple[dict[str, Any], ...]:
        # aiomysql's DictCursor returns a tuple from fetchall() in production.
        if self._reaction_query:
            return ()
        return (
            {"id": 3, "body": "third"},
            {"id": 2, "body": "second"},
            {"id": 1, "body": "first"},
        )


class _TupleConnection:
    def cursor(self, *_args: Any, **_kwargs: Any) -> _TupleCursor:
        return _TupleCursor()


class _AcquireContext:
    async def __aenter__(self) -> _TupleConnection:
        return _TupleConnection()

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _TuplePool:
    def acquire(self) -> _AcquireContext:
        return _AcquireContext()


def test_list_messages_accepts_tuple_rows_from_aiomysql() -> None:
    async def run() -> None:
        repository = SupportRepository()
        repository._pool = _TuplePool()  # type: ignore[assignment]

        rows = await repository.list_messages(52, limit=150)

        assert isinstance(rows, list)
        assert [row["id"] for row in rows] == [1, 2, 3]

    asyncio.run(run())
