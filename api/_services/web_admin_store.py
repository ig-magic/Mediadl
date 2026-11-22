"""
Zero-Database `/tmp` Admin Analytics, API Key Manager & Live Telegram Notifier
==============================================================================
100% Vercel Serverless safe — writes exclusively to `tempfile.gettempdir()`
(`/tmp`) without any SQLite/SQL database, manages `/{key}/...` API keys, and
optionally forwards every Web/API search in real time to the Admin's Telegram chat.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple
import requests

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_WEB_TMP = os.path.join(tempfile.gettempdir(), "social_web_admin_history.tmp.json")


def now_ist_str() -> str:
    return datetime.now(IST).strftime("%d %b %Y, %I:%M:%S %p")


class WebAdminTempStore:
    """Thread-safe `/tmp` store for Web & API visitor search histories + API keys."""

    def __init__(self, tmp_path: str = DEFAULT_WEB_TMP) -> None:
        self.tmp_path = tmp_path
        self._lock = threading.Lock()
        self.started_at = time.time()
        self.started_ist = now_ist_str()

        self.admin_key: str = (os.getenv("ADMIN_KEY") or "7707859083").strip()
        self.bot_token: str = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
        raw_ids = os.getenv("ADMIN_IDS", "7707859083")
        self.admin_ids: Set[int] = {
            int(p.strip())
            for p in raw_ids.replace(";", ",").split(",")
            if p.strip().lstrip("-").isdigit()
        } or {7707859083}

        raw_env_keys = os.getenv("API_KEYS", "demo,free,socialgrab")
        self.env_api_keys: Set[str] = {
            k.strip() for k in raw_env_keys.replace(";", ",").split(",") if k.strip()
        }
        self.env_api_keys.add(self.admin_key)

        self.live_tg_alerts: bool = True
        self.open_key_mode: bool = False  # False = validate against env + custom keys; True = accept any non-empty key
        self.custom_api_keys: Dict[str, Dict[str, Any]] = {}
        self.revoked_keys: Set[str] = set()
        self.api_key_usage: Dict[str, int] = {}

        self.metrics: Dict[str, int] = {
            "total_searches": 0,
            "ig_searches": 0,
            "snap_searches": 0,
            "tg_searches": 0,
            "api_calls": 0,
            "errors_caught": 0,
        }
        self.visitors: Dict[str, Dict[str, Any]] = {}
        self.recent_searches: List[Dict[str, Any]] = []
        self._load_tmp()

    def verify_key(self, key: Optional[str]) -> bool:
        """Verify Admin Panel key."""
        if not key:
            return False
        k = key.strip()
        return k == self.admin_key or (k.isdigit() and int(k) in self.admin_ids)

    def validate_api_key(self, api_key: Optional[str]) -> bool:
        """Validate `/{key}/...` path API key."""
        if not api_key:
            return False
        k = api_key.strip()
        if not k or len(k) > 80:
            return False
        with self._lock:
            if k in self.revoked_keys:
                return False
            if self.open_key_mode:
                return True
            if k in self.env_api_keys or k in self.custom_api_keys or self.verify_key(k):
                return True
            return False

    def create_api_key(self, custom_key: str = "", label: str = "") -> str:
        """Create or register a custom API key in `/tmp`."""
        k = custom_key.strip() or ("sg_" + secrets.token_hex(6))
        k = "".join(ch for ch in k if ch.isalnum() or ch in ("_", "-"))[:48]
        if not k:
            k = "sg_" + secrets.token_hex(6)
        with self._lock:
            self.revoked_keys.discard(k)
            self.custom_api_keys[k] = {
                "key": k,
                "label": (label or "Custom Key").strip()[:60],
                "created_at": now_ist_str(),
            }
            self._flush_unlocked()
        return k

    def delete_api_key(self, target_key: str) -> None:
        k = (target_key or "").strip()
        if not k or k == self.admin_key:
            return
        with self._lock:
            self.custom_api_keys.pop(k, None)
            if k in self.env_api_keys:
                self.revoked_keys.add(k)
            self._flush_unlocked()

    def toggle_open_key_mode(self) -> bool:
        with self._lock:
            self.open_key_mode = not self.open_key_mode
            self._flush_unlocked()
            return self.open_key_mode

    def list_api_keys(self) -> List[Dict[str, Any]]:
        with self._lock:
            out: List[Dict[str, Any]] = []
            seen: Set[str] = set()
            for ek in sorted(self.env_api_keys):
                if ek in self.revoked_keys:
                    continue
                seen.add(ek)
                out.append(
                    {
                        "key": ek,
                        "label": "Owner Key" if ek == self.admin_key else "Built-in / Env Key",
                        "created_at": "Default",
                        "calls": self.api_key_usage.get(ek, 0),
                    }
                )
            for ck, meta in self.custom_api_keys.items():
                if ck in seen or ck in self.revoked_keys:
                    continue
                seen.add(ck)
                out.append(
                    {
                        "key": ck,
                        "label": meta.get("label", "Custom Key"),
                        "created_at": meta.get("created_at", ""),
                        "calls": self.api_key_usage.get(ck, 0),
                    }
                )
            # Also surface any dynamic keys used in open_key_mode
            for uk, cnt in self.api_key_usage.items():
                if uk not in seen and uk != "web-ui":
                    out.append({"key": uk, "label": "Open Mode Key", "created_at": "Dynamic", "calls": cnt})
            return out

    def _load_tmp(self) -> None:
        if not os.path.exists(self.tmp_path):
            self._flush_unlocked()
            return
        try:
            with open(self.tmp_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k, v in (data.get("metrics") or {}).items():
                    if k in self.metrics and isinstance(v, int):
                        self.metrics[k] = v
                if isinstance(data.get("visitors"), dict):
                    self.visitors = data["visitors"]
                if isinstance(data.get("recent_searches"), list):
                    self.recent_searches = data["recent_searches"][:500]
                if isinstance(data.get("live_tg_alerts"), bool):
                    self.live_tg_alerts = data["live_tg_alerts"]
                if isinstance(data.get("open_key_mode"), bool):
                    self.open_key_mode = data["open_key_mode"]
                if isinstance(data.get("custom_api_keys"), dict):
                    self.custom_api_keys = data["custom_api_keys"]
                if isinstance(data.get("revoked_keys"), list):
                    self.revoked_keys = set(data["revoked_keys"])
                if isinstance(data.get("api_key_usage"), dict):
                    self.api_key_usage = {str(k): int(v) for k, v in data["api_key_usage"].items()}
        except Exception as exc:
            logger.debug("Temp load note: %s", exc)

    def _flush_unlocked(self) -> None:
        payload = {
            "updated_at": now_ist_str(),
            "live_tg_alerts": self.live_tg_alerts,
            "open_key_mode": self.open_key_mode,
            "custom_api_keys": self.custom_api_keys,
            "revoked_keys": sorted(self.revoked_keys),
            "api_key_usage": self.api_key_usage,
            "metrics": self.metrics,
            "visitors": self.visitors,
            "recent_searches": self.recent_searches[:500],
        }
        try:
            tmp_dir = os.path.dirname(self.tmp_path) or tempfile.gettempdir()
            fd, swap = tempfile.mkstemp(prefix="web_adm_", suffix=".tmp", dir=tmp_dir)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(swap, self.tmp_path)
        except Exception as exc:
            logger.debug("Temp flush note: %s", exc)

    @staticmethod
    def visitor_id_from_request(client_ip: str, visitor_tag: str = "", user_agent: str = "") -> Tuple[str, str]:
        tag = (visitor_tag or "").strip()[:36]
        raw = tag or f"{client_ip}|{user_agent[:60]}"
        short_id = "vis_" + hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:10]
        label = tag if (tag and not tag.startswith("v_")) else f"Visitor {short_id[-6:].upper()}"
        return short_id, label

    def record_search(
        self,
        client_ip: str,
        user_agent: str,
        visitor_tag: str,
        app_code: str,
        action_label: str,
        query_text: str,
        status_text: str,
        source: str = "Web UI",
        api_key: str = "",
    ) -> Dict[str, Any]:
        ts = now_ist_str()
        vid, vlabel = self.visitor_id_from_request(client_ip, visitor_tag, user_agent)
        app_norm = app_code.lower().strip()
        app_disp = {"ig": "📸 Instagram", "snap": "👻 Snapchat", "tg": "✈️ Telegram"}.get(app_norm, app_code)

        entry = {
            "time": ts,
            "visitor_id": vid,
            "visitor_label": vlabel,
            "ip_masked": self._mask_ip(client_ip),
            "user_agent": (user_agent or "Unknown")[:90],
            "source": source if not api_key else f"API ({api_key})",
            "api_key": api_key or "web-ui",
            "app_code": app_norm,
            "app": app_disp,
            "action": action_label,
            "query": query_text[:250],
            "status": status_text[:180],
        }

        with self._lock:
            self.metrics["total_searches"] += 1
            if source == "REST API" or api_key:
                self.metrics["api_calls"] += 1
            if api_key:
                self.api_key_usage[api_key] = self.api_key_usage.get(api_key, 0) + 1
            if app_norm in ("ig", "snap", "tg"):
                self.metrics[f"{app_norm}_searches"] += 1

            vis = self.visitors.get(vid)
            if not vis:
                vis = {
                    "visitor_id": vid,
                    "label": vlabel,
                    "ip_masked": entry["ip_masked"],
                    "user_agent": entry["user_agent"],
                    "first_seen": ts,
                    "last_seen": ts,
                    "total_searches": 0,
                    "ig_count": 0,
                    "snap_count": 0,
                    "tg_count": 0,
                    "searches": [],
                }
                self.visitors[vid] = vis

            vis["last_seen"] = ts
            vis["ip_masked"] = entry["ip_masked"]
            vis["total_searches"] = int(vis.get("total_searches", 0)) + 1
            if app_norm in ("ig", "snap", "tg"):
                vis[f"{app_norm}_count"] = int(vis.get(f"{app_norm}_count", 0)) + 1

            v_list = vis.setdefault("searches", [])
            v_list.insert(0, entry)
            del v_list[200:]

            self.recent_searches.insert(0, entry)
            del self.recent_searches[500:]

            total_for_vis = vis["total_searches"]
            self._flush_unlocked()

        if self.live_tg_alerts and self.bot_token:
            threading.Thread(
                target=self._send_tg_admin_alert,
                args=(entry, total_for_vis),
                daemon=True,
            ).start()

        return entry

    @staticmethod
    def _mask_ip(ip: str) -> str:
        ip = (ip or "unknown").strip()
        if "." in ip:
            parts = ip.split(".")
            if len(parts) == 4:
                return f"{parts[0]}.{parts[1]}.x.{parts[3]}"
        return ip[:24]

    def _send_tg_admin_alert(self, entry: Dict[str, Any], vis_total: int) -> None:
        msg = (
            "🌐 <b>Live Website / API Search Alert!</b>\n\n"
            f"👤 <b>Visitor:</b> <code>{html.escape(entry['visitor_id'])}</code> ({html.escape(entry['ip_masked'])})\n"
            f"🔑 <b>Source / Key:</b> <code>{html.escape(entry['source'])}</code>\n"
            f"📱 <b>Platform:</b> {entry['app']} — <b>{html.escape(entry['action'])}</b>\n"
            f"🔍 <b>Query:</b> <code>{html.escape(entry['query'][:160])}</code>\n"
            f"📊 <b>Status:</b> {html.escape(entry['status'])}\n"
            f"🕒 <b>Time (IST):</b> {html.escape(entry['time'])}\n"
            f"🔢 <b>Visitor Searches:</b> {vis_total}"
        )
        for aid in list(self.admin_ids):
            try:
                requests.post(
                    f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                    json={
                        "chat_id": aid,
                        "text": msg,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    timeout=5,
                )
            except Exception:
                pass

    def bump_error(self) -> None:
        with self._lock:
            self.metrics["errors_caught"] += 1
            self._flush_unlocked()

    def toggle_alerts(self) -> bool:
        with self._lock:
            self.live_tg_alerts = not self.live_tg_alerts
            self._flush_unlocked()
            return self.live_tg_alerts

    def clear_all(self) -> None:
        with self._lock:
            for k in self.metrics:
                self.metrics[k] = 0
            self.visitors.clear()
            self.recent_searches.clear()
            self.api_key_usage.clear()
            self._flush_unlocked()

    def tmp_size_kb(self) -> float:
        try:
            return round(os.path.getsize(self.tmp_path) / 1024.0, 1)
        except Exception:
            return 0.0

    def uptime_human(self) -> str:
        sec = max(0, int(time.time() - self.started_at))
        h, r = divmod(sec, 3600)
        m, s = divmod(r, 60)
        return f"{h}h {m}m {s}s" if h else (f"{m}m {s}s" if m else f"{s}s")

    def summary_dict(self) -> Dict[str, Any]:
        sorted_keys = self.list_api_keys()
        with self._lock:
            sorted_visitors = sorted(
                [dict(v) for v in self.visitors.values()],
                key=lambda x: int(x.get("total_searches", 0)),
                reverse=True,
            )
            return {
                "started_ist": self.started_ist,
                "uptime": self.uptime_human(),
                "tmp_path": self.tmp_path,
                "tmp_size_kb": self.tmp_size_kb(),
                "live_tg_alerts": self.live_tg_alerts,
                "open_key_mode": self.open_key_mode,
                "api_keys": sorted_keys,
                "tg_bot_configured": bool(self.bot_token),
                "unique_visitors": len(self.visitors),
                "metrics": dict(self.metrics),
                "visitors": sorted_visitors[:100],
                "recent_searches": list(self.recent_searches[:100]),
            }

    def export_txt_report(self, visitor_id: Optional[str] = None) -> str:
        snap = self.summary_dict()
        lines = [
            "============================================================",
            "   3-IN-1 SOCIAL DOWNLOADER WEB & API — TEMP REPORT (.TXT)",
            f"   Generated (IST): {now_ist_str()}",
            f"   Temp Store File: {snap['tmp_path']} ({snap['tmp_size_kb']} KB)",
            "============================================================",
            "",
        ]
        if visitor_id:
            v = next((x for x in snap["visitors"] if x["visitor_id"] == visitor_id), None)
            if not v:
                lines.append(f"Visitor {visitor_id} not found.")
            else:
                lines.extend(
                    [
                        f"Visitor ID    : {v['visitor_id']}",
                        f"Masked IP     : {v['ip_masked']}",
                        f"User-Agent    : {v['user_agent']}",
                        f"First Seen    : {v['first_seen']}",
                        f"Last Seen     : {v['last_seen']}",
                        f"Total Searches: {v['total_searches']} (IG: {v['ig_count']}, Snap: {v['snap_count']}, TG: {v['tg_count']})",
                        "",
                        "SEARCH HISTORY:",
                    ]
                )
                for idx, s in enumerate(v.get("searches") or [], 1):
                    lines.append(
                        f"  {idx:02d}. [{s['time']}] ({s['source']}) {s['app']} | {s['action']} | {s['query']} -> {s['status']}"
                    )
        else:
            m = snap["metrics"]
            lines.extend(
                [
                    f"Uptime          : {snap['uptime']} (Since {snap['started_ist']})",
                    f"Unique Visitors : {snap['unique_visitors']}",
                    f"Total Searches  : {m['total_searches']} (API Calls: {m['api_calls']})",
                    f"  - Instagram   : {m['ig_searches']}",
                    f"  - Snapchat    : {m['snap_searches']}",
                    f"  - Telegram    : {m['tg_searches']}",
                    f"Errors Caught   : {m['errors_caught']}",
                    "",
                    "API KEYS USAGE:",
                    "------------------------------------------------------------",
                ]
            )
            for ak in snap["api_keys"]:
                lines.append(f"  Key: {ak['key']} ({ak['label']}) -> {ak['calls']} calls")
            lines.extend(
                [
                    "",
                    "VISITORS & SEARCH HISTORIES:",
                    "------------------------------------------------------------",
                ]
            )
            for v in snap["visitors"]:
                lines.append(
                    f"\n[{v['visitor_id']}] IP={v['ip_masked']} | Searches={v['total_searches']} | Last={v['last_seen']}"
                )
                for idx, s in enumerate((v.get("searches") or [])[:40], 1):
                    lines.append(
                        f"   {idx:02d}. [{s['time']}] ({s['source']}) {s['app']} - {s['action']}: {s['query']} [{s['status']}]"
                    )
        return "\n".join(lines) + "\n"
