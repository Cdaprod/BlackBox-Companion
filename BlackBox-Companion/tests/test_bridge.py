"""Mock-only unit/integration tests. These never contact BlackBox or a vision API."""
from __future__ import annotations
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import time

os.environ['BRIDGE_TOKEN'] = 'test-' + ('x' * 32)
os.environ['BLACKBOX_FTP_USER'] = 'dummy'
os.environ['BLACKBOX_FTP_PASSWORD'] = 'dummy'
os.environ['UPLOAD_SPOOL_DIR'] = tempfile.mkdtemp(prefix='bbtest-')
os.environ['CORS_ORIGINS'] = 'null'
spec = importlib.util.spec_from_file_location('server', Path(__file__).resolve().parents[1] / 'bridge/server.py')
server = importlib.util.module_from_spec(spec)
sys.modules['server'] = server
spec.loader.exec_module(server)
from fastapi.testclient import TestClient
client = TestClient(server.app)
HEADERS = {'Authorization': 'Bearer ' + os.environ['BRIDGE_TOKEN']}

class FakeFTPS:
    records = []
    def __init__(self, *args, **kwargs):
        pass
    def connect(self, host, port):
        self.records.append(('connect', host, port))
    def login(self, user, password):
        self.records.append(('login', user))
    def prot_p(self):
        self.records.append(('prot_p',))
    def cwd(self, path):
        self.records.append(('cwd', path))
    def storbinary(self, command, stream, blocksize, callback):
        blocks = []
        while chunk := stream.read(blocksize):
            blocks.append(chunk)
            callback(chunk)
        self.records.append(('stored', command, b''.join(blocks)))
    def quit(self):
        pass

def test_auth_health():
    assert client.get('/api/health').status_code == 401
    result = client.get('/api/health', headers=HEADERS)
    assert result.status_code == 200
    assert result.json()['ftp_configured']

def test_cors_file_origin():
    r = client.options('/api/health', headers={'Origin': 'null', 'Access-Control-Request-Method': 'GET', 'Access-Control-Request-Headers': 'authorization'})
    assert r.status_code == 200
    assert r.headers['access-control-allow-origin'] == 'null'

def test_chunked_ftps_flow(monkeypatch):
    monkeypatch.setattr(server, 'FTP_TLS', FakeFTPS)
    payload = b'asset example data' * 10000
    r = client.post('/api/uploads', headers=HEADERS, json={'filename': 'NIKON_0123.mov', 'size': len(payload)})
    assert r.status_code == 201, r.text
    upload_id = r.json()['id']
    first = payload[:50000]
    assert client.put(f'/api/uploads/{upload_id}/chunk?offset=0', data=first, headers=HEADERS).json()['offset'] == len(first)
    assert client.put(f'/api/uploads/{upload_id}/chunk?offset=0', data=first, headers=HEADERS).status_code == 409
    state = client.get(f'/api/uploads/{upload_id}', headers=HEADERS).json()
    assert state['offset'] == len(first)
    assert client.post(f'/api/uploads/{upload_id}/complete', headers=HEADERS).status_code == 409
    r = client.put(f'/api/uploads/{upload_id}/chunk?offset={len(first)}', data=payload[len(first):], headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()['offset'] == len(payload)
    done = client.post(f'/api/uploads/{upload_id}/complete', headers=HEADERS)
    assert done.status_code == 202
    for _ in range(100):
        status = client.get('/api/jobs/' + upload_id, headers=HEADERS).json()
        if status['state'] == 'transferred':
            break
        time.sleep(.03)
    assert status['state'] == 'transferred', status
    assert ('prot_p',) in FakeFTPS.records
    assert ('stored', 'STOR NIKON_0123.mov', payload) in FakeFTPS.records
    assert not (server.UPLOAD_DIR / (upload_id + '.part')).exists()

def test_name_validation():
    for filename in ['../escape.mp4', 'hack\nSTOR bad', 'back\\slash.mp4']:
        r = client.post('/api/uploads', headers=HEADERS, json={'filename': filename, 'size': 100})
        assert r.status_code == 422

def test_metadata_requires_configured_provider():
    r = client.post('/api/metadata', headers=HEADERS, json={'filename': 'clip.mp4', 'frames': ['data:image/jpeg;base64,YQ==']})
    assert r.status_code == 503

def test_metadata_vision_response(monkeypatch):
    import io
    class FakeHTTP:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self,*args): return self.data.read(*args)
        def __init__(self):
            response = {'choices': [{'message': {'content': __import__('json').dumps({
                'title': 'Clouds drifting above trees',
                'description': 'Soft white clouds pass above a green woodland on a clear day',
                'keywords': ['clouds','trees','woodland','sky','clouds'],
                'category': 'Nature',
                'editorial_review_recommended': False,
                'review_reason': ''
            })}}]}
            self.data=io.BytesIO(__import__('json').dumps(response).encode())
    monkeypatch.setattr(server,'AI_KEY','test-key')
    monkeypatch.setattr(server,'urlopen',lambda *args,**kwargs:FakeHTTP())
    r=client.post('/api/metadata',headers=HEADERS,json={'filename':'woodland.mp4','frames':['data:image/jpeg;base64,YWJj']})
    assert r.status_code == 200,r.text
    assert r.json()['title']=='Clouds drifting above trees'
    assert r.json()['keywords']==['clouds','trees','woodland','sky']
