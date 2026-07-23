import uuid
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone

from sqlalchemy import String, Boolean, Integer, JSON, ForeignKey, DateTime, BigInteger
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

def utcnow():
    return datetime.now(timezone.utc)

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(50), default="user")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    
    # Sharing
    share_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    share_token: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    share_password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    
    # Traffic
    traffic_reset_strategy: Mapped[str] = mapped_column(String(50), default="never")
    traffic_used: Mapped[int] = mapped_column(BigInteger, default=0)
    traffic_total: Mapped[int] = mapped_column(BigInteger, default=0)
    last_reset_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expiration_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    traffic_limit: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    
    # Optional Profile
    telegramId: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    remnawave_uuid: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    connections: Mapped[List["UserConnection"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    api_tokens: Mapped[List["ApiToken"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Server(Base):
    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(255))
    host: Mapped[str] = mapped_column(String(255))
    ssh_port: Mapped[int] = mapped_column(Integer, default=22)
    username: Mapped[str] = mapped_column(String(255))
    password: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    private_key: Mapped[Optional[str]] = mapped_column(String(4096), nullable=True)
    server_info: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    host_fingerprint: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    protocols: Mapped[List["ServerProtocol"]] = relationship(back_populates="server", cascade="all, delete-orphan")
    connections: Mapped[List["UserConnection"]] = relationship(back_populates="server", cascade="all, delete-orphan")


class ServerProtocol(Base):
    __tablename__ = "server_protocols"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    protocol_type: Mapped[str] = mapped_column(String(50))
    installed: Mapped[bool] = mapped_column(Boolean, default=False)
    port: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    base_protocol: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    instance: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    container_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    extra_params: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)

    server: Mapped["Server"] = relationship(back_populates="protocols")


class UserConnection(Base):
    __tablename__ = "user_connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    protocol: Mapped[str] = mapped_column(String(50))
    client_id: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    user: Mapped["User"] = relationship(back_populates="connections")
    server: Mapped["Server"] = relationship(back_populates="connections")


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    token_hash: Mapped[str] = mapped_column(String(255))
    token_prefix: Mapped[str] = mapped_column(String(10))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship(back_populates="api_tokens")


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[Dict[str, Any]] = mapped_column(JSON)
