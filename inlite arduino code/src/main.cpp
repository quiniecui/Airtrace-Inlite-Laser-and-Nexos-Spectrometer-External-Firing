#include <Arduino.h>

/*
 * inlite_firmware.ino  (v4 - adds PRIME burst)
 * Inlite laser control - Arduino Uno firmware
 *
 * Controls: RUN/STOP, STANDBY/READY, SHUTTER, FLASHLAMP FIRE, PRIME (burst).
 * (Q-switch enable intentionally NOT included in this version.)
 *
 *   Arduino pin HIGH -> transistor ON -> laser input pulled to GROUND -> ASSERTED
 *   Arduino pin LOW  -> transistor OFF -> laser internal pull-up holds idle (SAFE)
 *
 * Command vocabulary (MUST match the GUI):
 *   RUN_ON / RUN_OFF
 *   READY_ON / READY_OFF
 *   SHUTTER_OPEN / SHUTTER_CLOSE
 *   FIRE          (momentary, single flashlamp pulse)
 *   PRIME         (burst of PRIME_SHOTS flashlamp pulses at 20 Hz, shutter closed)
 *   PING          (heartbeat)
 *   ESTOP         (drop all to safe)
 *
 * Reports one SIG: line after each change so the GUI lights match real pin state.
 *
 * ----------------------------------------------------------------------------
 * SAFETY: enforces operating SEQUENCE only. NOT a safety system.
 * Real safety = hardware interlocks (DB-15 pins 7&8, 2&10), key switch,
 * physical shutter, eyewear. Laser must be in IF,E and the correct PF mode;
 * interlock loops CLOSED.
 * NOTE: Without Q-switch enable, the laser reaches READY but will not emit
 * Q-switched pulses unless Q-switching is enabled by other means.
 * ----------------------------------------------------------------------------
 */

// ---- Pin assignments (clear of 0,1 = serial and 13 = onboard LED) ----
const int PIN_RUN     = 6;   // -> RUN/STOP        (DB-15 pin 3)
const int PIN_READY   = 7;   // -> STANDBY/READY   (DB-15 pin 4)
const int PIN_SHUTTER = 8;   // -> SHUTTER         (DB-15 pin 5)
const int PIN_FIRE    = 9;   // -> FLASHLAMP FIRE  (DB-15 pin 15)

// ---- Fire pulse timing ((10) 10us- (5000)5ms) ----
const unsigned long FIRE_PULSE_US = 10;

// ---- Prime burst (non-blocking): feeds the VR voltage ramp ----
const int           PRIME_SHOTS       = 100;  // matches VR ramp (500 V over 100 shots)
const unsigned long PRIME_INTERVAL_MS = 50;   // 20 Hz (laser HZ=20); 1/20 s = 50 ms
int           primeShotsLeft = 0;
unsigned long primeLastShot  = 0;

// ---- Watchdog: drop to safe if GUI stops sending PING ----
const unsigned long HEARTBEAT_TIMEOUT_MS = 2000;  // GUI pings ~every 500 ms
unsigned long lastHeartbeat = 0;
bool linkAlive = true;

// ---- Serial input buffer ----
const uint8_t CMD_BUF_SIZE = 24;
char cmdBuf[CMD_BUF_SIZE];
uint8_t cmdLen = 0;

// ---- Held-signal states ----
bool sRun = false, sReady = false, sShutter = false;

void setAllSafe() {
  primeShotsLeft = 0;             // cancel any in-progress prime burst
  digitalWrite(PIN_FIRE, LOW);
  digitalWrite(PIN_SHUTTER, LOW); sShutter = false;
  digitalWrite(PIN_READY, LOW);   sReady = false;
  digitalWrite(PIN_RUN, LOW);     sRun = false;
}

void reportSignals() {
  Serial.print(F("SIG:RUN="));    Serial.print(sRun ? 1 : 0);
  Serial.print(F(",READY="));     Serial.print(sReady ? 1 : 0);
  Serial.print(F(",SHUTTER="));   Serial.print(sShutter ? 1 : 0);
  Serial.print(F(",MODE="));
  if (!sRun)            Serial.println(F("STOP"));
  else if (!sReady)     Serial.println(F("STANDBY"));
  else                  Serial.println(F("READY"));
}

void fireSingleShot() {
  if (!linkAlive) { Serial.println(F("ERR:LINK_STALE")); return; }
  // Flashlamp fire allowed when at least RUN+READY+SHUTTER are set.
  if (sRun && sReady && sShutter) {
    digitalWrite(PIN_FIRE, HIGH);
    delayMicroseconds(FIRE_PULSE_US);
    digitalWrite(PIN_FIRE, LOW);
    Serial.println(F("EVENT:FIRED"));
  } else {
    Serial.println(F("ERR:NOT_ARMED"));
  }
}

void startPrime() {
  if (!linkAlive)            { Serial.println(F("ERR:LINK_STALE")); return; }
  if (primeShotsLeft > 0)    { Serial.println(F("ERR:PRIME_BUSY")); return; }
  // Require RUN+READY; REFUSE if shutter open (prime with the beam contained).
  if (!(sRun && sReady))     { Serial.println(F("ERR:NEED_READY")); return; }
  if (sShutter)              { Serial.println(F("ERR:CLOSE_SHUTTER_FIRST")); return; }
  primeShotsLeft = PRIME_SHOTS;
  primeLastShot  = millis() - PRIME_INTERVAL_MS;  // allow first shot immediately
  Serial.print(F("EVENT:PRIME_START,")); Serial.println(PRIME_SHOTS);
}

// Called every loop(); fires one flashlamp pulse per interval until done.
void servicePrime() {
  if (primeShotsLeft <= 0) return;
  // Abort if arming was lost mid-burst (watchdog, ESTOP, READY_OFF, RUN_OFF).
  if (!(sRun && sReady)) {
    primeShotsLeft = 0;
    Serial.println(F("EVENT:PRIME_ABORT"));
    return;
  }
  if (millis() - primeLastShot >= PRIME_INTERVAL_MS) {
    digitalWrite(PIN_FIRE, HIGH);
    delayMicroseconds(FIRE_PULSE_US);
    digitalWrite(PIN_FIRE, LOW);
    primeLastShot = millis();
    primeShotsLeft--;
    Serial.print(F("SHOT_LEFT:")); Serial.println(primeShotsLeft);
    if (primeShotsLeft == 0) Serial.println(F("EVENT:PRIME_DONE"));
  }
}

void handleCommand(const char* cmd) {
  if (strcmp(cmd, "PING") == 0) { lastHeartbeat = millis(); linkAlive = true; return; }

  else if (strcmp(cmd, "ESTOP") == 0) {
    setAllSafe();
    Serial.println(F("EVENT:ESTOP"));
  }
  // ---- RUN/STOP ----
  else if (strcmp(cmd, "RUN_ON") == 0)  { digitalWrite(PIN_RUN, HIGH); sRun = true; }
  else if (strcmp(cmd, "RUN_OFF") == 0) { setAllSafe(); }   // dropping RUN collapses all

  // ---- STANDBY/READY ----
  else if (strcmp(cmd, "READY_ON") == 0) {
    if (sRun) { digitalWrite(PIN_READY, HIGH); sReady = true; }
    else { Serial.println(F("ERR:NEED_RUN")); return; }
  }
  else if (strcmp(cmd, "READY_OFF") == 0) {
    primeShotsLeft = 0;             // dropping READY cancels a prime burst
    digitalWrite(PIN_FIRE, LOW);
    digitalWrite(PIN_SHUTTER, LOW); sShutter = false;
    digitalWrite(PIN_READY, LOW);   sReady = false;
  }
  // ---- SHUTTER ----
  else if (strcmp(cmd, "SHUTTER_OPEN") == 0) {
    if (sRun && sReady) { digitalWrite(PIN_SHUTTER, HIGH); sShutter = true; }
    else { Serial.println(F("ERR:NEED_READY")); return; }
  }
  else if (strcmp(cmd, "SHUTTER_CLOSE") == 0) {
    digitalWrite(PIN_SHUTTER, LOW); sShutter = false;
  }
  // ---- FIRE (single) ----
  else if (strcmp(cmd, "FIRE") == 0) { fireSingleShot(); return; }

  // ---- PRIME (burst) ----
  else if (strcmp(cmd, "PRIME") == 0) { startPrime(); return; }

  else { Serial.println(F("ERR:UNKNOWN_CMD")); return; }

  reportSignals();
}

void setup() {
  pinMode(PIN_RUN, OUTPUT);     digitalWrite(PIN_RUN, LOW);
  pinMode(PIN_READY, OUTPUT);   digitalWrite(PIN_READY, LOW);
  pinMode(PIN_SHUTTER, OUTPUT); digitalWrite(PIN_SHUTTER, LOW);
  pinMode(PIN_FIRE, OUTPUT);    digitalWrite(PIN_FIRE, LOW);
  setAllSafe();

  Serial.begin(115200);
  lastHeartbeat = millis();
  reportSignals();
}

void loop() {
  servicePrime();   // advance the non-blocking prime burst (if running)

  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (cmdLen > 0) { cmdBuf[cmdLen] = '\0'; handleCommand(cmdBuf); cmdLen = 0; }
    } else if (cmdLen < CMD_BUF_SIZE - 1) {
      cmdBuf[cmdLen++] = c;
    }
  }

  if (millis() - lastHeartbeat > HEARTBEAT_TIMEOUT_MS) {
    if (sRun || sReady || sShutter) {
      setAllSafe();
      Serial.println(F("EVENT:WATCHDOG_SAFE"));
      reportSignals();
    }
    linkAlive = false;
    lastHeartbeat = millis();
  }
}
