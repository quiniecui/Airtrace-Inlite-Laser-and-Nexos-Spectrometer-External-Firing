#!/usr/bin/env python3
"""
inlite_gui.py  (v5 - adds PRIME burst button)
Inlite laser control GUI - Python + Tkinter.

Controls (top -> bottom): RUN/STOP, STANDBY/READY, SHUTTER, PRIME, FIRE, RESET.
SHUTTER can be opened/closed in ANY state (no sequence gate).
PRIME sends a burst of flashlamp pulses (shutter closed) to feed the laser's
voltage ramp (VR) so it can reach Emission (MD,3) in external flash mode (PF,2).
Each held control is a TOGGLE with an indicator light driven by the Arduino's
reported state.

Requires:  pip install pyserial   (Tkinter ships with Python)

Command vocabulary MUST match the firmware:
  RUN_ON/RUN_OFF, READY_ON/READY_OFF, SHUTTER_OPEN/SHUTTER_CLOSE,
  FIRE, PRIME, PING, ESTOP

SAFETY: convenience only, NOT a safety system. The shutter is the last barrier
before light exits - rely on the physical shutter, hardware interlocks, key
switch, and eyewear as the real protection, not this button.
"""

import tkinter as tk
import serial
import serial.tools.list_ports

BAUD = 115200
PING_INTERVAL_MS = 500
SERIAL_PORT = "COM3"        # set to your Arduino's port; None = auto-pick

# ---- Theme colors ----
BG    = "#dce6f0"           # window background (light blue)
FG    = "#1a1a1a"
GREEN = "#2ecc40"
GRAY  = "#999999"
RED   = "#c0392b"
WARN  = "#b5790a"


def find_port():
    if SERIAL_PORT:
        return SERIAL_PORT
    ports = list(serial.tools.list_ports.comports())
    for p in ports:
        print("Found port:", p.device, "-", p.description)
    return ports[0].device if ports else None


class LaserGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Inlite Laser Control")
        self.root.configure(bg=BG)
        self.fire_armed = False
        self.priming = False

        self.sig = {"RUN": 0, "READY": 0, "SHUTTER": 0}
        self.mode = "?"

        port = find_port()
        self.ser, self.connected = None, False
        if port:
            try:
                self.ser = serial.Serial(port, BAUD, timeout=0)
                self.connected = True
                print("Connected to", port)
            except Exception as e:
                print("Serial error:", e)
        else:
            print("No serial port found.")

        self._build_ui()
        self._schedule_ping()
        self._schedule_read()

    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        self.mode_lbl = tk.Label(self.root, text="MODE: ?", font=("Arial", 18, "bold"),
                                 fg=FG, bg=BG)
        self.mode_lbl.grid(row=0, column=0, columnspan=3, pady=(12, 4))

        self.conn_lbl = tk.Label(self.root, font=("Arial", 10), bg=BG,
                                 fg=(GREEN if self.connected else RED),
                                 text=("connected" if self.connected else "NOT CONNECTED"))
        self.conn_lbl.grid(row=1, column=0, columnspan=3, pady=(0, 8))

        self.lights = {}
        self.btns = {}
        self.txts = {}

        def make_row(r, key, on_label, off_label, on_cmd, off_cmd):
            light = tk.Canvas(self.root, width=26, height=26, bg=BG, highlightthickness=0)
            dot = light.create_oval(3, 3, 23, 23, fill=GRAY, outline="#555")
            light.grid(row=r, column=0, **pad)
            self.lights[key] = (light, dot)

            btn = tk.Button(self.root, width=20, height=2, fg="white",
                            command=lambda: self.toggle(key, on_cmd, off_cmd))
            btn.grid(row=r, column=1, **pad)
            self.btns[key] = (btn, on_label, off_label)

            txt = tk.Label(self.root, width=10, bg=BG, fg=FG, font=("Arial", 11))
            txt.grid(row=r, column=2, **pad)
            self.txts[key] = txt

        make_row(2, "RUN",     "RUN",          "STOP",          "RUN_ON",       "RUN_OFF")
        make_row(3, "READY",   "GO READY",     "GO STANDBY",    "READY_ON",     "READY_OFF")
        make_row(4, "SHUTTER", "OPEN SHUTTER", "CLOSE SHUTTER", "SHUTTER_OPEN", "SHUTTER_CLOSE")

        self.prime_btn = tk.Button(self.root, text="PRIME (100 shots)", width=30, height=2,
                                   bg="#2c5f8a", fg="white", command=self.on_prime)
        self.prime_btn.grid(row=5, column=0, columnspan=3, pady=(14, 4))

        self.fire_btn = tk.Button(self.root, text="FIRE", width=30, height=2,
                                  bg="#b43c00", fg="white", command=self.on_fire)
        self.fire_btn.grid(row=6, column=0, columnspan=3, pady=(4, 6))

        tk.Button(self.root, text="RESET (E-STOP)", width=30, height=2,
                  bg=RED, fg="white", command=self.on_estop
                  ).grid(row=7, column=0, columnspan=3, pady=(6, 12))

        self.msg = tk.Label(self.root, text="", fg=WARN, bg=BG, font=("Arial", 10),
                            wraplength=360)
        self.msg.grid(row=8, column=0, columnspan=3, pady=(0, 10))

        self._refresh_widgets()

    def toggle(self, key, on_cmd, off_cmd):
        self._disarm_fire()
        # Sequence warnings remain for READY; SHUTTER is now unrestricted (any state).
        if on_cmd == "READY_ON" and not self.sig["RUN"]:
            self._warn("Warning: going READY before RUN is set.")
        self.send(off_cmd if self.sig[key] else on_cmd)

    def on_prime(self):
        self._disarm_fire()
        if self.priming:
            self._warn("Prime already running.")
            return
        if not (self.sig["RUN"] and self.sig["READY"]):
            self._warn("Prime needs RUN + READY.")
            return
        if self.sig["SHUTTER"]:
            self._warn("Close the shutter before priming.")
            return
        self.send("PRIME")

    def on_fire(self):
        if not self.fire_armed:
            if not (self.sig["RUN"] and self.sig["READY"] and self.sig["SHUTTER"]):
                self._warn("Not armed (need RUN + READY + SHUTTER).")
            self.fire_armed = True
            self.fire_btn.config(text="FIRE - click again to confirm", bg="#ff9600")
        else:
            self.send("FIRE")
            self._disarm_fire()

    def _disarm_fire(self):
        if self.fire_armed:
            self.fire_armed = False
            self.fire_btn.config(text="FIRE", bg="#b43c00")

    def on_estop(self):
        self._disarm_fire()
        self.send("ESTOP")
        self._warn("E-STOP sent: all signals dropped to safe.")

    def _warn(self, text):
        self.msg.config(text=text)

    def send(self, cmd):
        if self.ser and self.connected:
            try:
                self.ser.write((cmd + "\n").encode())
            except Exception as e:
                print("Write failed:", e)
        else:
            print("Cannot send (%s): not connected" % cmd)

    def _schedule_ping(self):
        if self.ser and self.connected:
            try:
                self.ser.write(b"PING\n")
            except Exception:
                pass
        self.root.after(PING_INTERVAL_MS, self._schedule_ping)

    def _schedule_read(self):
        if self.ser and self.connected:
            try:
                while self.ser.in_waiting:
                    line = self.ser.readline().decode(errors="ignore").strip()
                    if line:
                        self._handle_line(line)
            except Exception as e:
                print("Read error:", e)
        self.root.after(50, self._schedule_read)

    def _handle_line(self, line):
        print("Arduino:", line)
        if line.startswith("SIG:"):
            for part in line[4:].split(","):
                if "=" in part:
                    k, v = part.split("=", 1)
                    if k in self.sig:
                        self.sig[k] = 1 if v == "1" else 0
                    elif k == "MODE":
                        self.mode = v
            self._refresh_widgets()
        elif line.startswith("SHOT_LEFT:"):
            left = line[len("SHOT_LEFT:"):]
            self._warn("Priming... shots remaining: " + left)
        elif line.startswith("EVENT:PRIME_START"):
            self.priming = True
            self._set_prime_busy(True)
            self._warn("Prime started (100 shots).")
        elif line.startswith("EVENT:PRIME_DONE"):
            self.priming = False
            self._set_prime_busy(False)
            self._warn("Prime done. Check MD over RS232 for MD,3.")
        elif line.startswith("EVENT:PRIME_ABORT"):
            self.priming = False
            self._set_prime_busy(False)
            self._warn("Prime aborted (arming lost).")
        elif line.startswith("ERR:"):
            self._warn("Arduino: " + line[4:])
        elif line.startswith("EVENT:"):
            self._warn(line[6:])

    def _set_prime_busy(self, busy):
        self.prime_btn.config(
            text=("PRIMING..." if busy else "PRIME (100 shots)"),
            bg=("#7a4fb0" if busy else "#2c5f8a"),
            state=("disabled" if busy else "normal"))

    def _refresh_widgets(self):
        emit = (self.mode == "EMISSION") and self.sig["SHUTTER"]
        self.mode_lbl.config(
            text=("MODE: " + self.mode + ("  - LIGHT CAN EXIT" if emit else "")),
            fg=(RED if emit else FG))

        for key, (light, dot) in self.lights.items():
            light.itemconfig(dot, fill=(GREEN if self.sig[key] else GRAY))

        for key, (btn, on_label, off_label) in self.btns.items():
            on = self.sig[key]
            btn.config(text=(off_label if on else on_label),
                       bg=("#1f6f3f" if on else "#34495e"))
            self.txts[key].config(text=("ON" if on else "off"),
                                  fg=(GREEN if on else "#777777"))


def main():
    root = tk.Tk()
    LaserGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()