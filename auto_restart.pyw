# -*- coding: utf-8 -*-
"""
자동 종료/재시작 트레이 프로그램 (Windows 포터블)

기능
  - 요일별로 원하는 시각에 PC 자동 '종료' 또는 '재시작' (요일마다 따로 설정)
  - 예약 시각이 되면 열린 프로그램을 모두 무시하고 '강제로' 즉시 실행 (경고 팝업 없음)
  - 창의 ✕(닫기)를 눌러도 종료되지 않고 트레이로 최소화되어 계속 동작
  - 부팅 시 자동 실행되어 트레이에 상주 → 설정은 매주 그대로 유지·반복

관리자 권한 불필요. 설정은 실행 파일과 같은 폴더의 config.json 에 저장됩니다.
"""

import os
import sys
import copy
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
APP_TITLE = "자동 종료/재시작"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
CREATE_NO_WINDOW = 0x08000000          # 콘솔 창 깜빡임 방지

# 요일 (datetime.weekday(): 월=0 ... 일=6)
WD_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WD_LABELS = ["월", "화", "수", "목", "금", "토", "일"]


# ----------------------------------------------------------------------------
# 경로 / 설정
# ----------------------------------------------------------------------------
def app_dir():
    """실행 파일(또는 스크립트)이 위치한 폴더 — 포터블 설정 저장 위치."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(app_dir(), "config.json")


def default_schedule():
    return {k: {"on": False, "hour": 18, "minute": 0, "action": "shutdown"}
            for k in WD_KEYS}


DEFAULT_CONFIG = {
    "schedule": default_schedule(),   # 요일별 {on, hour, minute, action}
    "enabled": False,
    "autostart": True,
}


def load_config():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if k == "schedule" and isinstance(v, dict):
                for dk, dv in v.items():
                    if dk in cfg["schedule"] and isinstance(dv, dict):
                        cfg["schedule"][dk].update(dv)
            else:
                cfg[k] = v
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
# 트레이 아이콘 이미지 (전원 심볼)
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

        # 내부 상태
        self.next_dt = None               # 다음 실행 시각(datetime) 또는 None
        self.next_action = None           # 'shutdown' | 'restart'
        self.start_time = time.time()     # 부팅 직후 우발적 실행 방지용
        self.sched_running = True
        self._last_enabled = None
        self._show_request = False
        self._quit_request = False
        self._notified_tray = False

        # GUI
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("400x560")
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

        if "--tray" in sys.argv:
            self.root.withdraw()

        self._tick()

    # --- GUI 구성 ---------------------------------------------------------
    def _build_gui(self):
        self.autostart_var = tk.BooleanVar(value=self.cfg["autostart"])
        self.status_var = tk.StringVar(value="")
        self.row_vars = {}   # key -> (on_var, hour_var, min_var, act_var)

        ttk.Label(self.root, text="🔌  자동 종료/재시작",
                  font=("Malgun Gothic", 14, "bold")).pack(pady=(10, 4))

        # 요일별 예약
        sf = ttk.LabelFrame(self.root, text="요일별 예약  (체크한 요일 · 시각 · 동작)")
        sf.pack(fill="x", padx=12, pady=6)

        for key, label in zip(WD_KEYS, WD_LABELS):
            e = self.cfg["schedule"][key]
            row = ttk.Frame(sf)
            row.pack(fill="x", padx=8, pady=1)

            on_var = tk.BooleanVar(value=e["on"])
            hour_var = tk.StringVar(value=f'{int(e["hour"]):02d}')
            min_var = tk.StringVar(value=f'{int(e["minute"]):02d}')
            act_var = tk.StringVar(
                value="종료" if e.get("action", "shutdown") == "shutdown" else "재시작")

            ttk.Checkbutton(row, text=label, variable=on_var, width=3).pack(side="left")
            ttk.Spinbox(row, from_=0, to=23, width=3, textvariable=hour_var,
                        format="%02.0f", wrap=True).pack(side="left", padx=(6, 1))
            ttk.Label(row, text=":").pack(side="left")
            ttk.Spinbox(row, from_=0, to=59, width=3, textvariable=min_var,
                        format="%02.0f", wrap=True).pack(side="left", padx=(1, 8))
            ttk.Combobox(row, values=["종료", "재시작"], textvariable=act_var,
                         width=6, state="readonly").pack(side="left")

            self.row_vars[key] = (on_var, hour_var, min_var, act_var)

        # 옵션
        f2 = ttk.LabelFrame(self.root, text="옵션")
        f2.pack(fill="x", padx=12, pady=6)
        ttk.Checkbutton(f2, text="부팅 시 자동 실행 (트레이 상주 · 설정 유지)",
                        variable=self.autostart_var).pack(anchor="w", padx=8, pady=6)

        # 상태
        ttk.Label(self.root, textvariable=self.status_var, justify="left",
                  font=("Malgun Gothic", 10)).pack(fill="x", padx=14, pady=(6, 4))

        # 버튼
        b = ttk.Frame(self.root)
        b.pack(fill="x", padx=12, pady=(2, 4))
        ttk.Button(b, text="예약 시작", command=self.apply_settings).pack(
            side="left", expand=True, fill="x", padx=2)
        ttk.Button(b, text="예약 중지", command=self.stop_schedule).pack(
            side="left", expand=True, fill="x", padx=2)
        ttk.Button(b, text="트레이로 최소화", command=self.hide_window).pack(
            side="left", expand=True, fill="x", padx=2)

        ttk.Label(self.root,
                  text="✕ 를 눌러도 트레이에서 계속 동작합니다.\n"
                       "예약 시각이 되면 열린 프로그램을 모두 무시하고 강제 실행합니다.",
                  foreground="#888", justify="center").pack(pady=(2, 8))

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
        if self.cfg["enabled"] and self.next_dt:
            act = "종료" if self.next_action == "shutdown" else "재시작"
            return "다음: " + self.next_dt.strftime("%m/%d %H:%M") + " " + act
        return "예약 없음"

    # --- 다음 실행 시각 계산 (tk 호출 없음 → 어느 스레드에서나 안전) -------
    def _recompute_next(self):
        if not self.cfg["enabled"]:
            self.next_dt = None
            self.next_action = None
            return
        now = datetime.datetime.now()
        best = None
        best_action = None
        for i in range(0, 8):  # 오늘부터 7일 내 가장 가까운 예약
            day = now + datetime.timedelta(days=i)
            e = self.cfg["schedule"].get(WD_KEYS[day.weekday()])
            if not e or not e.get("on"):
                continue
            dt = day.replace(hour=int(e["hour"]), minute=int(e["minute"]),
                             second=0, microsecond=0)
            if dt <= now:
                continue
            if best is None or dt < best:
                best = dt
                best_action = e.get("action", "shutdown")
        self.next_dt = best
        self.next_action = best_action

    # --- 스케줄러 스레드 ---------------------------------------------------
    def _scheduler(self):
        last_fire = None
        while self.sched_running:
            try:
                if self.cfg["enabled"] and time.time() - self.start_time > 90:
                    now = datetime.datetime.now()
                    e = self.cfg["schedule"].get(WD_KEYS[now.weekday()])
                    if (e and e.get("on")
                            and now.hour == int(e["hour"])
                            and now.minute == int(e["minute"])):
                        fire_key = (now.year, now.month, now.day, now.hour, now.minute)
                        if fire_key != last_fire:
                            last_fire = fire_key
                            self._do_action(e.get("action", "shutdown"))
            except Exception:
                pass
            time.sleep(10)

    def _do_action(self, action):
        # 무조건 강제 즉시 실행 — 열린 프로그램/저장 안 된 파일 전부 무시
        flag = "/s" if action == "shutdown" else "/r"
        try:
            subprocess.run(["shutdown", flag, "/t", "0", "/f"],
                           creationflags=CREATE_NO_WINDOW)
        except Exception:
            pass

    # --- 동작 (버튼/트레이 공용) ------------------------------------------
    def apply_settings(self):
        sched = {}
        try:
            for key in WD_KEYS:
                on_var, hour_var, min_var, act_var = self.row_vars[key]
                sched[key] = {
                    "on": bool(on_var.get()),
                    "hour": max(0, min(23, int(hour_var.get()))),
                    "minute": max(0, min(59, int(min_var.get()))),
                    "action": "shutdown" if act_var.get() == "종료" else "restart",
                }
        except ValueError:
            messagebox.showerror(APP_TITLE, "시각을 올바르게 입력하세요.")
            return

        if not any(sched[k]["on"] for k in WD_KEYS):
            messagebox.showerror(APP_TITLE, "최소 한 개 요일을 체크하세요.")
            return

        self.cfg["schedule"] = sched
        self.cfg["enabled"] = True
        # 요일 예약은 매주 반복·부팅 유지가 필요 → 자동 실행 보장
        self.cfg["autostart"] = True
        self.autostart_var.set(True)

        set_autostart(True)
        save_config(self.cfg)
        self._recompute_next()

        lines = []
        for key, label in zip(WD_KEYS, WD_LABELS):
            e = sched[key]
            if e["on"]:
                act = "종료" if e["action"] == "shutdown" else "재시작"
                lines.append(f"  {label}요일  {e['hour']:02d}:{e['minute']:02d}  {act}")
        messagebox.showinfo(
            APP_TITLE,
            "다음 예약이 시작되었습니다:\n\n" + "\n".join(lines) +
            "\n\n재부팅 후에도 매주 그대로 유지됩니다.")

    def stop_schedule(self):
        self.cfg["enabled"] = False
        save_config(self.cfg)
        self._recompute_next()
        messagebox.showinfo(APP_TITLE, "예약을 중지했습니다.")

    # --- 트레이 콜백 (tk 직접 호출 금지 → 플래그/평범한 상태만 변경) -------
    def _tray_show(self, icon=None, item=None):
        self._show_request = True

    def _tray_quit(self, icon=None, item=None):
        self._quit_request = True

    def _tray_toggle(self, icon, item):
        self.cfg["enabled"] = not self.cfg["enabled"]
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
        if self.cfg["enabled"] and self.next_dt:
            remaining = int((self.next_dt - datetime.datetime.now()).total_seconds())
            remaining = max(0, remaining)
            h, m, s = remaining // 3600, (remaining % 3600) // 60, remaining % 60
            act = "종료" if self.next_action == "shutdown" else "재시작"
            wd = WD_LABELS[self.next_dt.weekday()]
            head = f"다음: {self.next_dt.strftime('%m/%d')}({wd}) {self.next_dt.strftime('%H:%M')} {act}"
            self.status_var.set(f"{head}\n남은 시간: {h:02d}:{m:02d}:{s:02d}")
        else:
            self.status_var.set("예약 없음 — 요일/시각을 정하고 '예약 시작'을 누르세요.")

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
