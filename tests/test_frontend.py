from pathlib import Path
import subprocess
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / 'blackbox-portable-dashboard.html'
VIDEO = ROOT / 'tests/sample.mp4'

def make_sample():
    if not VIDEO.exists():
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc=size=320x180:rate=25','-t','6','-c:v','mpeg4','-q:v','7',str(VIDEO)],check=True)

def test_local_html_dashboard():
    make_sample()
    http = ThreadingHTTPServer(('127.0.0.1', 0), partial(SimpleHTTPRequestHandler, directory=str(ROOT)))
    threading.Thread(target=http.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{http.server_address[1]}/blackbox-portable-dashboard.html'
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path='/usr/bin/chromium', args=['--no-sandbox','--disable-web-security'])
        page = browser.new_page(viewport={'width':1440,'height':900}, accept_downloads=True)
        failures=[]
        page.on('pageerror',lambda error:failures.append(str(error)))
        page.set_content(DASH.read_text(),wait_until="load")
        assert page.title().startswith('BlackBox Companion')
        assert page.locator('#stat-clips').inner_text()=='0'
        page.screenshot(path=str(ROOT/'dashboard-preview-desktop.png'),full_page=True)
        page.locator('#files-input').set_input_files(str(VIDEO))
        page.wait_for_timeout(2200)
        assert page.locator('#stat-clips').inner_text()=='1'
        assert page.locator('.clip').count()==1
        page.get_by_role('button',name='Edit',exact=True).click()
        page.locator('#edit-title').fill('Colorful animated test pattern')
        page.locator('#edit-description').fill('Colorful television test patterns move across the display')
        page.locator('#edit-keywords').fill('abstract;test;pattern;color;screen;texture;motion;technology')
        page.locator('#edit-reviewed').check()
        page.locator('#save-editor').click()
        assert page.locator('#stat-meta').inner_text()=='1'
        page.locator('[data-view="settings"]').click()
        assert page.locator('#view-settings').is_visible()
        page.locator('#template-input').set_input_files({'name':'official-template.csv','mimeType':'text/csv','buffer':b'FILENAME,DESCRIPTION,KEY WORDS,BATCHNAME,EDITORIAL,EDITORIAL CAPTION\n'})
        assert 'FILENAME' in page.locator('#schema-state').inner_text()
        page.locator('[data-view="workspace"]').click()
        with page.expect_download() as download_info:
            page.locator('#export-csv').click()
        download=download_info.value
        data=Path(download.path()).read_text(encoding='utf-8-sig')
        assert 'Colorful television test patterns' in data
        assert 'sample.mp4' in data
        with page.expect_download() as download_info:
            page.locator('#export-workspace').click()
        assert download_info.value.suggested_filename.endswith('.json')
        page.screenshot(path=str(ROOT/'dashboard-preview-with-clip.png'),full_page=True)
        mobile=browser.new_page(viewport={'width':390,'height':844},device_scale_factor=2,accept_downloads=True)
        mobile.set_content(DASH.read_text(),wait_until="load")
        mobile.screenshot(path=str(ROOT/'dashboard-preview-mobile.png'),full_page=True)
        assert not failures,failures
        browser.close()
    http.shutdown()
