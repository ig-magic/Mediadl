"""
Vercel Serverless + FastAPI Backend (`api/index.py`)
====================================================
Powers:
  1. The 3-in-1 Social HD Downloader Web App (`/` -> `public/index.html`)
  2. Public Developer REST API (`/api/v1/*`) + Swagger `/docs` & `/redoc`
  3. CORS-Safe Media Streaming & 1-Click Force-Download Proxy (`/api/v1/proxy`)
  4. Zero-DB `/tmp` Temp-File Admin Panel + Real-Time Telegram Admin Search Alerts
"""

from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
import hashlib
import os
import re
import urllib.parse
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel
import requests

# Load local .env if present (and also check ../insta_dp_bot/.env in workspace so TG alerts work out of the box!)
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
load_dotenv(os.path.join(_ROOT, ".env"))
_BOT_ENV = os.path.abspath(os.path.join(_ROOT, "..", "insta_dp_bot", ".env"))
if os.path.exists(_BOT_ENV):
    load_dotenv(_BOT_ENV, override=False)

from ._services.instagram_service import (  # noqa: E402
    InstagramFetchError,
    InstagramLoginError,
    InstagramService,
    InvalidUsernameError,
    MediaItem,
    ProfileNotFoundError,
    TwoFactorRequiredError,
    classify_instagram_input,
)
from ._services.snapchat_service import (  # noqa: E402
    SnapchatError,
    SnapchatService,
    parse_snapchat_input,
)
from ._services.telegram_service import (  # noqa: E402
    TelegramFetchError,
    TelegramMediaService,
    classify_telegram_input,
)
from ._services.web_admin_store import WebAdminTempStore  # noqa: E402

app = FastAPI(
    title="3-in-1 Social HD Downloader API",
    description=(
        "Production REST API for downloading HD Profile Pictures (including Private IG & Dual Front/Background Snapchat DPs), "
        "Active Stories, Highlights, Carousels, Reels, Spotlights, and Public Telegram Channel Media."
    ),
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

ig_service = InstagramService()
snap_service = SnapchatService()
tg_service = TelegramMediaService()
web_store = WebAdminTempStore()

# Short-lived in-memory blob ring buffer (speeds up already-fetched avatars & MTProto story bytes)
_BLOB_CACHE: "OrderedDict[str, tuple[bytes, str]]" = OrderedDict()
_MAX_BLOBS = 32

ALLOWED_PROXY_HOSTS = (
    "cdninstagram.com",
    "fbcdn.net",
    "akamaihd.net",
    "instagram.com",
    "socialhubapi.com",
    "rapidcdn.app",
    "sc-cdn.net",
    "snapchat.com",
    "telesco.pe",
    "telegram.org",
    "cdn-telegram.org",
    "clipssaver.com",
    "storynavigation.com",
)


def _store_blob(data: bytes, mime: str = "image/jpeg") -> str:
    if not data:
        return ""
    if data[:8].startswith(b"\x89PNG"):
        mime = "image/png"
    elif len(data) > 12 and data[4:8] == b"ftyp":
        mime = "video/mp4"
    key = hashlib.sha1(data[:4096] + str(len(data)).encode()).hexdigest()[:16]
    _BLOB_CACHE[key] = (data, mime)
    _BLOB_CACHE.move_to_end(key)
    while len(_BLOB_CACHE) > _MAX_BLOBS:
        _BLOB_CACHE.popitem(last=False)
    return f"/api/v1/blob/{key}"


def _proxy_url(raw_url: str, filename: str = "media", dl: bool = False) -> str:
    if not raw_url:
        return ""
    enc = urllib.parse.quote(raw_url, safe="")
    fn = urllib.parse.quote(re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:60])
    return f"/api/v1/proxy?url={enc}&filename={fn}" + ("&dl=1" if dl else "")


def _serialize_media_item(m: MediaItem, prefix: str = "media", idx: int = 1) -> Dict[str, Any]:
    ext = "mp4" if m.is_video else "jpg"
    fname = f"{prefix}_{idx}.{ext}"
    return {
        "index": idx,
        "is_video": bool(m.is_video),
        "type": "video" if m.is_video else "image",
        "raw_url": m.media_url,
        "view_url": _proxy_url(m.media_url, fname, dl=False),
        "download_url": _proxy_url(m.media_url, fname, dl=True),
        "thumbnail_url": _proxy_url(m.thumbnail_url, f"{prefix}_{idx}_thumb.jpg", dl=False) if m.thumbnail_url else "",
        "shortcode": m.shortcode or "",
        "taken_at": m.taken_at,
    }


def _req_meta(request: Request) -> tuple[str, str, str, str]:
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "unknown")
    ua = request.headers.get("user-agent", "")
    vis = request.headers.get("x-visitor-id", "")
    src = "Web UI" if request.headers.get("x-ui-client") == "1" else "REST API"
    return ip, ua, vis, src


def _log_req(request: Request, app_code: str, action: str, query_text: str, status: str) -> None:
    if getattr(request.state, "skip_inner_log", False):
        return
    ip, ua, vis, src = _req_meta(request)
    api_k = getattr(request.state, "path_api_key", "")
    web_store.record_search(
        client_ip=ip,
        user_agent=ua,
        visitor_tag=vis,
        app_code=app_code,
        action_label=action,
        query_text=query_text,
        status_text=status,
        source=src,
        api_key=api_k,
    )


# ===========================================================================
# Global Error Handling (Zero Unhandled Crashes!)
# ===========================================================================

@app.exception_handler(ProfileNotFoundError)
async def _not_found_handler(request: Request, exc: ProfileNotFoundError) -> JSONResponse:
    web_store.bump_error()
    return JSONResponse(
        status_code=404,
        content={"ok": False, "error": str(exc), "suggestions": getattr(exc, "suggestions", [])},
    )


@app.exception_handler(InstagramFetchError)
async def _domain_err_handler(request: Request, exc: InstagramFetchError) -> JSONResponse:
    web_store.bump_error()
    return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})


@app.exception_handler(Exception)
async def _unexpected_err_handler(request: Request, exc: Exception) -> JSONResponse:
    web_store.bump_error()
    return JSONResponse(
        status_code=500,
        content={"ok": False, "error": f"Request could not be completed: {str(exc)[:240]}"},
    )


# ===========================================================================
# Frontend Entry (`/` -> `public/index.html`) & Health Check
# ===========================================================================

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def serve_spa() -> Response:
    html_path = os.path.join(_ROOT, "public", "index.html")
    if os.path.exists(html_path):
        return FileResponse(html_path, media_type="text/html")
    return HTMLResponse("<h1>3-in-1 Social Downloader API is running.</h1>")


@app.get("/api/v1/health", tags=["System"])
async def api_health() -> Dict[str, Any]:
    return {
        "ok": True,
        "service": "3-in-1 Social HD Downloader Web & API",
        "version": "2.0.0",
        "uptime": web_store.uptime_human(),
        "platforms": {
            "instagram": {
                "ready": True,
                "private_dp_zero_login": True,
                "session_connected": bool(ig_service.session_id),
                "logged_in_user": ig_service.logged_in_user,
            },
            "snapchat": {"ready": True, "dual_front_and_bg_dp": True, "zero_login": True},
            "telegram": {
                "ready": True,
                "public_dp_and_channels": True,
                "stories_mtproto_connected": tg_service.has_story_mtproto,
                "mtproto_account": tg_service.logged_in_label,
            },
        },
    }


# ===========================================================================
# CORS-Safe Media Streaming & Force-Download Proxy
# ===========================================================================

@app.get("/api/v1/blob/{blob_id}", include_in_schema=False)
async def serve_cached_blob(
    blob_id: str,
    dl: int = Query(0),
    filename: str = Query("download"),
    fallback: str = Query(""),
) -> Response:
    item = _BLOB_CACHE.get(blob_id)
    if item:
        data, mime = item
        ext = "png" if "png" in mime else ("mp4" if "mp4" in mime else "jpg")
        clean_fn = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:60]
        if not clean_fn.endswith(f".{ext}"):
            clean_fn = f"{clean_fn}.{ext}"
        headers = {"Cache-Control": "public, max-age=3600"}
        if dl:
            headers["Content-Disposition"] = f'attachment; filename="{clean_fn}"'
        return Response(content=data, media_type=mime, headers=headers)

    if fallback:
        return await proxy_media(url=fallback, dl=dl, filename=filename)
    raise HTTPException(status_code=404, detail="Media cache expired; please search again.")


@app.get("/api/v1/proxy", tags=["Media Proxy"])
async def proxy_media(
    url: str = Query(..., description="Encoded upstream CDN media URL"),
    dl: int = Query(0, description="Set dl=1 to force browser file download"),
    filename: str = Query("social_hd_media", description="Suggested download filename"),
) -> Response:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not any(
        host == h or host.endswith("." + h) for h in ALLOWED_PROXY_HOSTS
    ):
        raise HTTPException(status_code=400, detail="Unsupported media host for proxy.")

    referer = "https://www.instagram.com/"
    if "sc-cdn.net" in host or "snapchat.com" in host:
        referer = "https://www.snapchat.com/"
    elif "telesco.pe" in host or "telegram.org" in host:
        referer = "https://t.me/"
    elif "socialhubapi.com" in host or "clipssaver.com" in host:
        referer = "https://clipssaver.com/"

    def _fetch_stream(target_u: str, ref: str):
        for _ in range(6):
            target = urllib.parse.urlparse(target_u)
            target_host = (target.hostname or "").lower()
            if target.scheme not in ("http", "https") or not any(
                target_host == h or target_host.endswith("." + h) for h in ALLOWED_PROXY_HOSTS
            ):
                raise HTTPException(status_code=400, detail="Unsupported media redirect host.")
            response = requests.get(
                target_u,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
                    ),
                    "Referer": ref,
                },
                stream=True,
                timeout=28,
                allow_redirects=False,
            )
            if response.status_code not in (301, 302, 303, 307, 308) or not response.headers.get("Location"):
                return response
            target_u = urllib.parse.urljoin(target_u, response.headers["Location"])
            response.close()
        raise HTTPException(status_code=502, detail="Too many media redirects.")

    upstream = await asyncio.to_thread(_fetch_stream, url, referer)
    # Fallback: if socialhubapi wrapper fails/expires, unwrap `?src=<base64>` directly to Instagram scontent CDN!
    if upstream.status_code >= 400 and "src=" in (parsed.query or ""):
        upstream.close()
        try:
            src_b64 = urllib.parse.parse_qs(parsed.query).get("src", [""])[0]
            if src_b64:
                src_b64 += "=" * (-len(src_b64) % 4)
                unwrapped = base64.urlsafe_b64decode(src_b64).decode("utf-8")
                unwrapped_url = urllib.parse.urlparse(unwrapped)
                unwrapped_host = (unwrapped_url.hostname or "").lower()
                if unwrapped_url.scheme == "https" and any(
                    unwrapped_host == h or unwrapped_host.endswith("." + h)
                    for h in ("cdninstagram.com", "fbcdn.net")
                ):
                    upstream = await asyncio.to_thread(_fetch_stream, unwrapped, "https://www.instagram.com/")
        except Exception:
            pass

    if upstream.status_code >= 400:
        upstream.close()
        raise HTTPException(status_code=502, detail=f"Upstream CDN HTTP {upstream.status_code}")

    ctype = upstream.headers.get("Content-Type") or "application/octet-stream"
    ext = "mp4" if "video" in ctype else ("png" if "png" in ctype else "jpg")
    clean_fn = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:65]
    if "." not in clean_fn[-5:]:
        clean_fn = f"{clean_fn}.{ext}"

    resp_headers = {"Cache-Control": "public, max-age=3600"}
    if upstream.headers.get("Content-Length"):
        resp_headers["Content-Length"] = upstream.headers["Content-Length"]
    if dl:
        resp_headers["Content-Disposition"] = f'attachment; filename="{clean_fn}"'

    def _iter_chunks():
        try:
            for chunk in upstream.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return StreamingResponse(_iter_chunks(), media_type=ctype, headers=resp_headers)


# ===========================================================================
# 1. INSTAGRAM REST API ENDPOINTS
# ===========================================================================

@app.get("/api/v1/instagram/profile", include_in_schema=False)
async def api_ig_profile(
    request: Request,
    username: str = Query(..., description="Instagram username or profile URL"),
) -> Dict[str, Any]:
    try:
        _, clean_u, _ = classify_instagram_input(username)
        res = await asyncio.to_thread(ig_service.get_profile_pic, clean_u)
        blob_path = _store_blob(res.image_bytes, "image/jpeg")
        fb_enc = urllib.parse.quote(res.hd_pic_url, safe="")
        view_u = f"{blob_path}?filename={res.username}_ig_hd&fallback={fb_enc}"
        dl_u = f"{blob_path}?dl=1&filename={res.username}_ig_hd&fallback={fb_enc}"
        _log_req(request, "ig", "Profile + HD DP", f"@{res.username}", "✅ Success")
        return {
            "ok": True,
            "platform": "instagram",
            "username": res.username,
            "user_id": res.user_id,
            "full_name": res.full_name,
            "biography": res.biography,
            "external_url": res.external_url,
            "category": res.category,
            "is_private": res.is_private,
            "is_verified": res.is_verified,
            "has_active_story": res.has_public_story,
            "followers_count": res.followers_count,
            "following_count": res.following_count,
            "posts_count": res.posts_count,
            "hd_profile_pic_raw": res.hd_pic_url,
            "hd_profile_pic_view": view_u,
            "hd_profile_pic_download": dl_u,
            "size_kb": round(len(res.image_bytes) / 1024.0, 1),
        }
    except Exception as exc:
        _log_req(request, "ig", "Profile + HD DP", username, f"⚠️ {exc}")
        raise


@app.get("/api/v1/instagram/stories", include_in_schema=False)
async def api_ig_stories(
    request: Request,
    username: str = Query(..., description="Instagram username or story URL"),
) -> Dict[str, Any]:
    kind, val1, val2 = classify_instagram_input(username)
    clean_u = val1 if kind in ("profile", "story") else username.strip().lstrip("@").lower()
    sid = val2 if kind == "story" else None
    items = await asyncio.to_thread(ig_service.get_user_stories, clean_u, sid)
    serialized = [_serialize_media_item(m, f"{clean_u}_ig_story", i) for i, m in enumerate(items, 1)]
    _log_req(request, "ig", "Active Stories", f"@{clean_u}", f"✅ {len(serialized)} stories")
    return {
        "ok": True,
        "platform": "instagram",
        "username": clean_u,
        "count": len(serialized),
        "items": serialized,
    }


@app.get("/api/v1/instagram/highlights", include_in_schema=False)
async def api_ig_highlights(
    request: Request,
    username: str = Query(..., description="Instagram username"),
) -> Dict[str, Any]:
    _, clean_u, _ = classify_instagram_input(username)
    trays = await asyncio.to_thread(ig_service.get_user_highlights, clean_u)
    _log_req(request, "ig", "Highlights List", f"@{clean_u}", f"✅ {len(trays)} highlights")
    return {
        "ok": True,
        "platform": "instagram",
        "username": clean_u,
        "count": len(trays),
        "highlights": [{"highlight_id": h.highlight_id, "title": h.title} for h in trays],
    }


@app.get("/api/v1/instagram/highlight-items", include_in_schema=False)
async def api_ig_highlight_items(
    request: Request,
    username: str = Query(...),
    highlight_id: str = Query(...),
) -> Dict[str, Any]:
    clean_u = username.strip().lstrip("@").lower()
    items = await asyncio.to_thread(ig_service.get_highlight_items, clean_u, highlight_id)
    serialized = [_serialize_media_item(m, f"{clean_u}_hl_{highlight_id}", i) for i, m in enumerate(items, 1)]
    _log_req(request, "ig", "Highlight Media", f"@{clean_u} ({highlight_id})", f"✅ {len(serialized)} slides")
    return {
        "ok": True,
        "platform": "instagram",
        "username": clean_u,
        "highlight_id": highlight_id,
        "count": len(serialized),
        "items": serialized,
    }


@app.get("/api/v1/instagram/posts", include_in_schema=False)
async def api_ig_recent_posts(
    request: Request,
    username: str = Query(...),
    limit: int = Query(8, ge=1, le=20),
) -> Dict[str, Any]:
    _, clean_u, _ = classify_instagram_input(username)
    posts = await asyncio.to_thread(ig_service.get_recent_posts, clean_u, limit)
    _log_req(request, "ig", "Recent Posts", f"@{clean_u}", f"✅ {len(posts)} posts")
    return {
        "ok": True,
        "platform": "instagram",
        "username": clean_u,
        "count": len(posts),
        "posts": [
            {
                "shortcode": p.shortcode,
                "url": f"https://www.instagram.com/p/{p.shortcode}/",
                "is_video": p.is_video,
                "is_carousel": p.is_carousel,
                "caption": p.caption,
                "like_count": p.like_count,
                "view_count": p.view_count,
                "taken_at": p.taken_at,
            }
            for p in posts
        ],
    }


@app.get("/api/v1/instagram/reels", include_in_schema=False)
async def api_ig_recent_reels(
    request: Request,
    username: str = Query(...),
    limit: int = Query(8, ge=1, le=20),
) -> Dict[str, Any]:
    _, clean_u, _ = classify_instagram_input(username)
    reels = await asyncio.to_thread(ig_service.get_recent_reels, clean_u, limit)
    _log_req(request, "ig", "Recent Reels", f"@{clean_u}", f"✅ {len(reels)} reels")
    return {
        "ok": True,
        "platform": "instagram",
        "username": clean_u,
        "count": len(reels),
        "reels": [
            {
                "shortcode": r.shortcode,
                "url": f"https://www.instagram.com/reel/{r.shortcode}/",
                "caption": r.caption,
                "like_count": r.like_count,
                "view_count": r.view_count,
                "taken_at": r.taken_at,
            }
            for r in reels
        ],
    }


@app.get("/api/v1/instagram/post", include_in_schema=False)
async def api_ig_download_post(
    request: Request,
    url: str = Query(..., description="Instagram Post/Reel URL or shortcode"),
) -> Dict[str, Any]:
    kind, val1, _ = classify_instagram_input(url)
    target = val1 if kind == "post" else url.strip()
    post = await asyncio.to_thread(ig_service.download_post_or_reel, target)
    serialized = [
        _serialize_media_item(m, f"{post.owner_username or 'ig'}_{post.shortcode}", i)
        for i, m in enumerate(post.items, 1)
    ]
    _log_req(request, "ig", "Post / Reel Download", target, f"✅ {len(serialized)} slide(s)")
    return {
        "ok": True,
        "platform": "instagram",
        "shortcode": post.shortcode,
        "owner_username": post.owner_username,
        "owner_full_name": post.owner_full_name,
        "caption": post.caption,
        "like_count": post.like_count,
        "comment_count": post.comment_count,
        "view_count": post.view_count,
        "taken_at": post.taken_at,
        "count": len(serialized),
        "items": serialized,
    }


class IgLoginBody(BaseModel):
    username: str = ""
    password: str = ""
    sessionid: str = ""


class Ig2faBody(BaseModel):
    code: str


@app.post("/api/v1/instagram/login", include_in_schema=False)
async def api_ig_login(body: IgLoginBody) -> Dict[str, Any]:
    if body.sessionid.strip():
        ig_service.set_session_id(body.sessionid.strip())
        return {"ok": True, "status": "connected", "username": ig_service.logged_in_user}
    if not body.username.strip() or not body.password:
        raise HTTPException(status_code=400, detail="Provide username & password (or sessionid cookie).")
    try:
        u = await asyncio.to_thread(ig_service.login_with_password, body.username.strip(), body.password)
        return {"ok": True, "status": "connected", "username": u}
    except TwoFactorRequiredError as exc:
        return {"ok": True, "status": "need_2fa", "message": str(exc)}


@app.post("/api/v1/instagram/2fa", include_in_schema=False)
async def api_ig_2fa(body: Ig2faBody) -> Dict[str, Any]:
    u = await asyncio.to_thread(ig_service.complete_2fa_login, body.code.strip())
    return {"ok": True, "status": "connected", "username": u}


@app.post("/api/v1/instagram/logout", include_in_schema=False)
async def api_ig_logout() -> Dict[str, Any]:
    ig_service.logout()
    return {"ok": True, "status": "logged_out"}


# ===========================================================================
# 2. SNAPCHAT REST API ENDPOINTS (Zero-Login + Dual Front & Background DP!)
# ===========================================================================

@app.get("/api/v1/snapchat/profile", include_in_schema=False)
async def api_snap_profile(
    request: Request,
    username: str = Query(..., description="Snapchat username or profile URL"),
) -> Dict[str, Any]:
    kind, val = parse_snapchat_input(username)
    if kind == "spotlight_url":
        return await api_snap_spotlight(request, q=val)

    prof = await asyncio.to_thread(snap_service.get_profile, val)
    front_blob = _store_blob(prof.avatar_bytes)
    fb_front = urllib.parse.quote(prof.avatar_url, safe="")
    front_obj = {
        "raw_url": prof.avatar_url,
        "view_url": f"{front_blob}?filename={prof.username}_snap_front&fallback={fb_front}",
        "download_url": f"{front_blob}?dl=1&filename={prof.username}_snap_front&fallback={fb_front}",
        "size_kb": round(len(prof.avatar_bytes) / 1024.0, 1),
    }

    bg_obj = None
    if prof.background_bytes and prof.background_url:
        bg_blob = _store_blob(prof.background_bytes)
        fb_bg = urllib.parse.quote(prof.background_url, safe="")
        bg_obj = {
            "raw_url": prof.background_url,
            "view_url": f"{bg_blob}?filename={prof.username}_snap_background&fallback={fb_bg}",
            "download_url": f"{bg_blob}?dl=1&filename={prof.username}_snap_background&fallback={fb_bg}",
            "size_kb": round(len(prof.background_bytes) / 1024.0, 1),
        }

    _log_req(
        request,
        "snap",
        "Profile + Dual HD DP",
        f"@{prof.username}",
        "✅ Dual Front+BG DP" if bg_obj else "✅ Single DP",
    )
    return {
        "ok": True,
        "platform": "snapchat",
        "username": prof.username,
        "display_name": prof.display_name,
        "bio": prof.bio,
        "subscribers": prof.subscribers,
        "is_verified": prof.is_verified,
        "is_public_creator": prof.is_public_creator,
        "has_bitmoji_3d": prof.has_bitmoji_3d,
        "address": prof.address,
        "website_url": prof.website_url,
        "front_profile_pic": front_obj,
        "background_profile_pic": bg_obj,
        "snapcode_view_url": _proxy_url(prof.snapcode_url, f"{prof.username}_snapcode.png", dl=False),
        "snapcode_download_url": _proxy_url(prof.snapcode_url, f"{prof.username}_snapcode.png", dl=True),
        "stories_count": len(prof.stories),
        "highlights_count": len(prof.highlights),
        "spotlights_count": len(prof.spotlights),
        "highlights_preview": [
            {"index": h.index, "highlight_id": h.highlight_id, "title": h.title, "snaps_count": len(h.snaps)}
            for h in prof.highlights
        ],
    }


@app.get("/api/v1/snapchat/stories", include_in_schema=False)
async def api_snap_stories(
    request: Request,
    username: str = Query(..., description="Snapchat username"),
) -> Dict[str, Any]:
    _, val = parse_snapchat_input(username)
    prof = await asyncio.to_thread(snap_service.get_profile, val)
    items = [_serialize_media_item(m, f"snap_{prof.username}_story", i) for i, m in enumerate(prof.stories, 1)]
    _log_req(request, "snap", "Snap Stories", f"@{prof.username}", f"✅ {len(items)} snaps")
    return {
        "ok": True,
        "platform": "snapchat",
        "username": prof.username,
        "count": len(items),
        "items": items,
    }


@app.get("/api/v1/snapchat/highlights", include_in_schema=False)
async def api_snap_highlights(
    request: Request,
    username: str = Query(..., description="Snapchat username"),
    index: Optional[int] = Query(None, description="Optional specific highlight index to expand"),
) -> Dict[str, Any]:
    _, val = parse_snapchat_input(username)
    prof = await asyncio.to_thread(snap_service.get_profile, val)
    if index is not None:
        if index < 0 or index >= len(prof.highlights):
            raise HTTPException(status_code=404, detail="Highlight index out of range.")
        hl = prof.highlights[index]
        snaps = [_serialize_media_item(m, f"snap_{prof.username}_hl{index}", i) for i, m in enumerate(hl.snaps, 1)]
        _log_req(request, "snap", f"Snap Highlight ({hl.title})", f"@{prof.username}", f"✅ {len(snaps)} snaps")
        return {
            "ok": True,
            "platform": "snapchat",
            "username": prof.username,
            "highlight": {"index": hl.index, "title": hl.title, "count": len(snaps), "items": snaps},
        }

    _log_req(request, "snap", "Snap Highlights List", f"@{prof.username}", f"✅ {len(prof.highlights)} highlights")
    return {
        "ok": True,
        "platform": "snapchat",
        "username": prof.username,
        "count": len(prof.highlights),
        "highlights": [
            {
                "index": h.index,
                "highlight_id": h.highlight_id,
                "title": h.title,
                "count": len(h.snaps),
                "cover_url": _proxy_url(h.snaps[0].thumbnail_url or h.snaps[0].media_url, f"hl_{h.index}.jpg")
                if h.snaps
                else "",
            }
            for h in prof.highlights
        ],
    }


@app.get("/api/v1/snapchat/spotlight", include_in_schema=False)
async def api_snap_spotlight(
    request: Request,
    q: str = Query(..., description="Snapchat Spotlight URL or username"),
) -> Dict[str, Any]:
    kind, val = parse_snapchat_input(q)
    if kind == "spotlight_url":
        title, raw_items = await asyncio.to_thread(snap_service.download_share_or_spotlight_url, val)
        items = [_serialize_media_item(m, "snap_spotlight", i) for i, m in enumerate(raw_items, 1)]
        _log_req(request, "snap", "Spotlight Link", val, f"✅ {len(items)} video")
        return {"ok": True, "platform": "snapchat", "title": title, "count": len(items), "items": items}

    prof = await asyncio.to_thread(snap_service.get_profile, val)
    items = [_serialize_media_item(m, f"snap_{prof.username}_spotlight", i) for i, m in enumerate(prof.spotlights, 1)]
    _log_req(request, "snap", "Creator Spotlights", f"@{prof.username}", f"✅ {len(items)} videos")
    return {
        "ok": True,
        "platform": "snapchat",
        "username": prof.username,
        "title": f"@{prof.username} Spotlights",
        "count": len(items),
        "items": items,
    }


@app.get("/api/v1/snapchat/snapcode", include_in_schema=False)
async def api_snap_snapcode(
    request: Request,
    username: str = Query(..., description="Snapchat username"),
) -> Dict[str, Any]:
    _, uname = parse_snapchat_input(username)
    qr_bytes = await asyncio.to_thread(snap_service.download_snapcode_bytes, uname)
    blob = _store_blob(qr_bytes, "image/png")
    raw_u = f"https://app.snapchat.com/web/deeplink/snapcode?username={uname}&type=PNG&size=1000"
    fb = urllib.parse.quote(raw_u, safe="")
    _log_req(request, "snap", "Snapcode QR", f"@{uname}", "✅ Sent 1000px QR")
    return {
        "ok": True,
        "platform": "snapchat",
        "username": uname,
        "view_url": f"{blob}?filename={uname}_snapcode&fallback={fb}",
        "download_url": f"{blob}?dl=1&filename={uname}_snapcode&fallback={fb}",
        "raw_url": raw_u,
    }


# ===========================================================================
# 3. TELEGRAM REST API ENDPOINTS (Public DP/Channels + Optional MTProto Story)
# ===========================================================================

@app.get("/api/v1/telegram/profile", include_in_schema=False)
async def api_tg_profile(
    request: Request,
    username: str = Query(..., description="Telegram @username or t.me link"),
) -> Dict[str, Any]:
    kind, val1, val2 = classify_telegram_input(username)
    if kind == "tg_post":
        return await api_tg_post(request, url=f"https://t.me/{val1}/{val2}")

    prof = await asyncio.to_thread(tg_service.get_public_profile, val1)
    view_u = ""
    dl_u = ""
    if prof.avatar_bytes and prof.avatar_url:
        blob = _store_blob(prof.avatar_bytes, "image/jpeg")
        fb = urllib.parse.quote(prof.avatar_url, safe="")
        view_u = f"{blob}?filename={prof.username}_tg_dp&fallback={fb}"
        dl_u = f"{blob}?dl=1&filename={prof.username}_tg_dp&fallback={fb}"

    _log_req(request, "tg", "Profile + HD DP", f"@{prof.username}", "✅ Success")
    return {
        "ok": True,
        "platform": "telegram",
        "username": prof.username,
        "title": prof.title,
        "bio": prof.bio,
        "extra_stats": prof.extra_stats,
        "is_verified": prof.is_verified,
        "is_channel_or_group": prof.is_channel_or_group,
        "avatar_raw": prof.avatar_url,
        "avatar_view": view_u,
        "avatar_download": dl_u,
        "stories_mtproto_unlocked": tg_service.has_story_mtproto,
    }


@app.get("/api/v1/telegram/channel-posts", include_in_schema=False)
async def api_tg_channel_posts(
    request: Request,
    channel: str = Query(..., description="Public Telegram channel username"),
    limit: int = Query(8, ge=1, le=20),
) -> Dict[str, Any]:
    _, ch, _ = classify_telegram_input(channel)
    posts = await asyncio.to_thread(tg_service.get_recent_channel_posts, ch, limit)
    _log_req(request, "tg", "Channel Posts List", f"@{ch}", f"✅ {len(posts)} posts")
    return {
        "ok": True,
        "platform": "telegram",
        "channel": ch,
        "count": len(posts),
        "posts": [
            {
                "post_id": p["post_id"],
                "icon": p["icon"],
                "snippet": p["snippet"],
                "url": f"https://t.me/{ch}/{p['post_id']}",
            }
            for p in posts
        ],
    }


@app.get("/api/v1/telegram/post", include_in_schema=False)
async def api_tg_post(
    request: Request,
    url: str = Query(..., description="Public Telegram post URL (https://t.me/channel/123)"),
) -> Dict[str, Any]:
    kind, ch, pid = classify_telegram_input(url)
    if kind != "tg_post":
        return await api_tg_channel_posts(request, channel=ch, limit=8)
    post = await asyncio.to_thread(tg_service.download_channel_post, ch, pid)
    items = [_serialize_media_item(m, f"tg_{ch}_{pid}", i) for i, m in enumerate(post.items, 1)]
    _log_req(request, "tg", "Channel Post Media", f"{ch}/{pid}", f"✅ {len(items)} item(s)")
    return {
        "ok": True,
        "platform": "telegram",
        "channel": post.channel,
        "post_id": post.post_id,
        "caption": post.caption,
        "views": post.views,
        "count": len(items),
        "items": items,
    }


@app.get("/api/v1/telegram/stories", include_in_schema=False)
async def api_tg_stories(
    request: Request,
    username: str = Query(..., description="Telegram username or t.me/user/s/id link"),
    pinned: bool = Query(False, description="True for Pinned Highlights, False for Active 24h Stories"),
) -> Dict[str, Any]:
    kind, uname, sid = classify_telegram_input(username)
    if not tg_service.has_story_mtproto:
        _log_req(request, "tg", "TG Stories", f"@{uname}", "ℹ️ MTProto Not Connected")
        return {
            "ok": False,
            "needs_mtproto": True,
            "username": uname,
            "error": (
                "Telegram Bot/Web API hides Stories without an MTProto client session. "
                "Click 'Unlock TG Stories' to connect your free my.telegram.org API_ID & API_HASH!"
            ),
        }

    story_id_int = int(sid) if (kind == "tg_story" and sid.isdigit()) else None
    dl_list = await tg_service.fetch_stories_mtproto(uname, story_id=story_id_int, pinned=pinned, limit=10)
    out = []
    for idx, st in enumerate(dl_list, 1):
        mime = "video/mp4" if st.is_video else "image/jpeg"
        blob = _store_blob(st.data, mime)
        out.append(
            {
                "index": idx,
                "story_id": st.story_id,
                "is_video": st.is_video,
                "caption": st.caption,
                "view_url": f"{blob}?filename=tg_{uname}_story_{st.story_id}",
                "download_url": f"{blob}?dl=1&filename=tg_{uname}_story_{st.story_id}",
            }
        )
    _log_req(request, "tg", "Pinned Highlights" if pinned else "Active Stories", f"@{uname}", f"✅ {len(out)} stories")
    return {
        "ok": True,
        "platform": "telegram",
        "username": uname,
        "pinned": pinned,
        "count": len(out),
        "items": out,
    }


class TgMtStartBody(BaseModel):
    api_id: int
    api_hash: str
    phone: str


class TgMtOtpBody(BaseModel):
    code: str


class TgMt2faBody(BaseModel):
    password: str


@app.post("/api/v1/telegram/mtproto/start", include_in_schema=False)
async def api_tg_mtproto_start(body: TgMtStartBody) -> Dict[str, Any]:
    await tg_service.start_mtproto_login(body.api_id, body.api_hash.strip(), body.phone.strip())
    return {"ok": True, "status": "otp_sent"}


@app.post("/api/v1/telegram/mtproto/verify-otp", include_in_schema=False)
async def api_tg_mtproto_otp(body: TgMtOtpBody) -> Dict[str, Any]:
    status_res, label = await tg_service.verify_mtproto_otp(body.code)
    return {"ok": True, "status": status_res, "account": label}


@app.post("/api/v1/telegram/mtproto/verify-2fa", include_in_schema=False)
async def api_tg_mtproto_2fa(body: TgMt2faBody) -> Dict[str, Any]:
    label = await tg_service.verify_mtproto_2fa(body.password)
    return {"ok": True, "status": "connected", "account": label}


# ===========================================================================
# Universal Auto-Resolve Endpoint (`/api/v1/resolve`)
# ===========================================================================

@app.get("/api/v1/resolve", include_in_schema=False)
async def api_universal_resolve(
    request: Request,
    q: str = Query(..., description="Any Instagram, Snapchat, or Telegram URL or username"),
    platform: str = Query("auto", description="auto | ig | snap | tg"),
) -> Dict[str, Any]:
    low = q.strip().lower()
    plat = platform.lower().strip()
    if plat == "auto":
        if "snapchat.com/" in low or "t.snapchat.com/" in low:
            plat = "snap"
        elif "t.me/" in low or "telegram.me/" in low:
            plat = "tg"
        else:
            plat = "ig"

    if plat == "snap":
        kind, val = parse_snapchat_input(q)
        if kind == "spotlight_url":
            return await api_snap_spotlight(request, q=val)
        return await api_snap_profile(request, username=val)

    if plat == "tg":
        kind, v1, v2 = classify_telegram_input(q)
        if kind == "tg_post":
            return await api_tg_post(request, url=f"https://t.me/{v1}/{v2}")
        if kind == "tg_story":
            return await api_tg_stories(request, username=q, pinned=False)
        return await api_tg_profile(request, username=v1)

    kind, v1, _ = classify_instagram_input(q)
    if kind == "post":
        return await api_ig_download_post(request, url=v1)
    if kind == "story":
        return await api_ig_stories(request, username=q)
    return await api_ig_profile(request, username=v1)


# ===========================================================================
# 4. ADMIN PANEL API (Zero-DB `/tmp` Temp File + Export `.txt`)
# ===========================================================================

def _require_admin(request: Request, key: str = "") -> None:
    candidate = key or request.headers.get("x-admin-key", "")
    if not web_store.verify_key(candidate):
        raise HTTPException(status_code=401, detail="Invalid Admin Key. Enter your Admin ID / Key.")


@app.get("/api/v1/admin/summary", include_in_schema=False)
async def api_admin_summary(request: Request, key: str = Query("")) -> Dict[str, Any]:
    _require_admin(request, key)
    data = web_store.summary_dict()
    data["ok"] = True
    data["ig_connected"] = ig_service.logged_in_user if ig_service.session_id else None
    data["tg_mtproto_connected"] = tg_service.logged_in_label if tg_service.has_story_mtproto else None
    return data


@app.post("/api/v1/admin/toggle-alerts", include_in_schema=False)
async def api_admin_toggle(request: Request, key: str = Query("")) -> Dict[str, Any]:
    _require_admin(request, key)
    enabled = web_store.toggle_alerts()
    return {"ok": True, "live_tg_alerts": enabled}


@app.post("/api/v1/admin/clear", include_in_schema=False)
async def api_admin_clear(request: Request, key: str = Query("")) -> Dict[str, Any]:
    _require_admin(request, key)
    web_store.clear_all()
    return {"ok": True}


class AdminKeyBody(BaseModel):
    custom_key: str = ""
    label: str = ""


class AdminKeyDeleteBody(BaseModel):
    target_key: str


@app.post("/api/v1/admin/keys/create", include_in_schema=False)
async def api_admin_create_key(request: Request, body: AdminKeyBody, key: str = Query("")) -> Dict[str, Any]:
    _require_admin(request, key)
    created = web_store.create_api_key(body.custom_key, body.label)
    return {"ok": True, "api_key": created, "api_keys": web_store.list_api_keys()}


@app.post("/api/v1/admin/keys/delete", include_in_schema=False)
async def api_admin_delete_key(request: Request, body: AdminKeyDeleteBody, key: str = Query("")) -> Dict[str, Any]:
    _require_admin(request, key)
    web_store.delete_api_key(body.target_key)
    return {"ok": True, "api_keys": web_store.list_api_keys()}


@app.post("/api/v1/admin/keys/toggle-mode", include_in_schema=False)
async def api_admin_toggle_key_mode(request: Request, key: str = Query("")) -> Dict[str, Any]:
    _require_admin(request, key)
    mode = web_store.toggle_open_key_mode()
    return {"ok": True, "open_key_mode": mode}


@app.get("/api/v1/admin/export-txt", include_in_schema=False)
async def api_admin_export_txt(
    request: Request,
    key: str = Query(""),
    visitor_id: Optional[str] = Query(None),
) -> Response:
    _require_admin(request, key)
    content = web_store.export_txt_report(visitor_id=visitor_id)
    fname = f"visitor_{visitor_id}_report.tmp.txt" if visitor_id else "social_web_admin_report.tmp.txt"
    return PlainTextResponse(
        content=content,
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ===========================================================================
# 5. CLEAN PATH-BASED PUBLIC API (`/{key}/{platform}/{action}/{query}` & Shorthands)
#    Examples:
#      /{key}/snap/pic/{query}         | /{key}/snappic/{query}
#      /{key}/snap/info/{query}        | /{key}/snapinfo/{query}
#      /{key}/snap/story/{query}       | /{key}/snapstory/{query}
#      /{key}/snap/highlights/{query}  | /{key}/snaphighlights/{query}
#      /{key}/snap/spotlight/{query}   | /{key}/snapspotlight/{query}
#      /{key}/snap/snapcode/{query}    | /{key}/snapcode/{query}
#      /{key}/insta/pic/{query}        | /{key}/instapic/{query}
#      /{key}/insta/info/{query}       | /{key}/instainfo/{query}
#      /{key}/insta/story/{query}      | /{key}/instastory/{query}
#      /{key}/insta/highlights/{query} | /{key}/instahighlights/{query}
#      /{key}/insta/post/{query}       | /{key}/instapost/{query}
#      /{key}/insta/reel/{query}       | /{key}/instareel/{query}
#      /{key}/tg/pic/{query}           | /{key}/tgpic/{query}
#      /{key}/tg/info/{query}          | /{key}/tginfo/{query}
#      /{key}/tg/story/{query}         | /{key}/tgstory/{query}
#      /{key}/tg/highlights/{query}    | /{key}/tghighlights/{query}
#      /{key}/tg/post/{query}          | /{key}/tgpost/{query}
# ===========================================================================

SHORTHAND_MAP: Dict[str, tuple[str, str]] = {
    # Snapchat shorthands
    "snappic": ("snap", "pic"),
    "snapdp": ("snap", "pic"),
    "snapinfo": ("snap", "info"),
    "snapprofile": ("snap", "info"),
    "snapstory": ("snap", "story"),
    "snapstories": ("snap", "story"),
    "snaphighlights": ("snap", "highlights"),
    "snaphighlight": ("snap", "highlights"),
    "snapspotlight": ("snap", "spotlight"),
    "snapspotlights": ("snap", "spotlight"),
    "snapcode": ("snap", "snapcode"),
    # Instagram shorthands
    "instapic": ("insta", "pic"),
    "instadp": ("insta", "pic"),
    "igpic": ("insta", "pic"),
    "igdp": ("insta", "pic"),
    "instainfo": ("insta", "info"),
    "instaprofile": ("insta", "info"),
    "iginfo": ("insta", "info"),
    "instastory": ("insta", "story"),
    "instastories": ("insta", "story"),
    "igstory": ("insta", "story"),
    "instahighlights": ("insta", "highlights"),
    "instahighlight": ("insta", "highlights"),
    "ighighlights": ("insta", "highlights"),
    "instapost": ("insta", "post"),
    "instaposts": ("insta", "post"),
    "igpost": ("insta", "post"),
    "instareel": ("insta", "reel"),
    "instareels": ("insta", "reel"),
    "igreel": ("insta", "reel"),
    # Telegram shorthands
    "tgpic": ("tg", "pic"),
    "tgdp": ("tg", "pic"),
    "tginfo": ("tg", "info"),
    "tgprofile": ("tg", "info"),
    "tgstory": ("tg", "story"),
    "tgstories": ("tg", "story"),
    "tghighlights": ("tg", "highlights"),
    "tghighlight": ("tg", "highlights"),
    "tgpost": ("tg", "post"),
    "tgposts": ("tg", "post"),
    "tgchannel": ("tg", "post"),
}


def _request_origin(request: Request) -> str:
    if request.headers.get("x-ui-client") == "1":
        return ""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )
    return f"{proto.split(',')[0].strip()}://{host.split(',')[0].strip()}" if host else ""


def _absolutize_urls(obj: Any, origin: str) -> Any:
    if not origin:
        return obj
    if isinstance(obj, dict):
        return {k: _absolutize_urls(v, origin) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_absolutize_urls(x, origin) for x in obj]
    if isinstance(obj, str) and obj.startswith("/api/"):
        return f"{origin}{obj}"
    return obj


def _clean_path_query(raw_q: str) -> str:
    q = (raw_q or "").strip().strip("/")
    # Fix collapsed scheme slashes when users paste full URLs inside URL paths (`https:/www.` -> `https://www.`)
    q = re.sub(r"^(https?):/+", r"\1://", q, flags=re.I)
    return q


def _check_path_api_key(request: Request, key: str) -> str:
    clean_k = (key or "").strip()
    if not web_store.validate_api_key(clean_k):
        raise HTTPException(
            status_code=401,
            detail="Invalid API key in URL path. Use 'demo' or generate an API key in the Admin Panel.",
        )
    request.state.path_api_key = clean_k
    return clean_k


def _looks_like_ig_shortcode_or_url(q: str) -> bool:
    low = q.lower()
    if any(seg in low for seg in ("/p/", "/reel/", "/reels/", "/tv/")):
        return True
    if "/" not in q and "." not in q and not q.startswith("@"):
        if "-" in q or any(c.isupper() for c in q):
            return True
    return False


async def _dispatch_clean_api(
    request: Request,
    key: str,
    platform: str,
    action: str,
    raw_query: str,
) -> Any:
    clean_key = _check_path_api_key(request, key)
    origin = _request_origin(request)
    q = _clean_path_query(raw_query)
    if not q:
        raise HTTPException(status_code=400, detail="Missing query parameter in URL path.")

    plat = platform.lower().strip()
    act = action.lower().strip()
    direct_binary = request.query_params.get("direct") == "1" or request.query_params.get("raw") == "1"

    # --------------------- SNAPCHAT ---------------------
    if plat in ("snap", "snapchat"):
        if act in ("pic", "dp", "avatar"):
            _, val = parse_snapchat_input(q)
            prof = await asyncio.to_thread(snap_service.get_profile, val)
            if direct_binary and prof.avatar_bytes:
                _log_req(request, "snap", "Snap Pic (Direct)", f"@{prof.username}", "✅ Binary HD")
                mime = "image/png" if prof.avatar_bytes[:8].startswith(b"\x89PNG") else "image/jpeg"
                return Response(content=prof.avatar_bytes, media_type=mime)

            full = await api_snap_profile(request, username=val)
            res = {
                "ok": True,
                "platform": "snapchat",
                "endpoint": "pic",
                "username": full["username"],
                "display_name": full["display_name"],
                "is_verified": full["is_verified"],
                "front_profile_pic": full["front_profile_pic"],
                "background_profile_pic": full["background_profile_pic"],
                "snapcode_view_url": full["snapcode_view_url"],
                "snapcode_download_url": full["snapcode_download_url"],
            }
            return _absolutize_urls(res, origin)

        if act in ("info", "profile"):
            res = await api_snap_profile(request, username=q)
            res["endpoint"] = "info"
            return _absolutize_urls(res, origin)

        if act in ("story", "stories"):
            res = await api_snap_stories(request, username=q)
            res["endpoint"] = "story"
            return _absolutize_urls(res, origin)

        if act in ("highlights", "highlight"):
            # Support both `/{key}/snap/highlights/{user}` AND `/{key}/snap/highlights/{user}/{index}`
            parts = [p for p in q.split("/") if p]
            if len(parts) == 2 and parts[1].isdigit() and "snapchat.com" not in q.lower():
                res = await api_snap_highlights(request, username=parts[0], index=int(parts[1]))
                return _absolutize_urls(res, origin)

            _, val = parse_snapchat_input(q)
            prof = await asyncio.to_thread(snap_service.get_profile, val)
            all_hl = []
            for h in prof.highlights:
                snaps = [_serialize_media_item(m, f"snap_{prof.username}_hl{h.index}", i) for i, m in enumerate(h.snaps, 1)]
                all_hl.append(
                    {
                        "index": h.index,
                        "highlight_id": h.highlight_id,
                        "title": h.title,
                        "count": len(snaps),
                        "single_highlight_api": f"{origin}/{clean_key}/snap/highlights/{prof.username}/{h.index}",
                        "items": snaps,
                    }
                )
            _log_req(request, "snap", "Snap Highlights", f"@{prof.username}", f"✅ {len(all_hl)} highlights")
            return _absolutize_urls(
                {
                    "ok": True,
                    "platform": "snapchat",
                    "endpoint": "highlights",
                    "username": prof.username,
                    "count": len(all_hl),
                    "highlights": all_hl,
                },
                origin,
            )

        if act in ("spotlight", "spotlights"):
            res = await api_snap_spotlight(request, q=q)
            res["endpoint"] = "spotlight"
            return _absolutize_urls(res, origin)

        if act in ("snapcode", "qr"):
            res = await api_snap_snapcode(request, username=q)
            res["endpoint"] = "snapcode"
            return _absolutize_urls(res, origin)

        raise HTTPException(
            status_code=400,
            detail=f"Unknown Snapchat action '{act}'. Valid: pic, info, story, highlights, spotlight, snapcode.",
        )

    # --------------------- INSTAGRAM ---------------------
    if plat in ("insta", "ig", "instagram"):
        if act in ("pic", "dp", "avatar"):
            _, clean_u, _ = classify_instagram_input(q)
            prof_res = await asyncio.to_thread(ig_service.get_profile_pic, clean_u)
            if direct_binary and prof_res.image_bytes:
                _log_req(request, "ig", "IG Pic (Direct)", f"@{prof_res.username}", "✅ Binary HD")
                return Response(content=prof_res.image_bytes, media_type="image/jpeg")

            blob_path = _store_blob(prof_res.image_bytes, "image/jpeg")
            fb_enc = urllib.parse.quote(prof_res.hd_pic_url, safe="")
            _log_req(request, "ig", "IG HD Pic", f"@{prof_res.username}", "✅ 1080p HD")
            return _absolutize_urls(
                {
                    "ok": True,
                    "platform": "instagram",
                    "endpoint": "pic",
                    "username": prof_res.username,
                    "full_name": prof_res.full_name,
                    "is_private": prof_res.is_private,
                    "is_verified": prof_res.is_verified,
                    "raw_url": prof_res.hd_pic_url,
                    "view_url": f"{blob_path}?filename={prof_res.username}_ig_hd&fallback={fb_enc}",
                    "download_url": f"{blob_path}?dl=1&filename={prof_res.username}_ig_hd&fallback={fb_enc}",
                    "size_kb": round(len(prof_res.image_bytes) / 1024.0, 1),
                },
                origin,
            )

        if act in ("info", "profile"):
            res = await api_ig_profile(request, username=q)
            res["endpoint"] = "info"
            return _absolutize_urls(res, origin)

        if act in ("story", "stories"):
            res = await api_ig_stories(request, username=q)
            res["endpoint"] = "story"
            return _absolutize_urls(res, origin)

        if act in ("highlights", "highlight"):
            # Support `/{key}/insta/highlights/{username}` AND `/{key}/insta/highlights/{username}/{highlight_id}`
            parts = [p for p in q.split("/") if p]
            if len(parts) == 2 and "instagram.com" not in q.lower():
                res = await api_ig_highlight_items(request, username=parts[0], highlight_id=parts[1])
                res["endpoint"] = "highlight_items"
                return _absolutize_urls(res, origin)

            res = await api_ig_highlights(request, username=q)
            res["endpoint"] = "highlights"
            uname = res["username"]
            for h in res.get("highlights", []):
                h["slides_api_url"] = f"{origin}/{clean_key}/insta/highlights/{uname}/{h['highlight_id']}"
            return _absolutize_urls(res, origin)

        if act in ("post", "posts", "carousel"):
            if _looks_like_ig_shortcode_or_url(q):
                res = await api_ig_download_post(request, url=q)
                res["endpoint"] = "post"
                return _absolutize_urls(res, origin)
            res = await api_ig_recent_posts(request, username=q, limit=8)
            for p in res.get("posts", []):
                p["download_api_url"] = f"{origin}/{clean_key}/insta/post/{p['shortcode']}"
            res["endpoint"] = "posts"
            return _absolutize_urls(res, origin)

        if act in ("reel", "reels"):
            if _looks_like_ig_shortcode_or_url(q):
                res = await api_ig_download_post(request, url=q)
                res["endpoint"] = "reel"
                return _absolutize_urls(res, origin)
            res = await api_ig_recent_reels(request, username=q, limit=8)
            for r in res.get("reels", []):
                r["download_api_url"] = f"{origin}/{clean_key}/insta/reel/{r['shortcode']}"
            res["endpoint"] = "reels"
            return _absolutize_urls(res, origin)

        raise HTTPException(
            status_code=400,
            detail=f"Unknown Instagram action '{act}'. Valid: pic, info, story, highlights, post, reel.",
        )

    # --------------------- TELEGRAM ---------------------
    if plat in ("tg", "telegram"):
        if act in ("pic", "dp", "avatar"):
            _, val1, _ = classify_telegram_input(q)
            prof = await asyncio.to_thread(tg_service.get_public_profile, val1)
            if direct_binary and prof.avatar_bytes:
                _log_req(request, "tg", "TG Pic (Direct)", f"@{prof.username}", "✅ Binary HD")
                return Response(content=prof.avatar_bytes, media_type="image/jpeg")

            view_u = ""
            dl_u = ""
            if prof.avatar_bytes and prof.avatar_url:
                blob = _store_blob(prof.avatar_bytes, "image/jpeg")
                fb = urllib.parse.quote(prof.avatar_url, safe="")
                view_u = f"{blob}?filename={prof.username}_tg_dp&fallback={fb}"
                dl_u = f"{blob}?dl=1&filename={prof.username}_tg_dp&fallback={fb}"
            _log_req(request, "tg", "TG HD Pic", f"@{prof.username}", "✅ Success")
            return _absolutize_urls(
                {
                    "ok": True,
                    "platform": "telegram",
                    "endpoint": "pic",
                    "username": prof.username,
                    "title": prof.title,
                    "is_verified": prof.is_verified,
                    "is_channel_or_group": prof.is_channel_or_group,
                    "raw_url": prof.avatar_url,
                    "view_url": view_u,
                    "download_url": dl_u,
                },
                origin,
            )

        if act in ("info", "profile"):
            res = await api_tg_profile(request, username=q)
            res["endpoint"] = "info"
            return _absolutize_urls(res, origin)

        if act in ("story", "stories"):
            res = await api_tg_stories(request, username=q, pinned=False)
            res["endpoint"] = "story"
            return _absolutize_urls(res, origin)

        if act in ("highlights", "highlight", "pinned"):
            res = await api_tg_stories(request, username=q, pinned=True)
            res["endpoint"] = "highlights"
            return _absolutize_urls(res, origin)

        if act in ("post", "posts", "channel"):
            # Support `/{key}/tg/post/telegram/459` OR `/{key}/tg/post/https://t.me/telegram/459` OR `/{key}/tg/post/telegram`
            m_short = re.match(r"^@?([A-Za-z0-9_]{3,})/(\d+)$", q)
            target_url = f"https://t.me/{m_short.group(1)}/{m_short.group(2)}" if m_short else q
            res = await api_tg_post(request, url=target_url)
            if "posts" in res:
                ch_name = res.get("channel", "")
                for p in res["posts"]:
                    p["download_api_url"] = f"{origin}/{clean_key}/tg/post/{ch_name}/{p['post_id']}"
            res["endpoint"] = "post"
            return _absolutize_urls(res, origin)

        raise HTTPException(
            status_code=400,
            detail=f"Unknown Telegram action '{act}'. Valid: pic, info, story, highlights, post.",
        )

    raise HTTPException(status_code=404, detail=f"Unknown platform '{platform}'. Use snap, insta, or tg.")


@app.get("/{key}/snap/{action}/{query:path}", tags=["1. Snapchat Clean API"])
async def clean_snap_api(request: Request, key: str, action: str, query: str) -> Any:
    """`/{key}/snap/{pic|info|story|highlights|spotlight|snapcode}/{query}`"""
    return await _dispatch_clean_api(request, key, "snap", action, query)


@app.get("/{key}/snapchat/{action}/{query:path}", include_in_schema=False)
async def clean_snapchat_alias(request: Request, key: str, action: str, query: str) -> Any:
    return await _dispatch_clean_api(request, key, "snap", action, query)


@app.get("/{key}/insta/{action}/{query:path}", tags=["2. Instagram Clean API"])
async def clean_insta_api(request: Request, key: str, action: str, query: str) -> Any:
    """`/{key}/insta/{pic|info|story|highlights|post|reel}/{query}`"""
    return await _dispatch_clean_api(request, key, "insta", action, query)


@app.get("/{key}/ig/{action}/{query:path}", include_in_schema=False)
@app.get("/{key}/instagram/{action}/{query:path}", include_in_schema=False)
async def clean_insta_aliases(request: Request, key: str, action: str, query: str) -> Any:
    return await _dispatch_clean_api(request, key, "insta", action, query)


@app.get("/{key}/tg/{action}/{query:path}", tags=["3. Telegram Clean API"])
async def clean_tg_api(request: Request, key: str, action: str, query: str) -> Any:
    """`/{key}/tg/{pic|info|story|highlights|post}/{query}`"""
    return await _dispatch_clean_api(request, key, "tg", action, query)


@app.get("/{key}/telegram/{action}/{query:path}", include_in_schema=False)
async def clean_tg_alias(request: Request, key: str, action: str, query: str) -> Any:
    return await _dispatch_clean_api(request, key, "tg", action, query)


@app.get("/{key}/{shorthand}/{query:path}", tags=["4. Ultra-Short Endpoints (snappic, instapic, tgpic...)"])
async def clean_shorthand_api(request: Request, key: str, shorthand: str, query: str) -> Any:
    """
    Combined 1-word endpoints:
    `/{key}/snappic/{query}`, `/{key}/snapinfo/{query}`, `/{key}/snapstory/{query}`, `/{key}/snaphighlights/{query}`,
    `/{key}/instapic/{query}`, `/{key}/instainfo/{query}`, `/{key}/instastory/{query}`, `/{key}/instahighlights/{query}`, `/{key}/instapost/{query}`, `/{key}/instareel/{query}`,
    `/{key}/tgpic/{query}`, `/{key}/tginfo/{query}`, `/{key}/tgstory/{query}`, `/{key}/tghighlights/{query}`, `/{key}/tgpost/{query}`
    """
    mapped = SHORTHAND_MAP.get(shorthand.lower().strip())
    if not mapped:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Unknown endpoint '/{key}/{shorthand}/...'. "
                "Use /{key}/snap|insta|tg/{pic|info|story|highlights|post}/{query} "
                "or shorthands like /{key}/snappic/{query}, /{key}/instapic/{query}, /{key}/tgpic/{query}."
            ),
        )
    plat, act = mapped
    return await _dispatch_clean_api(request, key, plat, act, query)
