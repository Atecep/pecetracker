"""Optional Linux browser smoke against real app.py and disposable fixtures.

Requires an installed Chromium-family browser and the existing websockets
package. No personal profiles/data or live metadata services are accessed.
"""
import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class Browser:
    def __init__(self, connection):
        self.connection = connection
        self.sequence = 0

    async def call(self, method, **params):
        self.sequence += 1
        sequence = self.sequence
        await self.connection.send(json.dumps({'id': sequence, 'method': method, 'params': params}))
        while True:
            message = json.loads(await asyncio.wait_for(self.connection.recv(), 15))
            if message.get('id') == sequence:
                if 'error' in message: raise RuntimeError(message['error'])
                return message.get('result', {})

    async def evaluate(self, expression):
        result = await self.call('Runtime.evaluate', expression=expression, returnByValue=True)
        if 'exceptionDetails' in result: raise RuntimeError(result['exceptionDetails'])
        return result.get('result', {}).get('value')

    async def wait(self, expression):
        for _ in range(120):
            if await self.evaluate('document.body && (' + expression + ')'): return
            await asyncio.sleep(.25)
        raise RuntimeError('Browser condition timed out: ' + expression)

    async def click(self, text):
        selector = "Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes(%s))" % json.dumps(text)
        await self.wait('Boolean(' + selector + ')')
        await self.evaluate(selector + '.click()')

    async def screenshot(self, path):
        result = await self.call('Page.captureScreenshot', format='png', captureBeyondViewport=False)
        path.write_bytes(base64.b64decode(result['data']))


async def exercise(ws, url, output):
    import websockets
    async with websockets.connect(ws, max_size=20 * 1024 * 1024) as connection:
        browser = Browser(connection)
        await browser.call('Page.enable')
        await browser.call('Emulation.setDeviceMetricsOverride', width=1440, height=1000, deviceScaleFactor=1, mobile=False)
        await browser.call('Network.enable')
        await browser.call('Network.setBlockedURLs', urls=['*steamstatic.com*', '*github.com*', '*steampowered.com*', '*howlongtobeat.com*', '*protondb.com*'])
        await browser.call('Page.navigate', url=url)
        await browser.wait("document.body.innerText.includes('2 games in your library')")
        await browser.wait("Boolean(document.querySelector('[data-testid=stTextInput] input'))")
        await asyncio.sleep(.5)
        await browser.screenshot(output / 'library.png')
        await browser.evaluate("document.querySelector('[data-testid=stTextInput] input').focus()")
        await browser.call('Input.insertText', text='[')
        await browser.call('Input.dispatchKeyEvent', type='keyDown', key='Enter', code='Enter', windowsVirtualKeyCode=13)
        await browser.call('Input.dispatchKeyEvent', type='keyUp', key='Enter', code='Enter', windowsVirtualKeyCode=13)
        await browser.wait("document.body.innerText.includes('1 games found')")
        await browser.screenshot(output / 'literal-search.png')
        await browser.click('Open Fixture [Edition]')
        await browser.wait("Boolean(document.querySelector('[data-testid=stDialog] textarea'))")
        await browser.evaluate("document.querySelector('[data-testid=stDialog] textarea').focus(); document.querySelector('[data-testid=stDialog] textarea').select()")
        await browser.call('Input.insertText', text='Linux browser smoke draft')
        await browser.call('Input.dispatchKeyEvent', type='keyDown', key='Tab', code='Tab', windowsVirtualKeyCode=9)
        await browser.call('Input.dispatchKeyEvent', type='keyUp', key='Tab', code='Tab', windowsVirtualKeyCode=9)
        await asyncio.sleep(.5)
        await browser.click('Analyse ProtonDB')
        await browser.wait("document.body.innerText.includes('ProtonDB analysis')")
        await browser.screenshot(output / 'protondb.png')
        await browser.click('← Back')
        await browser.wait("document.querySelector('[data-testid=stDialog] textarea')?.value === 'Linux browser smoke draft'")
        await browser.click('💾 Save')
        await asyncio.sleep(1)
        await browser.screenshot(output / 'saved-details.png')
        if await browser.evaluate("Boolean(document.querySelector('[data-testid=stException]'))"):
            raise RuntimeError('Streamlit exception visible in browser')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--browser', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='slt-browser-') as scratch:
        scratch = Path(scratch)
        env = dict(os.environ, HOME=str(scratch), XDG_DATA_HOME=str(scratch), XDG_CONFIG_HOME=str(scratch), LOCALAPPDATA=str(scratch), STREAMLIT_BROWSER_GATHER_USAGE_STATS='false', PYTHONDONTWRITEBYTECODE='1')
        fixture = scratch / 'fixture.py'
        fixture.write_text('''import sys, runpy
from pathlib import Path
sys.path.insert(0, %r)
import database, settings, steam_cache, update_checker, global_bootstrap, metadata_background, hltb_background, protondb_service
from ui import common
from constants import STATUS_BACKLOG
import requests
requests.adapters.HTTPAdapter.send = lambda *a, **kw: (_ for _ in ()).throw(AssertionError('Live HTTP forbidden'))
database.init_database()
if not database.get_all_game_data():
 database.save_game_data(620, STATUS_BACKLOG, 'fixture notes')
 database.save_hltb_metadata(620, 'Fixture', 'matched', hltb_id=42, matched_name='Fixture', match_confidence='manual', main_story=10)
 database.save_game_metadata({'appid':620,'genres':['Action'],'categories':['Single-player'],'reviews':{'positive_percentage':95,'total_reviews':100}})
settings.load_settings = lambda: {'steam_profile':'fixture','steam_id':'123','steam_api_key':'fixture'}
steam_cache.load_games = lambda *a: [{'appid':620,'name':'Fixture [Edition]','playtime_forever':60},{'appid':621,'name':'Other fixture','playtime_forever':0}]
steam_cache.load_player_summary = lambda *a: {'personaname':'Fixture'}
update_checker.check_for_updates = lambda: None
global_bootstrap.start_global_bootstrap = lambda *a: None
global_bootstrap.get_bootstrap_result = lambda: None
metadata_background.start_metadata_background_refresh = lambda *a: None
hltb_background.start_hltb_background_refresh = lambda *a: None
protondb_service.analyse_protondb = lambda *a: {'summary':None}
from urllib.parse import quote
common.get_game_image = lambda appid: 'data:image/svg+xml,' + quote('<svg xmlns="http://www.w3.org/2000/svg" width="460" height="215"><rect width="460" height="215" fill="slateblue"/></svg>')
runpy.run_path(%r, run_name='__main__')
''' % (str(ROOT), str(ROOT / 'app.py')))
        port = free_port(); url = f'http://127.0.0.1:{port}'
        with (args.output / 'server.log').open('w') as server_log, (args.output / 'browser.log').open('w') as browser_log:
            server = subprocess.Popen([sys.executable, '-B', '-m', 'streamlit', 'run', str(fixture), '--server.address=127.0.0.1', f'--server.port={port}', '--server.headless=true', '--server.fileWatcherType=none', '--browser.gatherUsageStats=false'], env=env, stdout=server_log, stderr=subprocess.STDOUT)
            browser = None
            try:
                for _ in range(100):
                    if server.poll() is not None: raise RuntimeError('Streamlit exited; see server.log')
                    try:
                        with urlopen(url + '/_stcore/health', timeout=.5) as response:
                            if response.status == 200: break
                    except OSError: time.sleep(.1)
                else: raise RuntimeError('Streamlit did not become healthy')
                print('Temporary Streamlit server healthy', flush=True)
                profile = scratch / 'browser-profile'
                browser = subprocess.Popen([str(args.browser), '--disable-vivaldi', '--headless=new', '--disable-gpu', '--no-first-run', '--disable-background-networking', '--disable-component-update', '--disable-extensions', '--no-default-browser-check', '--remote-debugging-port=0', '--user-data-dir=' + str(profile), 'about:blank'], env=env, stdout=browser_log, stderr=subprocess.STDOUT)
                active = profile / 'DevToolsActivePort'
                for _ in range(100):
                    if browser.poll() is not None: raise RuntimeError('Browser exited; see browser.log')
                    if active.exists(): break
                    time.sleep(.1)
                else: raise RuntimeError('Browser DevTools unavailable')
                debug_port = active.read_text().splitlines()[0]
                with urlopen(f'http://127.0.0.1:{debug_port}/json/list', timeout=2) as response:
                    targets = json.load(response)
                target = next(item for item in targets if item['type'] == 'page')
                asyncio.run(exercise(target['webSocketDebuggerUrl'], url, args.output))
                import sqlite3
                with sqlite3.connect(scratch / 'SteamLibraryTracker' / 'library.db') as connection:
                    notes = connection.execute('SELECT notes FROM game_data WHERE appid=620').fetchone()[0]
                    manual = connection.execute('SELECT hltb_id,match_confidence FROM hltb_metadata WHERE appid=620').fetchone()
                if notes != 'Linux browser smoke draft' or manual != (42,'manual'):
                    raise RuntimeError('Saved data did not match browser actions')
                print('Linux rendered smoke passed: literal search, details, ProtonDB/back, explicit save, manual ID preserved', flush=True)
            finally:
                for process in (browser, server):
                    if process is not None:
                        process.terminate()
                        try: process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill();process.wait(timeout=5)


if __name__ == '__main__':
    main()
