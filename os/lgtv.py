"""LG webOS TV: upravljanje preko SSAP (WebSocket wss://<ip>:3001, samopotpisan sertifikat).
Ovaj firmver odbija potpisani LG manifest ("blacklisted certificate"), pa se registruje NEpotpisanim;
ključ uparivanja se čuva u data/lgtv-key.json ({"client-key": ...}) — sa njim TV ne pita ponovo.
"""
import asyncio
import itertools
import json
import os
import ssl

import aiohttp

PERMS = ['LAUNCH', 'LAUNCH_WEBAPP', 'APP_TO_APP', 'CLOSE', 'TEST_OPEN', 'TEST_PROTECTED', 'CONTROL_AUDIO',
         'CONTROL_DISPLAY', 'CONTROL_INPUT_JOYSTICK', 'CONTROL_INPUT_MEDIA_RECORDING', 'CONTROL_INPUT_MEDIA_PLAYBACK',
         'CONTROL_INPUT_TV', 'CONTROL_POWER', 'READ_APP_STATUS', 'READ_CURRENT_CHANNEL', 'READ_INPUT_DEVICE_LIST',
         'READ_NETWORK_STATE', 'READ_RUNNING_APPS', 'READ_TV_CHANNEL_LIST', 'WRITE_NOTIFICATION_TOAST', 'READ_POWER_STATE',
         'READ_COUNTRY_INFO', 'READ_SETTINGS', 'CONTROL_TV_SCREEN', 'CONTROL_TV_STANBY', 'CONTROL_FAVORITE_GROUP',
         'CONTROL_USER_INFO', 'CHECK_BLUETOOTH_DEVICE', 'CONTROL_BLUETOOTH', 'CONTROL_TIMER_INFO', 'STB_INTERNAL_CONNECTION',
         'CONTROL_RECORDING', 'READ_RECORDING_STATE', 'WRITE_RECORDING_LIST', 'READ_RECORDING_LIST', 'READ_RECORDING_SCHEDULE',
         'WRITE_RECORDING_SCHEDULE', 'READ_STORAGE_DEVICE_LIST', 'READ_TV_PROGRAM_INFO', 'CONTROL_BOX_CHANNEL',
         'READ_TV_ACR_AUTH_TOKEN', 'READ_TV_CONTENT_STATE', 'READ_TV_CURRENT_TIME', 'ADD_LAUNCHER_CHANNEL',
         'SET_CHANNEL_SKIP', 'RELEASE_CHANNEL_SKIP', 'CONTROL_CHANNEL_BLOCK', 'DELETE_SELECT_CHANNEL',
         'CONTROL_CHANNEL_GROUP', 'SCAN_TV_CHANNELS', 'CONTROL_TV_POWER', 'CONTROL_WOL',
         # (isti spisak kao "signed" deo LG manifesta — ključ je izdat za ovaj skup)
         'TEST_SECURE', 'CONTROL_INPUT_TEXT', 'CONTROL_MOUSE_AND_KEYBOARD', 'READ_INSTALLED_APPS',
         'READ_LGE_SDX', 'READ_NOTIFICATIONS', 'SEARCH', 'WRITE_SETTINGS', 'WRITE_NOTIFICATION_ALERT',
         'CONTROL_POWER', 'READ_CURRENT_CHANNEL', 'READ_RUNNING_APPS', 'READ_UPDATE_INFO', 'UPDATE_FROM_REMOTE_APP',
         'READ_LGE_TV_INPUT_EVENTS', 'READ_TV_CURRENT_TIME']


class TvError(Exception):
    pass


class Tv:
    def __init__(self, ip, key_file):
        self.ip, self.key_file = ip, key_file
        self.ses = self.ws = self.reader = None
        self.ids = itertools.count(1)
        self.wait = {}
        self.lock = asyncio.Lock()
        self.ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)   # bez učitavanja sistemskih sertifikata (sporo na Pi-ju, ionako se ne proveravaju)
        self.ssl.check_hostname = False
        self.ssl.verify_mode = ssl.CERT_NONE

    def key(self):
        try:
            with open(self.key_file) as f:
                return json.load(f).get('client-key')
        except (OSError, ValueError):
            return None

    @property
    def up(self):
        return self.ws is not None and not self.ws.closed

    async def close(self):
        if self.ws is not None:
            await self.ws.close()
        if self.ses is not None:
            await self.ses.close()
        self.ses = self.ws = None

    async def connect(self, pair_wait=8):
        """Otvara vezu i registruje se. Bez ključa TV pita na ekranu (pair_wait sekundi da se prihvati)."""
        async with self.lock:
            if self.up:
                return
            await self.close()
            self.ses = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=3))
            try:
                self.ws = await self.ses.ws_connect('wss://%s:3001' % self.ip, ssl=self.ssl, heartbeat=20, max_msg_size=0)
                await self.ws.send_str(json.dumps({'id': 'reg', 'type': 'register', 'payload': {
                    'forcePairing': False, 'pairingType': 'PROMPT', 'client-key': self.key(),
                    'manifest': {'manifestVersion': 1, 'appVersion': '1.1', 'permissions': PERMS}}}))
                while True:   # 'response' (PROMPT na TV-u) pa 'registered'
                    m = await self.ws.receive(timeout=pair_wait)
                    if m.type != aiohttp.WSMsgType.TEXT:
                        raise TvError('TV je zatvorio vezu')
                    m = json.loads(m.data)
                    if m.get('type') == 'registered':
                        k = (m.get('payload') or {}).get('client-key')
                        if k and k != self.key():
                            tmp = self.key_file + '.tmp'
                            with open(tmp, 'w') as f:
                                json.dump({'client-key': k}, f)
                            os.replace(tmp, self.key_file)
                        break
                    if m.get('type') == 'error':
                        raise TvError(m.get('error') or 'TV odbio uparivanje')
            except asyncio.TimeoutError:
                await self.close()
                raise TvError('TV nije prihvatio uparivanje (potvrdi upit na TV-u)')
            except (aiohttp.ClientError, OSError) as e:
                await self.close()
                raise TvError('TV nije dostupan (%s)' % (e.__class__.__name__,))
            except TvError:
                await self.close()
                raise
            self.reader = asyncio.get_running_loop().create_task(self._read(self.ws))

    async def _read(self, ws):
        try:
            async for m in ws:
                if m.type != aiohttp.WSMsgType.TEXT:
                    break
                m = json.loads(m.data)
                f = self.wait.pop(m.get('id'), None)
                if f and not f.done():
                    f.set_result(m)
        except (aiohttp.ClientError, ValueError):
            pass
        finally:
            for f in self.wait.values():
                if not f.done():
                    f.set_exception(TvError('veza sa TV-om prekinuta'))
            self.wait.clear()
            if ws is self.ws:
                await self.close()

    async def req(self, uri, payload=None, timeout=5):
        await self.connect()
        i = 'r%d' % next(self.ids)
        f = self.wait[i] = asyncio.get_running_loop().create_future()
        await self.ws.send_str(json.dumps({'id': i, 'type': 'request', 'uri': uri, 'payload': payload or {}}))
        try:
            m = await asyncio.wait_for(f, timeout)
        except asyncio.TimeoutError:
            self.wait.pop(i, None)
            raise TvError('TV ne odgovara')
        p = m.get('payload') or {}
        if m.get('type') == 'error' or p.get('returnValue') is False:
            raise TvError(m.get('error') or p.get('errorText') or 'greška')
        return p

    async def button(self, name):
        """Taster daljinskog (HOME, BACK, CHANNELUP…): ide i na uređaj na HDMI-ju preko SIMPLINK-a (CEC)."""
        p = await self.req('ssap://com.webos.service.networkinput/getPointerInputSocket')
        async with self.ses.ws_connect(p['socketPath'], ssl=self.ssl) as w:
            await w.send_str('type:button\nname:%s\n\n' % name)
            await asyncio.sleep(0.2)
