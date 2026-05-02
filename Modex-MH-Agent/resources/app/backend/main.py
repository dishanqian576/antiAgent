"""MH Agent Web Backend — FastAPI 入口"""
from __future__ import annotations
import asyncio
import logging
import sys
from contextlib import asynccontextmanager

# Windows 上必须使用 ProactorEventLoop 才能支持 asyncio.create_subprocess_exec
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from config import IS_DESKTOP, FRONTEND_DIST, API_PORT
from services.state_store import init_db
from services.workflow_engine import set_broadcast
from routers import workflows, artifacts, checkpoints, ws, settings, editor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # 注入 WebSocket 广播函数到 workflow_engine
    set_broadcast(ws.manager.broadcast)

    # 自动恢复被后端重启中断的工作流
    from services.state_store import get_workflows_to_resume
    from services.workflow_engine import run_workflow
    from routers.workflows import _tasks
    resume_ids = get_workflows_to_resume()
    for wf_id in resume_ids:
        logging.getLogger(__name__).info("Auto-resuming workflow %s after restart", wf_id)
        task = asyncio.create_task(run_workflow(wf_id))
        _tasks[wf_id] = task  # 注册到 _tasks 防止心跳检测重复触发

    # 启动心跳检测（每 60 秒检查僵尸工作流并自动恢复）
    from routers.workflows import start_heartbeat
    start_heartbeat()

    yield


app = FastAPI(title="MH Agent Web", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(workflows.router)
app.include_router(artifacts.router)
app.include_router(checkpoints.router)
app.include_router(settings.router)
app.include_router(editor.router)
app.include_router(ws.router)


# ============================================================
# 激活码验证（后端）
# ============================================================
import json
import hashlib
import time
import http.client
import ssl
from pathlib import Path
from urllib.parse import urlparse
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# 激活状态文件
if IS_DESKTOP:
    import os, platform
    if platform.system() == "Windows":
        _LICENSE_DIR = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "MHAgent"
    else:
        _LICENSE_DIR = Path.home() / ".mhagent"
else:
    _LICENSE_DIR = Path(__file__).resolve().parent
_LICENSE_FILE = _LICENSE_DIR / "license.json"


def _check_license_local() -> bool:
    """检查本地激活状态文件。每次启动都联网校验。"""
    if not _LICENSE_FILE.exists():
        return False
    try:
        data = json.loads(_LICENSE_FILE.read_text(encoding="utf-8"))
        # 验证签名
        payload = f"{data['license_key']}:{data['machine_id']}:{data['timestamp']}"
        expected_sig = hashlib.sha256(payload.encode()).hexdigest()[:16]
        if data.get("sig") != expected_sig:
            return False

        # 每次启动都联网校验
        try:
            renewed = _online_renew(data["license_key"], data["machine_id"])
            if renewed:
                return True
            else:
                # 服务端拒绝 → 删除本地 license
                _LICENSE_FILE.unlink(missing_ok=True)
                return False
        except Exception:
            # 网络不通 → 不允许离线使用（必须联网验证）
            return False

    except Exception:
        return False


def _online_renew(license_key: str, machine_id: str) -> bool:
    """联网续期：调用服务端验证激活码，获取解密密钥。"""
    try:
        url = "https://chba3zuuw6.sealoshzh.site/jihuoma"
        parsed = urlparse(url)
        payload = json.dumps({
            "license_key": license_key,
            "machine_id": machine_id,
            "timestamp": int(time.time() * 1000),
            "action": "renew",
            "need_dk": True,  # 请求下发解密密钥
        })
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection(parsed.hostname, 443, timeout=10, context=ctx)
        conn.request("POST", parsed.path, payload, {"Content-Type": "application/json"})
        res = conn.getresponse()
        body = json.loads(res.read().decode("utf-8"))
        conn.close()

        if body.get("valid"):
            # 续期成功，更新本地时间戳
            _save_license_local(license_key, machine_id)
            # 解密并缓存 skills 解密密钥
            dk_encrypted = body.get("dk")
            if dk_encrypted:
                try:
                    from services.skill_crypto import decrypt_dk_from_transport, set_decrypt_key
                    dk_hex = decrypt_dk_from_transport(dk_encrypted, license_key)
                    set_decrypt_key(dk_hex)
                except Exception:
                    pass  # dk 解密失败不影响基本功能
            return True
        return False
    except Exception:
        return False


def _save_license_local(license_key: str, machine_id: str):
    """保存激活状态到本地。"""
    _LICENSE_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    payload = f"{license_key}:{machine_id}:{ts}"
    sig = hashlib.sha256(payload.encode()).hexdigest()[:16]
    data = {
        "license_key": license_key,
        "machine_id": machine_id,
        "timestamp": ts,
        "sig": sig,
        "last_online_verify": ts,
    }
    _LICENSE_FILE.write_text(json.dumps(data), encoding="utf-8")


class LicenseVerifyRequest(BaseModel):
    license_key: str
    machine_id: str


@app.post("/api/license/verify")
async def license_verify(req: LicenseVerifyRequest):
    """转发激活码验证到外部服务器，成功后保存到本地并获取解密密钥。"""
    try:
        url = "https://chba3zuuw6.sealoshzh.site/jihuoma"
        parsed = urlparse(url)
        payload = json.dumps({
            "license_key": req.license_key,
            "machine_id": req.machine_id,
            "timestamp": int(time.time() * 1000),
            "need_dk": True,
        })
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection(parsed.hostname, 443, timeout=15, context=ctx)
        conn.request("POST", parsed.path, payload, {"Content-Type": "application/json"})
        res = conn.getresponse()
        body = json.loads(res.read().decode("utf-8"))
        conn.close()

        if body.get("valid"):
            _save_license_local(req.license_key, req.machine_id)
            global _license_verified_this_session
            _license_verified_this_session = True
            # 解密并缓存 skills 解密密钥
            dk_encrypted = body.get("dk")
            if dk_encrypted:
                try:
                    from services.skill_crypto import decrypt_dk_from_transport, set_decrypt_key
                    dk_hex = decrypt_dk_from_transport(dk_encrypted, req.license_key)
                    set_decrypt_key(dk_hex)
                except Exception:
                    pass
            return {"valid": True, "message": body.get("message", "激活成功")}
        else:
            return {"valid": False, "message": body.get("message", "验证失败")}
    except Exception as e:
        return {"valid": False, "message": f"网络错误: {str(e)}"}


@app.get("/api/license/status")
async def license_status():
    """检查本地激活状态。"""
    return {"licensed": _check_license_local()}


# 激活码中间件：启动时验证一次，之后用内存缓存
_LICENSE_WHITELIST = {"/api/license/verify", "/api/license/status", "/api/health", "/ws"}
_license_verified_this_session = False  # 本次启动是否已验证通过

@app.middleware("http")
async def license_middleware(request: Request, call_next):
    global _license_verified_this_session
    path = request.url.path
    # 放行白名单、静态资源、WebSocket
    if any(path.startswith(w) for w in _LICENSE_WHITELIST) or not path.startswith("/api/"):
        return await call_next(request)
    # 本次启动已验证过 → 直接放行
    if _license_verified_this_session:
        return await call_next(request)
    # 首次请求：检查激活状态
    if _check_license_local():
        _license_verified_this_session = True
        return await call_next(request)
    return JSONResponse(status_code=403, content={"detail": "未激活，请先输入激活码"})


@app.get("/api/health")
async def health():
    return {"status": "ok", "desktop": IS_DESKTOP}


@app.get("/api/templates")
async def get_templates():
    """返回可用的工作流模板"""
    from services.workflow_engine import TEMPLATES
    result = {}
    for key, tmpl in TEMPLATES.items():
        result[key] = {
            "name": tmpl.display_name,
            "pipeline_skill": tmpl.pipeline_skill,
            "steps": [
                {"skill_name": s.skill_name, "display_name": s.display_name,
                 "has_checkpoint": s.has_checkpoint, "checkpoint_type": s.checkpoint_type}
                for s in tmpl.sub_steps
            ],
        }
    return result


# --- 桌面模式：托管前端静态文件 ---
if IS_DESKTOP and FRONTEND_DIST.is_dir():
    # 静态资源（js/css/images）
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="static-assets")

    # logo 等 public 文件
    @app.get("/logo.svg")
    async def serve_logo():
        logo = FRONTEND_DIST / "logo.svg"
        if logo.exists():
            return FileResponse(str(logo), media_type="image/svg+xml")

    # SPA fallback：所有非 /api /ws 路径返回 index.html
    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        # 先检查是否是静态文件
        static_file = FRONTEND_DIST / full_path
        if static_file.is_file() and not full_path.startswith("api") and not full_path.startswith("ws"):
            return FileResponse(str(static_file))
        # 否则返回 index.html（SPA 路由）
        return FileResponse(str(FRONTEND_DIST / "index.html"))
