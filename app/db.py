"""服务端业务模块。"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import DATABASE_URL


def _make_engine(url: str) -> Engine:
    connect_args: dict[str, object] = {}
    if url.startswith("sqlite"):
        # 连接可能在线程池的不同工作线程间复用
        connect_args["check_same_thread"] = False
    engine = create_engine(
        url, future=True, pool_pre_ping=True, connect_args=connect_args
    )

    if engine.dialect.name == "sqlite":
        # SQLite 默认的 BEGIN DEFERRED 只在首次写时加预留锁，两个并发事务
        # 可能同时持有共享锁随后互相死锁。事务开始即申请预留锁，让“撤销”
        # 与“确认”严格串行化，后者拿到锁后读到的一定是最新授权状态。
        @event.listens_for(engine, "connect")
        def _set_busy_timeout(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA busy_timeout = 30000")
            cursor.close()

        @event.listens_for(engine, "begin")
        def _begin_immediate(sa_conn):
            sa_conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def make_engine(url: str) -> Engine:
    return _make_engine(url)


engine = _make_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Iterator[Session]:
    """执行确定性的业务处理。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
