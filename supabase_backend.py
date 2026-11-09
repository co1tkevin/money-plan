"""Supabase 接入层：配置读取、账号登录、客户端缓存、适配器组装。

对上层（main.py）只暴露几个薄函数；未安装 supabase 包或未配置 URL/Key 时
is_configured() 返回 False，页面继续使用 localStorage，不影响任何原有功能。

配置方式（任选其一）：
    环境变量  SUPABASE_URL / SUPABASE_ANON_KEY
    .streamlit/secrets.toml 里写同名键
"""

from __future__ import annotations

import os

import streamlit as st

from storage_adapter import HybridAdapter, StorageAdapter, StorageError, SupabaseAdapter

SUPABASE_AVAILABLE = True
IMPORT_ERROR = ""
try:
    from supabase import create_client
except Exception as exc:  # 未安装 supabase 包
    create_client = None
    SUPABASE_AVAILABLE = False
    IMPORT_ERROR = str(exc)


def get_config() -> tuple[str, str]:
    """读取 Supabase 项目地址与 anon key，环境变量优先，其次 st.secrets。"""
    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = (os.getenv("SUPABASE_ANON_KEY") or "").strip()
    if url and key:
        return url, key
    try:
        secrets = st.secrets
        url = url or str(secrets["SUPABASE_URL"]).strip()
    except Exception:
        pass
    try:
        key = key or str(st.secrets["SUPABASE_ANON_KEY"]).strip()
    except Exception:
        pass
    return url, key


def is_configured() -> bool:
    url, key = get_config()
    return bool(SUPABASE_AVAILABLE and url and key)


def _new_client():
    url, key = get_config()
    if not (url and key):
        raise StorageError("未配置 SUPABASE_URL / SUPABASE_ANON_KEY")
    client = create_client(url, key)
    # 给 PostgREST 加超时，避免断网时页面长时间无响应
    try:
        import httpx

        if hasattr(client, "postgrest") and hasattr(client.postgrest, "session"):
            client.postgrest.session.timeout = httpx.Timeout(20.0, connect=10.0)
    except Exception:
        pass
    return client


def get_client(auth: dict | None = None):
    """返回会话内缓存的客户端；auth 有 token 时恢复登录态。"""
    token = (auth or {}).get("access_token")
    client = st.session_state.get("sb_client")
    if client is not None and st.session_state.get("sb_token") == token:
        return client

    client = _new_client()
    if auth and auth.get("access_token") and auth.get("refresh_token"):
        try:
            client.auth.set_session(auth["access_token"], auth["refresh_token"])
        except Exception as exc:
            raise StorageError(f"恢复登录状态失败：{exc}") from exc
    st.session_state["sb_client"] = client
    st.session_state["sb_token"] = token
    return client


def login(email: str, password: str) -> dict:
    """邮箱密码登录，返回可直接存进 localStorage 的会话字典。"""
    client = get_client(None)
    try:
        result = client.auth.sign_in_with_password({"email": email, "password": password})
    except Exception as exc:
        raise StorageError(str(exc)) from exc
    session = getattr(result, "session", None)
    user = getattr(result, "user", None)
    if not session or not user:
        raise StorageError("登录失败：未返回有效会话")
    return {
        "email": getattr(user, "email", email),
        "user_id": str(user.id),
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
    }


def register(email: str, password: str) -> str:
    """邮箱注册；若项目开启了邮箱验证，需要用户到邮箱点确认后才能登录。"""
    client = get_client(None)
    try:
        result = client.auth.sign_up({"email": email, "password": password})
    except Exception as exc:
        raise StorageError(str(exc)) from exc
    session = getattr(result, "session", None)
    user = getattr(result, "user", None)
    if session and user:
        return "注册成功，已自动登录。"
    return "注册成功，请到邮箱完成验证后再登录。"


def logout() -> None:
    client = st.session_state.get("sb_client")
    if client is not None:
        try:
            client.auth.sign_out()
        except Exception:
            pass
    for key in ("sb_client", "sb_token"):
        st.session_state.pop(key, None)


def build_store(local: StorageAdapter, auth: dict | None, errors: list[str] | None = None) -> StorageAdapter:
    """按登录态组装存储：未登录走本地；已登录走云端优先 + 本地缓存的混合适配器。"""
    if not auth or not is_configured():
        return local
    try:
        client = get_client(auth)
    except Exception as exc:
        if errors is not None:
            errors.append(str(exc))
        return local
    return HybridAdapter(SupabaseAdapter(client, auth.get("user_id", "")), local)


def friendly_error(exc: Exception) -> str:
    """把云端/网络异常翻译成人话，避免页面直接抛堆栈。"""
    text = str(exc).strip() or exc.__class__.__name__
    lowered = text.lower()
    if "invalid login credentials" in lowered:
        return "邮箱或密码不正确。"
    if "email not confirmed" in lowered:
        return "邮箱尚未验证，请先到邮箱点击确认链接。"
    if "user already registered" in lowered:
        return "该邮箱已注册，请直接登录。"
    if "password should be at least" in lowered:
        return "密码长度不足（至少 6 位）。"
    if "rate limit" in lowered or "too many requests" in lowered:
        return "请求过于频繁，请稍后再试。"
    if "timeout" in lowered or "timed out" in lowered or "connect" in lowered:
        return "网络连接失败或超时，请检查网络后重试（本地数据不受影响）。"
    if "permission denied for table" in lowered:
        return "云端表缺少授权：请在 Supabase SQL Editor 执行 supabase_schema.sql 里的 grant 语句。"
    if "could not find the table" in lowered or "pgrst205" in lowered:
        return "云端表不存在：请先在 SQL Editor 执行 supabase_schema.sql 建表。"
    if "permission denied" in lowered or "row-level security" in lowered or "401" in lowered:
        return "云端权限校验失败（RLS 或登录已过期），请重新登录。"
    return text[:300]
