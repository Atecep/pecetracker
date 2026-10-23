"""Optional real Linux frozen-binary/browser smoke with temporary legacy data.

Does not use real settings, migrate personal data or contact metadata services.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from linux_browser_smoke import Browser
from version import APP_NAME, APP_TAGLINE


async def render(ws, url, output):
    import websockets
    async with websockets.connect(ws,max_size=20*1024*1024) as connection:
        browser=Browser(connection)
        await browser.call('Page.enable')
        await browser.call('Emulation.setDeviceMetricsOverride',width=1440,height=1000,deviceScaleFactor=1,mobile=False)
        await browser.call('Network.enable')
        await browser.call('Network.setBlockedURLs',urls=['*github.com*','*steampowered.com*','*howlongtobeat.com*','*protondb.com*'])
        await browser.call('Page.navigate',url=url)
        await browser.wait('document.title === '+json.dumps(APP_NAME))
        await browser.wait('document.body.innerText.includes('+json.dumps(APP_TAGLINE)+')')
        await browser.wait("Boolean(document.querySelector('[data-testid=stTextInput]'))")
        if await browser.evaluate("Boolean(document.querySelector('[data-testid=stException]'))"):
            raise RuntimeError('Frozen application rendered an exception')
        await browser.screenshot(output/'frozen-onboarding.png')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--executable',required=True,type=Path)
    parser.add_argument('--browser',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    executable=args.executable.resolve()
    with tempfile.TemporaryDirectory(prefix='pecetracker-frozen-') as scratch:
        scratch=Path(scratch);data=scratch/'SteamLibraryTracker';data.mkdir()
        # Import only after path redirection; all mutations below are fixtures.
        import database
        with patch.object(database,'DATABASE_PATH',data/'library.db'),patch.object(database,'ensure_data_dir'):
            database.init_database()
            database.save_game_data(620,'Playing','legacy personal note fixture')
            database.save_hltb_metadata(620,'Portal 2','matched',hltb_id=42,match_confidence='manual')
        config=data/'config.json';config.write_text(json.dumps({'steam_id':'','steam_api_key':'','steam_profile':''}))
        lock=data/'instance.lock';lock.write_text('{"pid":0,"protocol":2}')
        inode=lock.stat().st_ino
        before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (data/'library.db',config)}
        env=dict(os.environ,HOME=str(scratch),USERPROFILE=str(scratch),XDG_DATA_HOME=str(scratch),XDG_CONFIG_HOME=str(scratch),LOCALAPPDATA=str(scratch),PYTHONDONTWRITEBYTECODE='1',BROWSER='/bin/true',HTTP_PROXY='http://127.0.0.1:9',HTTPS_PROXY='http://127.0.0.1:9',ALL_PROXY='http://127.0.0.1:9',NO_PROXY='127.0.0.1,localhost')
        with (args.output/'frozen.log').open('w') as log,(args.output/'browser.log').open('w') as browser_log:
            app=subprocess.Popen([str(executable)],env=env,stdout=log,stderr=subprocess.STDOUT)
            browser=None
            try:
                state_path=data/'instance.json'
                for _ in range(150):
                    if app.poll() is not None:raise RuntimeError('Frozen process exited; see frozen.log')
                    if state_path.exists():
                        state=json.loads(state_path.read_text())
                        url=f"http://127.0.0.1:{state['port']}"
                        try:
                            with urlopen(url+'/_stcore/health',timeout=.5) as response:
                                if response.status==200:break
                        except OSError:pass
                    time.sleep(.1)
                else:raise RuntimeError('Frozen server did not become healthy')
                if state['pid']!=app.pid or state['protocol']!=2:raise RuntimeError('Unexpected ownership record')
                if lock.stat().st_ino!=inode:raise RuntimeError('Legacy lock inode was replaced')
                # Same-version launch must reopen the verified owner, not start
                # another server or terminate the first one.
                second=subprocess.run([str(executable)],env=env,stdout=log,stderr=subprocess.STDOUT,timeout=20)
                if second.returncode or app.poll() is not None:raise RuntimeError('Second launch did not reopen the existing owner')
                if json.loads(state_path.read_text())['instance_token']!=state['instance_token']:raise RuntimeError('Second launch changed ownership')
                profile=scratch/'browser-profile'
                browser=subprocess.Popen([str(args.browser),'--disable-vivaldi','--headless=new','--disable-gpu','--no-first-run','--disable-background-networking','--disable-component-update','--disable-extensions','--no-default-browser-check','--remote-debugging-port=0','--user-data-dir='+str(profile),'about:blank'],env=env,stdout=browser_log,stderr=subprocess.STDOUT)
                active=profile/'DevToolsActivePort'
                for _ in range(100):
                    if browser.poll() is not None:raise RuntimeError('Browser exited; see browser.log')
                    if active.exists():break
                    time.sleep(.1)
                else:raise RuntimeError('Browser DevTools unavailable')
                port=active.read_text().splitlines()[0]
                with urlopen(f'http://127.0.0.1:{port}/json/list',timeout=2) as response:targets=json.load(response)
                target=next(t for t in targets if t['type']=='page')
                asyncio.run(render(target['webSocketDebuggerUrl'],url,args.output))
                after={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (data/'library.db',config)}
                if after!=before:raise RuntimeError('Onboarding changed existing SQLite/config fixture')
                with sqlite3.connect(data/'library.db') as connection:
                    if connection.execute('SELECT hltb_id,match_confidence FROM hltb_metadata WHERE appid=620').fetchone()!=(42,'manual'):raise RuntimeError('Manual HLTB fixture changed')
                if (scratch/'Pecetracker').exists():raise RuntimeError('A second data directory was created')
                print('Frozen Linux smoke passed: Pecetracker title/tagline, legacy data unchanged, manual ID, native lock and second launch',flush=True)
            finally:
                for process in (browser,app):
                    if process is not None:
                        process.terminate()
                        try:process.wait(timeout=10)
                        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)


if __name__=='__main__':
    main()
