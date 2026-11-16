"""Standalone authenticated HTTP-to-explicit-FTPS and vision-metadata bridge.

The HTML dashboard is a separate, portable file. No FTP or AI credentials reach it.
Never put this service directly on the public Internet without HTTPS and access control.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import ssl
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from ftplib import FTP_TLS, error_perm
from pathlib import Path
from urllib.request import Request as URLRequest, urlopen
from urllib.error import HTTPError

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

TOKEN = os.getenv('BRIDGE_TOKEN', '')
FTP_HOST = os.getenv('BLACKBOX_FTP_HOST', 'portal.blackbox.global')
FTP_PORT = int(os.getenv('BLACKBOX_FTP_PORT', '21'))
FTP_DIR = os.getenv('BLACKBOX_FTP_DIR', 'stock_footage')
FTP_USER = os.getenv('BLACKBOX_FTP_USER', '')
FTP_PASS = os.getenv('BLACKBOX_FTP_PASSWORD', '')
ENV_FTP_USER = FTP_USER
ENV_FTP_PASS = FTP_PASS
verified_account = False
AI_URL = os.getenv('AI_CHAT_COMPLETIONS_URL', 'https://api.openai.com/v1/chat/completions')
AI_KEY = os.getenv('AI_API_KEY', '')
AI_MODEL = os.getenv('AI_MODEL', 'gpt-4o-mini')
MAX_UPLOAD_BYTES = int(os.getenv('MAX_UPLOAD_BYTES', str(8 * 1024**3)))
UPLOAD_DIR = Path(os.getenv('UPLOAD_SPOOL_DIR', tempfile.gettempdir())) / 'blackbox-bridge-spool'
CORS_ORIGINS = [x.strip() for x in os.getenv('CORS_ORIGINS', 'null').split(',') if x.strip()]

app = FastAPI(title='Portable BlackBox companion bridge', docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=['GET', 'POST', 'PUT'], allow_headers=['Authorization', 'Content-Type'])
executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='ftps')
lock = threading.Lock()
jobs: dict[str, dict] = {}
upload_mutex = asyncio.Lock()
MAX_CHUNK_BYTES = 16 * 1024**2


def require_auth(req: Request) -> None:
    if len(TOKEN) < 24:
        raise HTTPException(503, 'BRIDGE_TOKEN must be configured with at least 24 characters')
    incoming = req.headers.get('Authorization', '')
    if not hmac.compare_digest(incoming, 'Bearer ' + TOKEN):
        raise HTTPException(401, 'Invalid bridge token')


def ftp_ready() -> bool:
    return bool(FTP_USER and FTP_PASS and FTP_HOST)


def change_job(job_id: str, **changes) -> None:
    with lock:
        if job_id in jobs:
            jobs[job_id].update(changes)


def snapshot_job(job_id: str) -> dict:
    with lock:
        if job_id not in jobs:
            raise HTTPException(404, 'Job not found (bridge may have restarted)')
        return dict(jobs[job_id])


def ftps_transfer(job_id: str, path: Path, filename: str, size: int) -> None:
    ftp = None
    try:
        change_job(job_id, state='transferring', message='Transferring to BlackBox FTPS')
        ftp = FTP_TLS(context=ssl.create_default_context(), timeout=45)
        ftp.connect(FTP_HOST, FTP_PORT)
        ftp.login(FTP_USER, FTP_PASS)
        ftp.prot_p()  # encrypted data channel (not merely encrypted login)
        if FTP_DIR.strip('/'):
            for segment in FTP_DIR.strip('/').split('/'):
                ftp.cwd(segment)
        sent = 0
        def progress(block: bytes) -> None:
            nonlocal sent
            sent += len(block)
            change_job(job_id, bytes_transferred=min(sent, size))
        with path.open('rb') as source:
            ftp.storbinary('STOR ' + filename, source, blocksize=256 * 1024, callback=progress)
        change_job(job_id, state='transferred', bytes_transferred=size,
                   message='FTPS transfer completed; confirm ingestion in the official portal')
    except Exception as exc:
        # Never echo credentials or FTP auth details into the browser.
        error_type = type(exc).__name__
        change_job(job_id, state='failed', message='FTPS transfer failed: ' + error_type)
    finally:
        if ftp is not None:
            try:
                ftp.quit()
            except Exception:
                ftp.close()
        path.unlink(missing_ok=True)



class AccountInput(BaseModel):
    username: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=512)


def masked_account(user: str) -> str:
    if '@' not in user:
        return user[:1] + '***'
    local, domain = user.rsplit('@', 1)
    return local[:1] + '***@' + domain


def ftps_check_credentials(user: str, password: str) -> None:
    """Check real BlackBox credentials using explicit TLS, without uploading data."""
    ftp = FTP_TLS(context=ssl.create_default_context(), timeout=25)
    try:
        ftp.connect(FTP_HOST, FTP_PORT)
        ftp.login(user, password)
        ftp.prot_p()
        if FTP_DIR.strip('/'):
            for segment in FTP_DIR.strip('/').split('/'):
                ftp.cwd(segment)
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()


def sanitized_ftps_error(exc: Exception) -> str:
    if isinstance(exc, ssl.SSLError):
        return 'FTP TLS handshake failed; insecure FTP fallback is disabled'
    if isinstance(exc, error_perm):
        return 'FTP authentication refused or configured upload directory unavailable'
    if isinstance(exc, (OSError, TimeoutError)):
        return 'Cannot establish a secure FTP connection to the configured host'
    return 'FTP verification failed (' + type(exc).__name__ + ')'


def active_ftp_jobs() -> bool:
    with lock:
        return any(job['state'] in ('queued', 'transferring') for job in jobs.values())


@app.post('/api/account/connect')
async def account_connect(req: Request, body: AccountInput):
    require_auth(req)
    global FTP_USER, FTP_PASS, verified_account
    if not FTP_HOST or not FTP_DIR:
        raise HTTPException(503, 'FTP endpoint and remote directory must be configured on the bridge')
    if active_ftp_jobs():
        raise HTTPException(409, 'Wait for current FTP transfer jobs before changing credentials')
    username = body.username.strip()
    if not username or '\n' in username or '\r' in username:
        raise HTTPException(422, 'Invalid FTP username')
    try:
        await asyncio.to_thread(ftps_check_credentials, username, body.password)
    except Exception as exc:
        raise HTTPException(502, sanitized_ftps_error(exc)) from None
    if active_ftp_jobs():
        raise HTTPException(409, 'FTP transfer started during verification; please retry')
    FTP_USER = username
    FTP_PASS = body.password
    verified_account = True
    return {'verified': True, 'protocol': 'explicit FTPS', 'host': FTP_HOST,
            'username_masked': masked_account(username), 'remote_directory': FTP_DIR}


@app.post('/api/account/verify')
async def account_verify(req: Request):
    require_auth(req)
    global verified_account
    if not ftp_ready():
        raise HTTPException(503, 'Configure BlackBox credentials first')
    try:
        await asyncio.to_thread(ftps_check_credentials, FTP_USER, FTP_PASS)
    except Exception as exc:
        verified_account = False
        raise HTTPException(502, sanitized_ftps_error(exc)) from None
    verified_account = True
    return {'verified': True, 'protocol': 'explicit FTPS', 'host': FTP_HOST,
            'username_masked': masked_account(FTP_USER), 'remote_directory': FTP_DIR}


@app.post('/api/account/disconnect')
async def account_disconnect(req: Request):
    require_auth(req)
    global FTP_USER, FTP_PASS, verified_account
    if active_ftp_jobs():
        raise HTTPException(409, 'Cannot disconnect during active FTP transfers')
    FTP_USER, FTP_PASS = ENV_FTP_USER, ENV_FTP_PASS
    verified_account = False
    return {'verified': False,
            'message': 'Temporary credentials cleared. Environment defaults remain; this does not log out of the BlackBox website.'}


class AnalyzeInput(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    batch: str = Field(default='', max_length=140)
    frames: list[str] = Field(min_length=1, max_length=4)


@app.get('/api/health')
async def health(req: Request):
    require_auth(req)
    return {'ok': True, 'ftp_configured': ftp_ready(), 'ai_configured': bool(AI_KEY) or not AI_URL.startswith('https://api.openai.com'),
            'provider': 'BlackBox Global (independent companion)', 'max_upload_bytes': MAX_UPLOAD_BYTES}


@app.post('/api/metadata')
async def metadata(req: Request, body: AnalyzeInput):
    require_auth(req)
    if AI_URL.startswith('https://api.openai.com') and not AI_KEY:
        raise HTTPException(503, 'Configure AI_API_KEY on the bridge or use an OpenAI-compatible local vision endpoint')
    if any(not frame.startswith('data:image/jpeg;base64,') or len(frame) > 1_000_000 for frame in body.frames):
        raise HTTPException(422, 'Frames must be JPEG data URLs (max 1MB each)')
    prompt = (
        'You write strictly factual commercial stock-footage metadata from the supplied video frames. '
        'Images are chronological samples of one video; do not infer events between samples. '
        'Never invent locations, named persons, brands, dates, emotions or camera movement when unsupported. '
        'Write English. Return ONLY a JSON object with fields: '
        'title (concise, <=140 chars), description (unique 30-180 chars, no numbers, no special symbols), '
        'keywords (array of 15-35 genuinely relevant unique English terms or short phrases), '
        'category (broad suggested stock-footage category), '
        'editorial_review_recommended (boolean if recognizable people, trademarks, logos, copyrighted artwork, signage or uncertain rights are visible), '
        'review_reason (short explanation or empty string). '
        'Do not claim release authorization, and do not invent editorial caption city/country. '
        f'Local file name: {body.filename}; batch context: {body.batch}. '
    )
    payload = {
        'model': AI_MODEL,
        'temperature': 0.2,
        'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': prompt}] +
                      [{'type': 'image_url', 'image_url': {'url': frame}} for frame in body.frames]}],
        'response_format': {'type': 'json_object'},
    }
    headers = {'Content-Type': 'application/json'}
    if AI_KEY:
        headers['Authorization'] = 'Bearer ' + AI_KEY
    outbound = URLRequest(AI_URL, data=json.dumps(payload).encode(), headers=headers, method='POST')
    try:
        with urlopen(outbound, timeout=120) as result:
            response = json.load(result)
        content = response['choices'][0]['message']['content']
        parsed = json.loads(content)
    except (HTTPError, OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise HTTPException(502, 'Vision provider failed: ' + type(exc).__name__) from exc
    keywords = parsed.get('keywords', [])
    if isinstance(keywords, str):
        keywords = re.split(r'[;,]', keywords)
    keywords = list(dict.fromkeys(x.strip() for x in keywords if isinstance(x, str) and x.strip()))[:49]
    return {'title': str(parsed.get('title', ''))[:200],
            'description': str(parsed.get('description', ''))[:200],
            'keywords': keywords,
            'category': str(parsed.get('category', ''))[:90],
            'editorial_review_recommended': bool(parsed.get('editorial_review_recommended', False)),
            'review_reason': str(parsed.get('review_reason', ''))[:300]}


@app.put('/api/upload/{filename:path}', status_code=202)
async def upload(req: Request, filename: str):
    require_auth(req)
    if not ftp_ready():
        raise HTTPException(503, 'FTP credentials are not configured on the bridge')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_. ()-]{0,199}', filename) or filename in ('.', '..'):
        raise HTTPException(422, 'Use a simple filename without slashes or special characters')
    if req.headers.get('Content-Length'):
        try:
            if int(req.headers['Content-Length']) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, 'File exceeds the configured upload size limit')
        except ValueError:
            raise HTTPException(400, 'Invalid Content-Length')
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    job_id = uuid.uuid4().hex
    path = UPLOAD_DIR / (job_id + '.part')
    total = 0
    try:
        with path.open('xb') as output:
            async for block in req.stream():
                total += len(block)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, 'File exceeds configured upload size limit')
                output.write(block)
        if total == 0:
            raise HTTPException(422, 'Empty uploads are not allowed')
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    with lock:
        jobs[job_id] = {'id': job_id, 'filename': filename, 'bytes_total': total,
                        'bytes_transferred': 0, 'state': 'queued', 'message': 'Queued for FTPS transfer',
                        'created': int(time.time())}
    executor.submit(ftps_transfer, job_id, path, filename, total)
    return snapshot_job(job_id)


@app.get('/api/jobs/{job_id}')
async def get_job(req: Request, job_id: str):
    require_auth(req)
    return snapshot_job(job_id)


# Chunked protocol: each request is <=16 MiB, below common CDN/proxy limits.
# Sidecar manifests recover partially received uploads across bridge restarts.
class UploadCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    size: int = Field(gt=0)


def valid_filename(filename: str) -> bool:
    return bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_. ()-]{0,199}', filename)) and filename not in ('.', '..')


def session_files(upload_id: str) -> tuple[Path, Path]:
    if not re.fullmatch(r'[a-f0-9]{32}', upload_id):
        raise HTTPException(404, 'Upload session not found')
    return UPLOAD_DIR / (upload_id + '.part'), UPLOAD_DIR / (upload_id + '.json')


def session_info(upload_id: str) -> dict:
    data, manifest = session_files(upload_id)
    if not manifest.exists() or not data.exists():
        raise HTTPException(404, 'Upload session not found')
    try:
        info = json.loads(manifest.read_text())
    except (ValueError, OSError):
        raise HTTPException(500, 'Upload session is unreadable')
    info['offset'] = data.stat().st_size
    return info


@app.post('/api/uploads', status_code=201)
async def create_chunked_upload(req: Request, body: UploadCreate):
    require_auth(req)
    if not ftp_ready():
        raise HTTPException(503, 'FTP credentials are not configured on the bridge')
    if body.size > MAX_UPLOAD_BYTES:
        raise HTTPException(413, 'File exceeds configured upload size limit')
    if not valid_filename(body.filename):
        raise HTTPException(422, 'Use a simple filename without slashes or special characters')
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    upload_id = uuid.uuid4().hex
    data, manifest = session_files(upload_id)
    with data.open('xb'):
        pass
    info = {'id': upload_id, 'filename': body.filename, 'size': body.size,
            'offset': 0, 'created': int(time.time())}
    manifest.write_text(json.dumps(info))
    return {**info, 'max_chunk_bytes': MAX_CHUNK_BYTES}


@app.get('/api/uploads/{upload_id}')
async def get_chunked_upload(req: Request, upload_id: str):
    require_auth(req)
    return session_info(upload_id)


@app.put('/api/uploads/{upload_id}/chunk')
async def append_chunk(req: Request, upload_id: str, offset: int):
    require_auth(req)
    if offset < 0:
        raise HTTPException(422, 'Negative offset')
    async with upload_mutex:
        info = session_info(upload_id)
        if offset != info['offset']:
            raise HTTPException(409, f"Offset mismatch: server offset is {info['offset']}")
        payload = await req.body()
        if not payload or len(payload) > MAX_CHUNK_BYTES:
            raise HTTPException(413, 'Upload chunks must be 1–16 MiB')
        if offset + len(payload) > info['size']:
            raise HTTPException(413, 'Chunk exceeds declared file size')
        path, _ = session_files(upload_id)
        with path.open('ab') as stream:
            stream.write(payload)
        return {'id': upload_id, 'offset': offset + len(payload), 'size': info['size']}


@app.post('/api/uploads/{upload_id}/complete', status_code=202)
async def complete_chunked_upload(req: Request, upload_id: str):
    require_auth(req)
    async with upload_mutex:
        info = session_info(upload_id)
        if info['offset'] != info['size']:
            raise HTTPException(409, 'Upload is incomplete')
        path, manifest = session_files(upload_id)
        with lock:
            jobs[upload_id] = {'id': upload_id, 'filename': info['filename'], 'bytes_total': info['size'],
                               'bytes_transferred': 0, 'state': 'queued', 'message': 'Queued for FTPS transfer',
                               'created': int(time.time())}
        manifest.unlink(missing_ok=True)
        executor.submit(ftps_transfer, upload_id, path, info['filename'], info['size'])
        return snapshot_job(upload_id)
