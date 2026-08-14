"""
agent/executor.py — LifeOS v2.7
Изменения: поддержка медиаклавиш в _do_press через Windows API (ctypes).
"""

from __future__ import annotations

import os
import subprocess
import time
import webbrowser
import pyautogui
import pyperclip
from pydantic import BaseModel

from agent.protocol.actions import (
    OpenAppAction,
    CloseAppAction,
    ClickAction,
    DoubleClickAction,
    RightClickAction,
    MoveMouseAction,
    ScrollAction,
    PressAction,
    HotkeyAction,
    TypeAction,
    WaitAction,
    ScreenshotAction,
    FindTextAction,
    FindImageAction,
    SetVolumeAction,
    SayAction,
    OpenUrlAction,
    GetClipboardAction,
    SetClipboardAction,
)

APP_MAP: dict[str, str] = {
    "telegram": "https://web.telegram.org/k",
    "chrome": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "firefox": r"C:\Program Files\Mozilla Firefox\firefox.exe",
    "steam": r"C:\Program Files (x86)\Steam\steam.exe",
    "notepad": "notepad.exe",
    "explorer": "explorer.exe",
    "taskmgr": "taskmgr.exe",
    "settings": "ms-settings:",
    "calculator": "calc.exe",
    "paint": "mspaint.exe",
    "spotify": r"C:\Users\%USERNAME%\AppData\Roaming\Spotify\Spotify.exe",
    "vscode": "code",
    "word": "winword.exe",
    "excel": "excel.exe",
    "beamng.drive": r"C:\Program Files (x86)\Steam\steamapps\common\BeamNG.drive\BeamNG.drive.exe",
    "yandex music": r"C:\Users\Admin\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Яндекс Музыка.lnk",
    "roblox": r"C:\Users\Admin\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Roblox\Roblox Player.lnk",
    "яндекс музыка": r"C:\Users\Admin\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Яндекс Музыка.lnk",
    "v2raytun": r"C:\Users\Admin\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\v2raytun.lnk",
    "впн": r"C:\Users\Admin\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\v2raytun.lnk",
    "vpn": r"C:\Users\Admin\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\v2raytun.lnk",
}

# Windows Virtual-Key коды для медиаклавиш
# https://learn.microsoft.com/en-us/windows/win32/inputdev/virtual-key-codes
MEDIA_KEYS: dict[str, int] = {
    "next_track":       0xB0,
    "prev_track":       0xB1,
    "previous_track":   0xB1,
    "media_stop":       0xB2,
    "play_pause":       0xB3,
    "media_play_pause": 0xB3,
    "volume_mute":      0xAD,
    "volume_down":      0xAE,
    "volume_up":        0xAF,
}


def _resolve_app(name: str) -> str:
    return APP_MAP.get(name.lower().strip(), name)


def _send_media_key(vk_code: int) -> None:
    """Отправляет медиаклавишу через Windows API."""
    import ctypes
    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP       = 0x0002
    ctypes.windll.user32.keybd_event(vk_code, 0, KEYEVENTF_EXTENDEDKEY, 0)
    ctypes.windll.user32.keybd_event(vk_code, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)


class ActionResult(BaseModel):
    action: str
    success: bool
    output: str = ""
    screenshot: bytes | None = None

    class Config:
        arbitrary_types_allowed = True


class Executor:
    def __init__(self, on_say=None, on_screenshot=None):
        self.on_say = on_say
        self.on_screenshot = on_screenshot
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.05

    def _set_volume_windows(self, percent: int) -> bool:
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        devices = AudioUtilities.GetSpeakers()
        print(devices)

        interface = devices.Activate(
            IAudioEndpointVolume._iid_,
            CLSCTX_ALL,
            None,
        )

        volume = cast(interface, POINTER(IAudioEndpointVolume))

        print("Current:", volume.GetMasterVolumeLevelScalar())

        volume.SetMasterVolumeLevelScalar(percent / 100.0, None)

        print("New:", volume.GetMasterVolumeLevelScalar())

        return True

    async def run_plan(self, actions: list[BaseModel]) -> list[ActionResult]:
        import asyncio
        results = []
        for action in actions:
            result = await asyncio.to_thread(self._execute_sync, action)
            results.append(result)
            if not result.success:
                break
        return results

    def _execute_sync(self, action: BaseModel) -> ActionResult:
        name = action.action
        handler = getattr(self, f"_do_{name}", None)
        if handler is None:
            return ActionResult(action=name, success=False, output="No handler")
        try:
            return handler(action)
        except Exception as e:
            return ActionResult(action=name, success=False, output=str(e))

    def _do_say(self, a: SayAction) -> ActionResult:
        if self.on_say:
            self.on_say(a.text)
            return ActionResult(action="say", success=True, output=a.text)
        return ActionResult(action="say", success=False, output="No callback")

    def _do_screenshot(self, a: ScreenshotAction) -> ActionResult:
        import io
        img = pyautogui.screenshot()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        img_bytes = buf.getvalue()
        if a.send_to_chat and self.on_screenshot:
            self.on_screenshot(img_bytes)
        return ActionResult(action="screenshot", success=True)

    def _do_set_volume(self, a: SetVolumeAction) -> ActionResult:
        success = self._set_volume_windows(a.percent)
        return ActionResult(
            action="set_volume",
            success=success,
            output=f"Volume set to {a.percent}%" if success else "Failed to set volume",
        )

    def _do_wait(self, a: WaitAction) -> ActionResult:
        time.sleep(a.seconds)
        return ActionResult(action="wait", success=True, output=f"{a.seconds}s")

    def _do_open_url(self, a: OpenUrlAction) -> ActionResult:
        webbrowser.open(a.url)
        return ActionResult(action="open_url", success=True, output=a.url)

    def _do_open_app(self, a: OpenAppAction) -> ActionResult:
        path = _resolve_app(a.app)
        if path.startswith("ms-"):
            subprocess.Popen(["start", path], shell=True)
        else:
            subprocess.Popen(os.path.expandvars(path), shell=True)
        return ActionResult(action="open_app", success=True, output=f"Opened: {a.app}")

    def _do_close_app(self, a: CloseAppAction) -> ActionResult:
        name = a.app.lower().replace(".exe", "")
        cmd = f'taskkill /f /im "{name}.exe"'
        print(cmd)
        code = os.system(cmd)
        print(code)
        return ActionResult(action="close_app", success=True, output=f"Closed: {a.app}")

    def _do_click(self, a: ClickAction) -> ActionResult:
        pyautogui.click(a.x, a.y)
        return ActionResult(action="click", success=True)

    def _do_double_click(self, a: DoubleClickAction) -> ActionResult:
        pyautogui.doubleClick(a.x, a.y)
        return ActionResult(action="double_click", success=True)

    def _do_right_click(self, a: RightClickAction) -> ActionResult:
        pyautogui.rightClick(a.x, a.y)
        return ActionResult(action="right_click", success=True)

    def _do_move_mouse(self, a: MoveMouseAction) -> ActionResult:
        pyautogui.moveTo(a.x, a.y, duration=0.2)
        return ActionResult(action="move_mouse", success=True)

    def _do_scroll(self, a: ScrollAction) -> ActionResult:
        clicks = a.amount // 120
        if a.direction == "down":
            pyautogui.scroll(-clicks)
        elif a.direction == "up":
            pyautogui.scroll(clicks)
        return ActionResult(action="scroll", success=True)

    def _do_press(self, a: PressAction) -> ActionResult:
        key_lower = a.key.lower()

        # Медиаклавиши — pyautogui их не знает, используем Windows API напрямую
        if key_lower in MEDIA_KEYS:
            _send_media_key(MEDIA_KEYS[key_lower])
            return ActionResult(action="press", success=True, output=f"media: {a.key}")

        pyautogui.press(a.key)
        return ActionResult(action="press", success=True, output=a.key)

    def _do_hotkey(self, a: HotkeyAction) -> ActionResult:
        pyautogui.hotkey(*a.keys)
        return ActionResult(action="hotkey", success=True)

    def _do_type(self, a: TypeAction) -> ActionResult:
        pyautogui.write(a.text, interval=0.03)
        return ActionResult(action="type", success=True)

    def _do_find_text(self, a: FindTextAction) -> ActionResult:
        try:
            import pytesseract
            from PIL import ImageGrab
            img = ImageGrab.grab()
            found = (
                a.text.lower()
                in pytesseract.image_to_string(img, lang="rus+eng").lower()
            )
            return ActionResult(action="find_text", success=found, output=f"found={found}")
        except Exception as e:
            return ActionResult(action="find_text", success=False, output=str(e))

    def _do_find_image(self, a: FindImageAction) -> ActionResult:
        loc = pyautogui.locateOnScreen(a.image, confidence=0.8)
        if loc is None:
            return ActionResult(action="find_image", success=False, output="Not found")
        if a.click_on_found:
            pyautogui.click(pyautogui.center(loc))
        return ActionResult(action="find_image", success=True)

    def _do_get_clipboard(self, a: GetClipboardAction) -> ActionResult:
        return ActionResult(action="get_clipboard", success=True, output=pyperclip.paste())

    def _do_set_clipboard(self, a: SetClipboardAction) -> ActionResult:
        pyperclip.copy(a.text)
        return ActionResult(action="set_clipboard", success=True)