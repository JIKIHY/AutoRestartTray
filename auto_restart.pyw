# -*- coding: utf-8 -*-
"""
자동 재시작 트레이 프로그램 (Windows 포터블)

기능
  - 24시간 내 '매일 지정 시각' 또는 '카운트다운'으로 PC 자동 재시작
  - 예약 시각이 되면 열린 프로그램을 모두 무시하고 '강제로' 즉시 재시작 (경고 팝업 없음)
  - 창의 ✕(닫기)를 눌러도 종료되지 않고 트레이로 최소화되어 계속 동작
  - 부팅 시 자동 실행되어 트레이에 상주

관리자 권한 불필요. 설정은 실행 파일과 같은 폴더의 config.json 에 저장됩니다.
"""

import os
import sys
import json
import time
import threading
import datetime
import subprocess

import tkinter as tk
from tkinter import ttk, messagebox

import pystray
from PIL import Image, ImageDraw

# Windows 전용 모듈 (다른 OS에서는 임포트만 건너뜀)
try:
    import winreg
except ImportError:
    winreg = None

try:
    import ctypes
except ImportError:
    ctypes = None


APP_NAME = "AutoRestartTray"          # 레지스트리/뮤텍스 식별자
APP_TITLE = "자동 재시작"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
CREATE_NO_WINDOW = 0x08000000          # 콘솔 창 깜빡임 방지


# ----------------------------------------------------------------------------
# 경로 / 설정
# ----------------------------------------------------------------------------
def app_dir():
    """실행 파일(또는 스크립트)이 위치한 폴더 — 포터블 설정 저장 위치."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(app_dir(), "config.json")

DEFAULT_CONFIG = {
    "mode": "daily",        # 'daily'(매일 지정 시각) | 'countdown'(카운트다운)
    "hour": 4,
    "minute": 0,
    "cd_hours": 3,
    "cd_minutes": 0,
    "cd_target_ts": None,   # 카운트다운 목표 절대시각(epoch)
    "enabled": False,
    "autostart": True,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# 부팅 시 자동 실행 (HKCU Run)
# ----------------------------------------------------------------------------
def get_run_command():
    """자동 실행에 등록할 명령. --tray 로 시작하면 창 없이 트레이로만 뜸."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --tray'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    return f'"{pythonw}" "{os.path.abspath(__file__)}" --tray'


def set_autostart(enable):
    if winreg is None:
        return
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        if enable:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, get_run_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# 트레이 아이콘 이미지 (전원/재시작 심볼)
# ----------------------------------------------------------------------------
def make_icon(active):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = (33, 150, 243, 255) if active else (130, 130, 130, 255)  # 파랑/회색
    d.ellipse([3, 3, 61, 61], fill=color)
    d.arc([20, 20, 44, 44], start=300, end=240, fill="white", width=5)
    d.line([32, 14, 32, 34], fill="white", width=5)
    return img


# ----------------------------------------------------------------------------
# 메인 애플리케이션
# ----------------------------------------------------------------------------
class App:
    def __init__(self):
        self.cfg = load_config()

        # 내부 상태 (스레드 간 공유되는 평범한 속성)
        self.next_restart = None          # 다음 재시작 시각(datetime) 또는 None
        self.start_time = time.time()     # 부팅 직후 우발적 재시작 방지용
        self.sched_running = True
        self._last_enabled = None
        self._show_request = False        # 트레이 → 창 열기 요청
        self._quit_request = False        # 트레이 → 종료 요청
        self._notified_tray = False       # 트레이 안내 알림 1회용

        # GUI
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("380x430")
        self.root.resizable(False, False)
        self._build_gui()
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)  # ✕ → 트레이로

        # 트레이 아이콘 (별도 스레드)
        self.icon = pystray.Icon(APP_NAME, make_icon(self.cfg["enabled"]),
                                 APP_TITLE, self._build_menu())
        threading.Thread(target=self.icon.run, daemon=True).start()

        # 스케줄러 스레드
        threading.Thread(target=self._scheduler, daemon=True).start()

        set_autostart(self.cfg["autostart"])
        self._recompute_next()

        # --tray 로 시작되면 창을 숨긴 채 트레이로만
        if "--tray" in sys.argv:
            self.root.withdraw()

        self._tick()

    # --- GUI 구성 ---------------------------------------------------------
    def _build_gui(self):
        self.mode_var = tk.StringVar(value=self.cfg["mode"])
        self.hour_var = tk.StringVar(value=f'{int(self.cfg["hour"]):02d}')
        self.min_var = tk.StringVar(value=f'{int(self.cfg["minute"]):02d}')
        self.cdh_var = tk.StringVar(value=str(int(self.cfg["cd_hours"])))
        self.cdm_var = tk.StringVar(value=str(int(self.cfg["cd_minutes"])))
        self.autostart_var = tk.BooleanVar(value=self.cfg["autostart"])
        self.status_var = tk.StringVar(value="")

        ttk.Label(self.root, text="🔄  자동 재시작",
                  font=("Malgun Gothic", 14, "bold")).pack(pady=(12, 4))

        # 재시작 시점
        f1 = ttk.LabelFrame(self.root, text="재시작 시점")
        f1.pack(fill="x", padx=12, pady=6)

        r1 = ttk.Frame(f1)
        r1.pack(fill="x", padx=8, pady=4)
        ttk.Radiobutton(r1, text="매일 지정 시각", value="daily",
                        variable=self.mode_var).pack(side="left")
        ttk.Spinbox(r1, from_=0, to=23, width=4, textvariable=self.hour_var,
                    format="%02.0f", wrap=True).pack(side="left", padx=(8, 2))
        ttk.Label(r1, text=":").pack(side="left")
        ttk.Spinbox(r1, from_=0, to=59, width=4, textvariable=self.min_var,
                    format="%02.0f", wrap=True).pack(side="left", padx=(2, 0))

        r2 = ttk.Frame(f1)
        r2.pack(fill="x", padx=8, pady=4)
        ttk.Radiobutton(r2, text="카운트다운", value="countdown",
                        variable=self.mode_var).pack(side="left")
        ttk.Spinbox(r2, from_=0, to=24, width=4, textvariable=self.cdh_var,
                    wrap=True).pack(side="left", padx=(8, 2))
        ttk.Label(r2, text="시간").pack(side="left")
        ttk.Spinbox(r2, from_=0, to=59, width=4, textvariable=self.cdm_var,
                    wrap=True).pack(side="left", padx=(8, 2))
        ttk.Label(r2, text="분 후").pack(side="left")

        # 옵션
        f2 = ttk.LabelFrame(self.root, text="옵션")
        f2.pack(fill="x", padx=12, pady=6)
        ttk.Checkbutton(f2, text="부팅 시 자동 실행 (트레이 상주)",
                        variable=self.autostart_var).pack(anchor="w", padx=8, pady=6)

        # 상태
        ttk.Label(self.root, textvariable=self.status_var, justify="left",
                  font=("Malgun Gothic", 10)).pack(fill="x", padx=14, pady=(8, 4))

        # 버튼 (활성화 체크 없이 '예약 시작' 한 번이면 동작)
        b = ttk.Frame(self.root)
        b.pack(fill="x", padx=12, pady=(4, 4))
        ttk.Button(b, text="예약 시작", command=self.apply_settings).pack(
            side="left", expand=True, fill="x", padx=2)
        ttk.Button(b, text="예약 중지", command=self.stop_schedule).pack(
            side="left", expand=True, fill="x", padx=2)
        ttk.Button(b, text="트레이로 최소화", command=self.hide_window).pack(
            side="left", expand=True, fill="x", padx=2)

        ttk.Label(self.root,
                  text="✕ 를 눌러도 트레이에서 계속 동작합니다.\n"
                       "예약 시각이 되면 열린 프로그램을 모두 무시하고 강제 재시작합니다.",
                  foreground="#888", justify="center").pack(pady=(2, 10))

    # --- 트레이 메뉴 ------------------------------------------------------
    def _build_menu(self):
        return pystray.Menu(
            pystray.MenuItem("열기", self._tray_show, default=True),
            pystray.MenuItem(lambda item: self._status_line(),
                             lambda icon, item: None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("예약 켜기/끄기", self._tray_toggle,
                             checked=lambda item: self.cfg["enabled"]),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("종료", self._tray_quit),
        )

    def _status_line(self):
        if self.cfg["enabled"] and self.next_restart:
            return "다음: " + self.next_restart.strftime("%m/%d %H:%M")
        return "예약 없음"

    # --- 다음 재시작 시각 계산 (tk 호출 없음 → 어느 스레드에서나 안전) -----
    def _recompute_next(self):
        if not self.cfg["enabled"]:
            self.next_restart = None
            return
        if self.cfg["mode"] == "daily":
            now = datetime.datetime.now()
            t = now.replace(hour=int(self.cfg["hour"]), minute=int(self.cfg["minute"]),
                            second=0, microsecond=0)
            if t <= now:
                t += datetime.timedelta(days=1)
            self.next_restart = t
        else:  # countdown
            ts = self.cfg.get("cd_target_ts")
            if ts and ts > time.time():
                self.next_restart = datetime.datetime.fromtimestamp(ts)
            else:
                self.next_restart = None
                self.cfg["enabled"] = False

    # --- 스케줄러 스레드 ---------------------------------------------------
    def _scheduler(self):
        while self.sched_running:
            try:
                # 부팅 직후 90초는 보류 (재시작→부팅→즉시재시작 루프 방지)
                if (self.cfg["enabled"] and self.next_restart
                        and time.time() - self.start_time > 90
                        and datetime.datetime.now() >= self.next_restart):
                    self._do_restart()
            except Exception:
                pass
            time.sleep(1)

    def _do_restart(self):
        # 무조건 강제 즉시 재시작 — 열린 프로그램/저장 안 된 파일 전부 무시
        try:
            subprocess.run(["shutdown", "/r", "/t", "0", "/f"],
                           creationflags=CREATE_NO_WINDOW)
        except Exception:
            pass
        # 재부팅 전 중복 트리거 방지
        if self.cfg["mode"] == "daily":
            self.next_restart = self.next_restart + datetime.timedelta(days=1)
        else:
            self.cfg["enabled"] = False
            self.cfg["cd_target_ts"] = None
            self.next_restart = None
            save_config(self.cfg)

    # --- 동작 (버튼/트레이 공용) ------------------------------------------
    def apply_settings(self):
        try:
            self.cfg["mode"] = self.mode_var.get()
            self.cfg["hour"] = max(0, min(23, int(self.hour_var.get())))
            self.cfg["minute"] = max(0, min(59, int(self.min_var.get())))
            self.cfg["cd_hours"] = max(0, min(24, int(self.cdh_var.get())))
            self.cfg["cd_minutes"] = max(0, min(59, int(self.cdm_var.get())))
        except ValueError:
            messagebox.showerror(APP_TITLE, "숫자를 올바르게 입력하세요.")
            return

        # '예약 시작' = 곧바로 활성화 (활성화 체크 단계 없음)
        self.cfg["enabled"] = True
        self.cfg["autostart"] = self.autostart_var.get()
        if self.cfg["mode"] == "daily":
            # 매일 반복은 부팅 자동실행이 있어야 유지됨 → 자동 보장
            self.cfg["autostart"] = True
            self.autostart_var.set(True)

        if self.cfg["mode"] == "countdown":
            dur = self.cfg["cd_hours"] * 3600 + self.cfg["cd_minutes"] * 60
            if dur <= 0:
                self.cfg["enabled"] = False
                messagebox.showerror(APP_TITLE, "카운트다운 시간을 설정하세요.")
                return
            self.cfg["cd_target_ts"] = time.time() + dur

        set_autostart(self.cfg["autostart"])
        save_config(self.cfg)
        self._recompute_next()

        if self.cfg["mode"] == "daily":
            msg = (f"매일 {self.cfg['hour']:02d}:{self.cfg['minute']:02d} 에 자동 재시작됩니다.\n"
                   "재부팅 후에도 유지되며, 시각이 되면 강제로 재시작합니다.")
        else:
            msg = f"{self.cfg['cd_hours']}시간 {self.cfg['cd_minutes']}분 후 강제 재시작됩니다."
        messagebox.showinfo(APP_TITLE, msg)

    def stop_schedule(self):
        self.cfg["enabled"] = False
        self.cfg["cd_target_ts"] = None
        save_config(self.cfg)
        self._recompute_next()
        messagebox.showinfo(APP_TITLE, "자동 재시작 예약을 중지했습니다.")

    # --- 트레이 콜백 (tk 직접 호출 금지 → 플래그/평범한 상태만 변경) -------
    def _tray_show(self, icon=None, item=None):
        self._show_request = True

    def _tray_quit(self, icon=None, item=None):
        self._quit_request = True

    def _tray_toggle(self, icon, item):
        self.cfg["enabled"] = not self.cfg["enabled"]
        if self.cfg["enabled"] and self.cfg["mode"] == "countdown":
            dur = int(self.cfg["cd_hours"]) * 3600 + int(self.cfg["cd_minutes"]) * 60
            if dur > 0:
                self.cfg["cd_target_ts"] = time.time() + dur
        self._recompute_next()
        save_config(self.cfg)
        try:
            icon.update_menu()
        except Exception:
            pass

    # --- 창 표시/숨김/종료 (메인 스레드) ----------------------------------
    def _show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_window(self):
        self.root.withdraw()
        if not self._notified_tray:
            self._notified_tray = True
            try:
                self.icon.notify("트레이에서 계속 실행 중입니다.", APP_TITLE)
            except Exception:
                pass

    def _do_quit(self):
        self.sched_running = False
        try:
            self.icon.stop()
        except Exception:
            pass
        self.root.destroy()

    def _update_icon(self):
        try:
            self.icon.icon = make_icon(self.cfg["enabled"])
        except Exception:
            pass

    # --- 메인 스레드 UI 갱신 루프 -----------------------------------------
    def _tick(self):
        if self._show_request:
            self._show_request = False
            self._show_window()
        if self._quit_request:
            self._quit_request = False
            self._do_quit()
            return

        if self._last_enabled != self.cfg["enabled"]:
            self._last_enabled = self.cfg["enabled"]
            self._update_icon()
            try:
                self.icon.update_menu()
            except Exception:
                pass

        self._refresh_status()
        self.root.after(1000, self._tick)

    def _refresh_status(self):
        if self.cfg["enabled"] and self.next_restart:
            remaining = int((self.next_restart - datetime.datetime.now()).total_seconds())
            remaining = max(0, remaining)
            h, m, s = remaining // 3600, (remaining % 3600) // 60, remaining % 60
            if self.cfg["mode"] == "daily":
                head = f"매일 {int(self.cfg['hour']):02d}:{int(self.cfg['minute']):02d} 자동 재시작 (반복)"
            else:
                head = "다음 재시작: " + self.next_restart.strftime("%m/%d %H:%M")
            self.status_var.set(f"{head}\n남은 시간: {h:02d}:{m:02d}:{s:02d}")
        else:
            self.status_var.set("예약 없음 — 시각을 정하고 '예약 시작'을 누르세요.")

    def run(self):
        self.root.mainloop()


def main():
    # 단일 인스턴스 보장 (이미 실행 중이면 종료)
    if ctypes is not None and sys.platform == "win32":
        ctypes.windll.kernel32.CreateMutexW(None, False, "AutoRestartTray_Mutex")
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            sys.exit(0)
    App().run()


if __name__ == "__main__":
    main()
