import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core.database import Base, Session, ChatMessage
from datetime import datetime

def test_sqlite_foreign_keys_cascade():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    
    TestSessionLocal = sessionmaker(bind=engine)
    db = TestSessionLocal()
    
    session_id = "test-session-123"
    s = Session(
        id=session_id,
        name="Test Session",
        endpoint_url="http://localhost:8000",
        model="gpt-4",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    m = ChatMessage(id="test-msg-123", session_id=session_id, role="user", content="test message")
    
    db.add(s)
    db.add(m)
    db.commit()
    
    assert db.query(Session).count() == 1
    assert db.query(ChatMessage).count() == 1
    
    db.query(Session).filter(Session.id == session_id).delete()
    db.commit()
    
    assert db.query(ChatMessage).count() == 0
    
    db.close()


def test_sqlite_file_connections_enable_wal_and_busy_timeout(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'app.db'}",
        connect_args={"check_same_thread": False},
    )

    with engine.connect() as conn:
        journal_mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        busy_timeout = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()

    assert journal_mode.lower() == "wal"
    assert busy_timeout == 5000