/*
  Raising ABI: pixel face + MLX90614 + MPU6050 + humidifier + ERM motor.
  Board/core: Seeed XIAO RP2040, Earle Philhower Arduino-Pico.
  Shared Wire bus: D4/GP6 SDA, D5/GP7 SCL, 3.3V logic, common GND.
  Both identical eyes=0x3C, SSD1309 mouth=0x3D, MLX90614=0x5B,
  The 0x3D display contains ONLY the mouth and a short English status footer.
  MPU6050=0x69 (AD0 HIGH). No multiplexer or analog NTC required.
  Five states: SLEEPY, HAPPY (GOOD STATE), CRYING, HOT, COLD.
  Humidifier FET switch on D10/GP3: ON only while CRYING (including demo).
  DRV2605/DRV2605L at 0x5A: continuous ERM vibration while CRYING.
  Every cry episode lasts 5 seconds; actuator motion cannot extend it forever.
  Default: active-HIGH, suitable for a 3.3V-driven N-MOSFET low-side switch.
  Priority: strong shaking > hot/cold > gentle motion + normal temperature > sleepy.
  Starts in SENSOR mode. Serial115200: h=help, s=sensors, d=demo, b=new IR baseline.
  Live feedback every 500ms, plus an immediate message on state/reason changes.
  A one-line JSON [DATA] message every 100ms feeds the web app's baby monitor.
  Keep the IR sensor aimed at the room and the toy still for ~2 seconds at boot.
*/
#include <Arduino.h>
#include <Wire.h>
#include <U8g2lib.h>
#include <Adafruit_MLX90614.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_DRV2605.h>
#include <math.h>

// 0=SSD1306 128x64, 1=SH1106 128x64, 2=SSD1306 128x32.
#define EYE_DRIVER 0
// 0=SSD1309 NONAME2, 1=SSD1306, 2=SH1106 (all 128x64).
#define MOUTH_DRIVER 0
constexpr uint8_t EYES_ADDRESS = 0x3C, MOUTH_ADDRESS = 0x3D;
constexpr uint8_t PANEL_ADDRESSES[2] = {EYES_ADDRESS, MOUTH_ADDRESS};
constexpr uint8_t IR_ADDRESS = 0x5B, IMU_ADDRESS = 0x69;
// MLX90614 SMBus max=100kHz. Display objects MUST use this speed too.
constexpr uint32_t I2C_HZ = 100000;
constexpr bool START_IN_DEMO = false;
constexpr uint8_t HAPTIC_ADDRESS = 0x5A, MOTOR_STRENGTH = 127;
constexpr uint8_t MOTOR_CLAMP = 0x8B; // User-provided DRV2605L test: register 0x17.
constexpr uint8_t HUMIDIFIER_FET_PIN = D10; // XIAO D10 is GPIO3, not GPIO10.
// HIGH=ON for a non-inverting N-MOSFET low-side switch. No PWM is used.
// Set false only if the actual FET driver/module uses LOW=ON.
constexpr bool FET_ACTIVE_HIGH = true;
constexpr uint32_t IMU_INTERVAL_MS = 20, IR_INTERVAL_MS = 250;
constexpr uint32_t PRINT_INTERVAL_MS = 500, FRAME_INTERVAL_MS = 250;
constexpr uint32_t DATA_INTERVAL_MS = 100;
constexpr uint32_t RETRY_INTERVAL_MS = 3000;
constexpr uint32_t GENTLE_HOLD_MS = 600, CRY_HOLD_MS = 5000;
constexpr uint32_t MOTOR_SETTLE_MS = 500, DEMO_STATE_MS = 5000;
constexpr uint32_t IMU_FRESH_MS = 500, IR_FRESH_MS = 1500;
constexpr uint8_t BASELINE_SAMPLES = 8;
constexpr float GRAVITY = 9.80665f;

// Toy interaction thresholds. Tune using the serial readings.
constexpr float COLD_ON_C = 18.0f, COLD_OFF_C = 19.5f;
constexpr float HOT_ON_C = 34.0f, HOT_OFF_C = 32.5f;
// Baseline is informational only: absolute object temperature controls HOT/COLD.
constexpr float HAPPY_ACCEL_ON = 1.2f, HAPPY_ACCEL_OFF = 0.6f; // abs(|a|-1g), m/s^2
constexpr float HAPPY_SPIN_ON = 0.7f, HAPPY_SPIN_OFF = 0.35f;   // gyro magnitude, rad/s
constexpr float CRY_ACCEL = 5.0f, CRY_SPIN = 3.0f;

#if EYE_DRIVER == 0
using EyeDisplay = U8G2_SSD1306_128X64_NONAME_F_HW_I2C;
#elif EYE_DRIVER == 1
using EyeDisplay = U8G2_SH1106_128X64_NONAME_F_HW_I2C;
#elif EYE_DRIVER == 2
using EyeDisplay = U8G2_SSD1306_128X32_UNIVISION_F_HW_I2C;
#else
#error Unsupported EYE_DRIVER
#endif
#if MOUTH_DRIVER == 0
using MouthDisplay = U8G2_SSD1309_128X64_NONAME2_F_HW_I2C;
#elif MOUTH_DRIVER == 1
using MouthDisplay = U8G2_SSD1306_128X64_NONAME_F_HW_I2C;
#elif MOUTH_DRIVER == 2
using MouthDisplay = U8G2_SH1106_128X64_NONAME_F_HW_I2C;
#else
#error Unsupported MOUTH_DRIVER
#endif

EyeDisplay eyes(U8G2_R0, U8X8_PIN_NONE);
MouthDisplay mouth(U8G2_R0, U8X8_PIN_NONE);
// Separate pixel memory: never let the mouth reuse an eye image.
// Each allocation covers a full128x64 monochrome screen (128x32 eyes use512B).
uint8_t eyeFrameBuffer[1024];
uint8_t mouthFrameBuffer[1024];
U8G2 *panels[2] = {&eyes, &mouth};
bool panelReady[2] = {false, false};
bool displayAddressTest = false;
Adafruit_MLX90614 mlx;
Adafruit_MPU6050 mpu;
Adafruit_DRV2605 drv;

enum class Mood : uint8_t { SLEEPY, HAPPY, CRYING, HOT, COLD };
enum class Reason : uint8_t {
  DEMO, STRONG_MOTION, TOO_HOT, TOO_COLD, IMU_UNAVAILABLE,
  IMU_SETTLING, IR_UNAVAILABLE, GOOD_STATE, WEAK_OR_STILL
};
struct Reaction {
  uint32_t started = 0;
  uint16_t duration = 0;
  bool enabled = false;
};
Reaction crying;
bool cryMotionBlocked = false, cryFromDemo = false, cryOutputsStarted = false;
bool crySettling = false;
uint32_t motorStoppedAt = 0;
bool gentleMotion = false;
uint32_t lastGentle = 0;
bool demoMode = START_IN_DEMO;
uint32_t demoStarted = 0;
bool mlxReady = false, mpuReady = false, irValid = false, imuValid = false;
float objectC = NAN, ambientC = NAN, referenceC = NAN;
float baselineSum = 0.0f;
uint8_t baselineCount = 0;
bool referenceReady = false;
int8_t thermalState = 0, gazeX = 0;
float ax = NAN, ay = NAN, az = NAN, gx = NAN, gy = NAN, gz = NAN;
float acceleration = NAN, dynamicAccel = NAN, spin = NAN, mpuTemperatureC = NAN;
uint32_t lastIRGood = 0, lastIMUGood = 0, imuStarted = 0;
Mood currentMood = Mood::SLEEPY;
Reason currentReason = Reason::IMU_UNAVAILABLE;
bool mistOn = false; // Commanded output, not measured humidifier operation.
bool motorReady = false, motorOn = false; // Last acknowledged RTP command.

void initMistOutput() {
  // Preload OFF before enabling the output, before any USB/I2C startup waits.
  digitalWrite(HUMIDIFIER_FET_PIN, FET_ACTIVE_HIGH ? LOW : HIGH);
  pinMode(HUMIDIFIER_FET_PIN, OUTPUT);
  mistOn = false;
}
void updateMistOutput() {
  const bool shouldBeOn = currentMood == Mood::CRYING;
  if (shouldBeOn == mistOn) return;
  digitalWrite(HUMIDIFIER_FET_PIN, shouldBeOn == FET_ACTIVE_HIGH ? HIGH : LOW);
  mistOn = shouldBeOn;
}
void updateCryTiming(uint32_t now) {
  if (currentMood == Mood::CRYING && crying.enabled && !cryOutputsStarted) {
    // Start the full 5s AFTER both output commands have been issued.
    crying.started = now;
    cryOutputsStarted = true;
  }
  if (currentMood != Mood::CRYING && cryMotionBlocked && !crySettling) {
    // Start the cooldown only AFTER the actual OFF commands, even after a stall.
    motorStoppedAt = now;
    crySettling = true;
  }
}

const char *moodName(Mood m) {
  static const char *names[] = {"SLEEPY", "HAPPY", "CRYING", "HOT", "COLD"};
  return names[static_cast<uint8_t>(m)];
}
bool responds(uint8_t address) {
  Wire.beginTransmission(address);
  return Wire.endTransmission() == 0;
}
bool writeMotorValue(uint8_t value) {
  Wire.setClock(I2C_HZ);
  Wire.beginTransmission(HAPTIC_ADDRESS);
  Wire.write(DRV2605_REG_RTPIN);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}
void updateMotorOutput() {
  const bool shouldBeOn = currentMood == Mood::CRYING;
  if (!motorReady) { motorOn = false; return; }
  if (shouldBeOn == motorOn) return;
  if (!writeMotorValue(shouldBeOn ? MOTOR_STRENGTH : 0)) {
    motorReady = false; motorOn = false;
    return;
  }
  motorOn = shouldBeOn;
}
const char *motorCommandText() {
  return motorReady ? (motorOn ? "ON" : "OFF") : "UNAVAILABLE";
}
void tryInitMotor() {
  if (motorReady) return;
  Wire.setClock(I2C_HZ);
  motorReady = drv.begin(&Wire);
  motorOn = false;
  if (motorReady) {
    drv.useERM();
    drv.stop();
    // User-provided motor setup: ERM open-loop, clamp 0x8B, RTP strength 127.
    // Also enforce signed RTP so that zero always means zero drive.
    const uint8_t control3 = drv.readRegister8(DRV2605_REG_CONTROL3);
    drv.writeRegister8(DRV2605_REG_CONTROL3, (control3 | 0x20) & 0xF7);
    drv.writeRegister8(DRV2605_REG_CLAMPV, MOTOR_CLAMP);
    drv.setRealtimeValue(0);
    drv.setMode(DRV2605_MODE_REALTIME);
    motorReady = writeMotorValue(0);
  }
  Wire.setClock(I2C_HZ);
  Serial.println(motorReady ? "DRV2605 ERM ready at 0x5A; motor OFF"
                           : "DRV2605 missing/config failed at 0x5A; retrying later");
}
void trigger(Reaction &event, uint32_t now, uint16_t duration) {
  event.started = now; event.duration = duration; event.enabled = true;
}
bool active(Reaction &event, uint32_t now) {
  if (event.enabled && now - event.started >= event.duration) event.enabled = false;
  return event.enabled;
}
void clearMotionReactions() {
  // Sensor failures/reinitialization must not cut short a latched 5s cry.
  gentleMotion = false;
}
void resetTemperatureReference() {
  referenceReady = false; referenceC = NAN;
  baselineCount = 0; baselineSum = 0.0f;
}
void scanBus() {
  Serial.println("I2C scan (expected 0x3C, 0x3D, 0x5A, 0x5B, 0x69):");
  for (uint8_t address = 1; address < 127; ++address) {
    if (!responds(address)) continue;
    Serial.print("  0x"); if (address < 16) Serial.print('0');
    Serial.println(address, HEX);
  }
  Serial.println("0x3C represents BOTH eyes if they are identical and share the address.");
}
void initPanel(uint8_t index) {
  const uint8_t address = PANEL_ADDRESSES[index];
  panelReady[index] = responds(address);
  Serial.print(index == 0 ? "EYES at0x3C " : "MOUTH+STATUS at0x3D ");
  Serial.println(panelReady[index] ? "ACK" : "MISSING");
  if (!panelReady[index]) return;
  panels[index]->setI2CAddress(address << 1);
  panels[index]->setBusClock(I2C_HZ);
  panels[index]->begin();
  panels[index]->setContrast(96);
}
void displayDiagnostics() {
  scanBus();
  for (uint8_t i = 0; i < 2; ++i) {
    Serial.print(i == 0 ? "EYES0x3C " : "MOUTH0x3D ");
    Serial.print(responds(PANEL_ADDRESSES[i]) ? "ACK" : "NO_ACK");
    Serial.print(" initialized="); Serial.println(panelReady[i] ? "YES" : "NO");
  }
  Serial.print("Framebuffers=");
  Serial.println(eyes.getBufferPtr() != mouth.getBufferPtr() ? "SEPARATE" : "SHARED");
}
void displayPower(uint8_t address, bool on) {
  Wire.setClock(I2C_HZ);
  Wire.beginTransmission(address);
  Wire.write(0x00); Wire.write(on ? 0xAF : 0xAE);
  const uint8_t status = Wire.endTransmission();
  Serial.print("RAW DISPLAY0x"); Serial.print(address, HEX);
  Serial.print(on ? " ON status=" : " OFF status="); Serial.println(status);
}
void resumeDisplays() {
  if (displayAddressTest) {
    displayPower(EYES_ADDRESS, true); displayPower(MOUTH_ADDRESS, true);
    displayAddressTest = false;
  }
}
void tryInitSensors(uint32_t now) {
  Wire.setClock(I2C_HZ);
  if (!mlxReady) {
    mlxReady = mlx.begin(IR_ADDRESS, &Wire);
    Serial.println(mlxReady ? "MLX90614 ready at 0x5B" : "MLX90614 missing at 0x5B");
  }
  if (!mpuReady) {
    mpuReady = mpu.begin(IMU_ADDRESS, &Wire);
    if (mpuReady) {
      mpu.setAccelerometerRange(MPU6050_RANGE_8_G);
      mpu.setGyroRange(MPU6050_RANGE_500_DEG);
      mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);
      // These ranges also determine the checked burst-read conversion below.
      mpuReady = mpu.getAccelerometerRange() == MPU6050_RANGE_8_G &&
                 mpu.getGyroRange() == MPU6050_RANGE_500_DEG;
      imuStarted = millis(); imuValid = false;
      clearMotionReactions();
    }
    Serial.println(mpuReady ? "MPU6050 ready at 0x69" : "MPU6050 missing/config failed at 0x69");
  }
  Wire.setClock(I2C_HZ);
  (void)now;
}

// BEGIN SENSOR BEHAVIOR: isolated from drawing so it can be replay-checked.
bool cryMotionGuard(uint32_t now) {
  if (!cryMotionBlocked) return false;
  if (!crySettling) return true;
  if (now - motorStoppedAt < MOTOR_SETTLE_MS) return true;
  cryMotionBlocked = false;
  crySettling = false;
  return false;
}
void startCryEpisode(uint32_t now, bool fromDemo = false) {
  if (crying.enabled && (!cryOutputsStarted || active(crying, now))) return;
  trigger(crying, now, CRY_HOLD_MS);
  cryMotionBlocked = true;
  cryFromDemo = fromDemo;
  cryOutputsStarted = false;
  crySettling = false;
  gentleMotion = false;
}
void updateThermalState() {
  // Hysteresis avoids flickering at the temperature boundaries.
  if (objectC >= HOT_ON_C) thermalState = 1;
  else if (objectC <= COLD_ON_C) thermalState = -1;
  else if (thermalState == 1 && objectC < HOT_OFF_C) thermalState = 0;
  else if (thermalState == -1 && objectC > COLD_OFF_C) thermalState = 0;
}
void acceptIRSample(float object, float ambient, uint32_t now) {
  // Deliberate prototype range; reject NAN, extreme/error-bit conversions.
  if (!isfinite(object) || !isfinite(ambient) || object < -40.0f ||
      object > 125.0f || ambient < -40.0f || ambient > 125.0f) {
    irValid = false; thermalState = 0;
    return;
  }
  objectC = irValid ? objectC + 0.35f * (object - objectC) : object;
  ambientC = ambient; irValid = true; lastIRGood = now;
  if (!referenceReady) {
    baselineSum += object; ++baselineCount;
    if (baselineCount >= BASELINE_SAMPLES) {
      referenceC = baselineSum / baselineCount;
      referenceReady = true;
    }
  }
  updateThermalState();
}
void acceptMotionSample(float x, float y, float z, float rx, float ry,
                        float rz, float dieC, uint32_t now) {
  if (!isfinite(x) || !isfinite(y) || !isfinite(z) || !isfinite(rx) ||
      !isfinite(ry) || !isfinite(rz) || !isfinite(dieC)) {
    imuValid = false; clearMotionReactions(); gazeX = 0; return;
  }
  // Clear gentle-motion history across gaps; keep a started cry episode.
  if (!imuValid || now - lastIMUGood >= IMU_FRESH_MS) clearMotionReactions();
  ax = x; ay = y; az = z; gx = rx; gy = ry; gz = rz; mpuTemperatureC = dieC;
  acceleration = sqrtf(x*x + y*y + z*z);
  dynamicAccel = fabsf(acceleration - GRAVITY);
  spin = sqrtf(rx*rx + ry*ry + rz*rz);
  imuValid = true; lastIMUGood = now;
  // Mounting-dependent eye direction; swap/sign-change x if your IMU is rotated.
  gazeX = x > 3.0f ? 1 : (x < -3.0f ? -1 : 0);
  if (now - imuStarted < 250u) { clearMotionReactions(); return; }
  // Still collect telemetry, but do not mistake the ERM's vibration for shaking.
  if (demoMode || cryMotionGuard(now)) { gentleMotion = false; return; }
  if (dynamicAccel >= CRY_ACCEL || spin >= CRY_SPIN) {
    startCryEpisode(now);
    return;
  }
  const float accelThreshold = gentleMotion ? HAPPY_ACCEL_OFF : HAPPY_ACCEL_ON;
  const float spinThreshold = gentleMotion ? HAPPY_SPIN_OFF : HAPPY_SPIN_ON;
  if (dynamicAccel >= accelThreshold || spin >= spinThreshold) {
    gentleMotion = true; lastGentle = now;
  } else if (gentleMotion && now - lastGentle >= GENTLE_HOLD_MS) {
    gentleMotion = false;
  }
}
Mood chooseMood(uint32_t now) {
  // A started episode completes even if samples go stale or modes change.
  if (crying.enabled && (!cryOutputsStarted || active(crying, now))) {
    currentReason = cryFromDemo ? Reason::DEMO : Reason::STRONG_MOTION;
    return Mood::CRYING;
  }
  if (demoMode) {
    currentReason = Reason::DEMO;
    const Mood demoMood = static_cast<Mood>(((now - demoStarted) / DEMO_STATE_MS) % 5u);
    if (demoMood == Mood::CRYING) startCryEpisode(now, true);
    return demoMood;
  }
  const bool motionFresh = imuValid && now - lastIMUGood < IMU_FRESH_MS;
  const bool temperatureFresh = irValid && now - lastIRGood < IR_FRESH_MS;
  if (temperatureFresh && thermalState < 0) {
    currentReason = Reason::TOO_COLD; return Mood::COLD;
  }
  if (temperatureFresh && thermalState > 0) {
    currentReason = Reason::TOO_HOT; return Mood::HOT;
  }
  if (!motionFresh) {
    currentReason = Reason::IMU_UNAVAILABLE; return Mood::SLEEPY;
  }
  if (now - imuStarted < 250u) {
    currentReason = Reason::IMU_SETTLING; return Mood::SLEEPY;
  }
  if (!temperatureFresh) {
    currentReason = Reason::IR_UNAVAILABLE; return Mood::SLEEPY;
  }
  if (gentleMotion && now - lastGentle < GENTLE_HOLD_MS) {
    currentReason = Reason::GOOD_STATE; return Mood::HAPPY;
  }
  currentReason = Reason::WEAK_OR_STILL;
  return Mood::SLEEPY;
}
// END SENSOR BEHAVIOR.

void readTemperature(uint32_t now) {
  if (!mlxReady) return;
  Wire.setClock(I2C_HZ);
  // Object temperature drives the face. Ambient is the MLX package temperature.
  const float object = mlx.readObjectTempC();
  const float ambient = mlx.readAmbientTempC();
  acceptIRSample(object, ambient, now);
  if (!irValid) mlxReady = false;
}
int16_t signedWord(const uint8_t *p) {
  const uint16_t word = (uint16_t(p[0]) << 8) | p[1];
  return word < 0x8000u ? int16_t(word) : int16_t(int32_t(word) - 65536);
}
void readMotion(uint32_t now) {
  if (!mpuReady) return;
  Wire.setClock(I2C_HZ);
  Wire.beginTransmission(IMU_ADDRESS); Wire.write(0x3B);
  bool ok = Wire.endTransmission(false) == 0;
  uint8_t data[14];
  if (ok) ok = Wire.requestFrom(IMU_ADDRESS, size_t(14), true) == 14;
  if (ok) {
    for (uint8_t i = 0; i < 14; ++i) {
      if (!Wire.available()) { ok = false; break; }
      data[i] = uint8_t(Wire.read());
    }
  }
  if (!ok) {
    mpuReady = imuValid = false; gazeX = 0; clearMotionReactions(); return;
  }
  // Checked 14-byte burst, unlike getEvent() which hides transport failures.
  // These constants require the +/-8g, +/-500deg/s setup above.
  constexpr float ACCEL_SCALE = GRAVITY / 4096.0f;
  constexpr float GYRO_SCALE = 0.0174532925199433f / 65.5f;
  acceptMotionSample(signedWord(data) * ACCEL_SCALE,
      signedWord(data+2) * ACCEL_SCALE, signedWord(data+4) * ACCEL_SCALE,
      signedWord(data+8) * GYRO_SCALE, signedWord(data+10) * GYRO_SCALE,
      signedWord(data+12) * GYRO_SCALE, signedWord(data+6) / 340.0f + 36.53f, now);
}

// Crisp OLED sprites: eyes use 16 x 8 logical cells; the mouth uses 16 x 6.
// At 128 x 64 each cell is a solid 8 x 8 pixel block. The mouth reserves
// physical rows 48..63 for its English status caption. No blur or dithering.
namespace AbiBigPixels {
uint16_t rows[8];

void clear() {
  for (uint8_t y = 0; y < 8; ++y) rows[y] = 0;
}
void box(int x, int y, int width, int height, bool on = true) {
  for (int yy = y; yy < y + height; ++yy) {
    for (int xx = x; xx < x + width; ++xx) {
      if (xx < 0 || xx >= 16 || yy < 0 || yy >= 8) continue;
      const uint16_t bit = uint16_t(1u << xx);
      if (on) rows[yy] |= bit;
      else rows[yy] &= uint16_t(~bit);
    }
  }
}
void render(U8G2 &d, uint8_t logicalRows = 8) {
  d.clearBuffer();
  d.setDrawColor(1);
  const int blockW = d.getDisplayWidth() / 16;
  const int blockH = d.getDisplayHeight() / 8;
  if (blockW < 1 || blockH < 1) return;
  if (logicalRows > 8) logicalRows = 8;
  for (uint8_t y = 0; y < logicalRows; ++y) {
    for (uint8_t x = 0; x < 16; ++x) {
      if (rows[y] & uint16_t(1u << x))
        d.drawBox(x * blockW, y * blockH, blockW, blockH);
    }
  }
}
} // namespace AbiBigPixels

void drawEyeFrame(U8G2 &d, Mood mood, bool blink, int8_t gazeX, uint32_t now) {
  using namespace AbiBigPixels;
  clear();
  const int gaze = gazeX < -1 ? -1 : (gazeX > 1 ? 1 : gazeX);
  if (blink) {
    box(3, 3, 1, 1); box(4, 4, 8, 1); box(12, 3, 1, 1);
  } else if (mood == Mood::SLEEPY) {
    box(3, 4, 3, 1); box(6, 5, 4, 1); box(10, 4, 3, 1);
  } else if (mood == Mood::HAPPY) {
    box(7, 2, 2, 1); box(5, 3, 2, 1); box(9, 3, 2, 1);
    box(3, 4, 2, 2); box(11, 4, 2, 2);
  } else if (mood == Mood::CRYING) {
    box(3, 2, 2, 1); box(5, 3, 2, 1); box(7, 4, 2, 1);
    box(9, 3, 2, 1); box(11, 2, 2, 1);
    const int fall = (now / 300u) % 2u;
    box(12, 4 + fall, 1, 1); box(11, 5 + fall, 2, 2);
  } else if (mood == Mood::HOT || mood == Mood::COLD) {
    box(5, 3, 6, 4); box(4, 4, 8, 2);
    box(6 + gaze, 4, 4, 2, false);
    if (mood == Mood::HOT) {
      box(4, 0, 4, 1); box(8, 1, 4, 1);
      box(13, 1, 1, 1); box(13, 2, 2, 2); // Sweat drop.
    } else {
      box(4, 1, 4, 1); box(8, 0, 4, 1);
    }
  }
  render(d);
}

const char *mouthStatus(Mood mood) {
  switch (mood) {
    case Mood::HAPPY: return "GOOD STATE";
    case Mood::COLD: return "TOO COLD";
    case Mood::HOT: return "TOO HOT";
    case Mood::CRYING: return "CRYING";
    case Mood::SLEEPY: return "SLEEPY";
    default: return "SLEEPY";
  }
}

void drawMouthFrame(U8G2 &d, Mood mood, uint32_t now) {
  using namespace AbiBigPixels;
  clear();
  // Only logical rows 0..5 are used: 48 px of mouth above a 16 px caption.
  if (mood == Mood::HAPPY) {
    box(3, 0, 10, 3); box(4, 3, 8, 1);
    box(5, 4, 6, 1); box(6, 5, 4, 1);
    box(4, 1, 8, 2, false); box(5, 3, 6, 1, false);
  } else if (mood == Mood::CRYING) {
    box(5, 0, 6, 1); box(3, 1, 10, 1); box(2, 2, 12, 4);
    box(4, 2, 8, 3, false); // Arched upper lip, wide flat lower lip.
  } else if (mood == Mood::HOT) {
    box(4, 0, 8, 4); box(3, 1, 10, 2);
    box(4, 1, 8, 2, false);
    box(8, 2, 3, 3); box(9, 5, 1, 1); // Tongue below the lower lip.
    box(9, 2, 1, 2, false); // Groove is also made from whole coarse blocks.
  } else if (mood == Mood::COLD) {
    const int shiver = ((now / 180u) % 2u) ? 1 : -1;
    for (int x = 2; x <= 12; x += 2)
      box(x + shiver, ((x / 2) % 2) ? 2 : 3, 2, 1);
  } else if (mood == Mood::SLEEPY) {
    box(7, 1, 2, 4); box(6, 2, 4, 2); box(7, 2, 2, 2, false);
  }
  render(d, 6);
  // Footer is drawn ONLY on the large mouth panel, after the coarse sprite.
  d.setFont(u8g2_font_6x10_tr);
  d.setFontMode(1);
  d.setFontPosBaseline();
  d.setDrawColor(1);
  const char *caption = mouthStatus(mood);
  const int captionWidth = d.getStrWidth(caption);
  const int captionX = (d.getDisplayWidth() - captionWidth) / 2;
  d.drawStr(captionX < 0 ? 0 : captionX, d.getDisplayHeight() - 3, caption);
}


void serviceDisplay(uint32_t now) {
  if (displayAddressTest) return;
  static bool sending = false;
  static uint8_t panel = 0, tileRow = 0;
  static uint32_t lastFrame = 0;
  static Mood frameMood = Mood::SLEEPY;
  static bool frameBlink = false;
  static int8_t frameLook = 0;
  static uint32_t frameTime = 0;
  if (!sending) {
    if (now - lastFrame < FRAME_INTERVAL_MS) return;
    lastFrame = now;
    frameMood = currentMood; frameTime = now;
    frameBlink = now % 4300u < 300u && currentMood != Mood::SLEEPY;
    frameLook = (!demoMode && imuValid) ? gazeX : 0;
    sending = true; panel = tileRow = 0;
  }
  while (panel < 2 && !panelReady[panel]) { ++panel; tileRow = 0; }
  if (panel >= 2) { sending = false; return; }
  // Each physical address has its own framebuffer and its own renderer.
  if (tileRow == 0) {
    if (panel == 0) drawEyeFrame(eyes, frameMood, frameBlink, frameLook, frameTime);
    else drawMouthFrame(mouth, frameMood, frameTime);
  }
  // One 8-physical-pixel row takes ~13ms at100kHz, then return to the sensors.
  panels[panel]->setI2CAddress(PANEL_ADDRESSES[panel] << 1);
  panels[panel]->updateDisplayArea(0, tileRow, panels[panel]->getDisplayWidth()/8, 1);
  if (++tileRow >= panels[panel]->getDisplayHeight()/8) { ++panel; tileRow = 0; }
  if (panel >= 2) sending = false;
}
const char *feedbackText(Reason reason) {
  switch (reason) {
    case Reason::DEMO: return "Demo only; send s for real sensor reactions";
    case Reason::STRONG_MOTION: return "Strong motion: fixed 5s cry; actuator vibration ignored";
    case Reason::TOO_HOT: return "Too hot; aim IR at a cooler surface";
    case Reason::TOO_COLD: return "Too cold; aim IR at a warmer surface";
    case Reason::IMU_UNAVAILABLE: return "MPU missing/invalid/stale; check 0x69 wiring";
    case Reason::IMU_SETTLING: return "MPU starting; keep still briefly";
    case Reason::IR_UNAVAILABLE: return "IR missing/invalid/stale; check 0x5B wiring";
    case Reason::GOOD_STATE: return "Good state: gentle motion + comfortable temperature";
    default: return "Still or too weak; gently rock me to wake up";
  }
}
const char *motionFeedback(uint32_t now) {
  if (!imuValid || now - lastIMUGood >= IMU_FRESH_MS) return "UNAVAILABLE";
  if (now - imuStarted < 250u) return "SETTLING";
  if (active(crying, now)) return "CRY_HOLD_5S";
  if (cryMotionGuard(now)) return "MOTOR_SETTLING";
  if (dynamicAccel >= CRY_ACCEL || spin >= CRY_SPIN) return "TOO_STRONG";
  if (gentleMotion && now - lastGentle < GENTLE_HOLD_MS) return "GENTLE";
  return "STILL/WEAK";
}
void printHelp() {
  if (!Serial) return;
  Serial.println("\nABI FACE + MIST + ERM MOTOR | 115200 baud | live every 500ms");
  Serial.println("Priority: CRYING > HOT/COLD > HAPPY (GOOD STATE) > SLEEPY.");
  Serial.println("Still/weak=SLEEPY; gentle+normal temperature=HAPPY; strong=CRYING.");
  Serial.print("HAPPY enter: dyn>="); Serial.print(HAPPY_ACCEL_ON, 2);
  Serial.print("m/s2 OR spin>="); Serial.print(HAPPY_SPIN_ON, 2); Serial.println("rad/s.");
  Serial.print("HAPPY release: BOTH dyn<"); Serial.print(HAPPY_ACCEL_OFF, 2);
  Serial.print(" AND spin<"); Serial.print(HAPPY_SPIN_OFF, 2);
  Serial.print(" for "); Serial.print(GENTLE_HOLD_MS); Serial.println("ms.");
  Serial.print("CRYING: dyn>="); Serial.print(CRY_ACCEL, 2);
  Serial.print("m/s2 OR spin>="); Serial.print(CRY_SPIN, 2);
  Serial.print("rad/s; hold "); Serial.print(CRY_HOLD_MS); Serial.println("ms after output commands start.");
  Serial.println("A started cry finishes its 5s even if readings fail or s/d changes mode.");
  Serial.println("No motion retrigger during cry; ignore another 500ms after motor stops.");
  Serial.print("COLD: obj<="); Serial.print(COLD_ON_C, 1);
  Serial.print("C; release above "); Serial.print(COLD_OFF_C, 1); Serial.println("C.");
  Serial.print("HOT: obj>="); Serial.print(HOT_ON_C, 1);
  Serial.print("C; release below "); Serial.print(HOT_OFF_C, 1); Serial.println("C.");
  Serial.println("obj=smoothed IR surface temperature; amb=MLX package temperature.");
  Serial.println("dyn=abs(|a|-9.80665); spin=|gyro|; die=MPU chip temperature.");
  Serial.println("IR ref is readout only. HAPPY requires fresh IR and MPU readings.");
  Serial.println("Humidifier FET D10/GP3: ON only in CRYING, including demo; OFF otherwise.");
  Serial.print("FET ON level: "); Serial.println(FET_ACTIVE_HIGH ? "HIGH (3.3V)" : "LOW (0V)");
  Serial.println("mist_cmd is the FET control command, not confirmation of mist output.");
  Serial.println("DRV2605 0x5A: ERM real-time vibration during CRYING; MLX90614 is at 0x5B.");
  Serial.println("Motor configuration: ERM open-loop, CLAMP[0x17]=0x8B, RTP strength=127.");
  Serial.println("motor_cmd is the last acknowledged I2C command, not measured vibration.");
  Serial.println("h=help, s=sensors/restore displays, d=5-state demo, b=new IR reference.");
  Serial.println("i=I2C diagnostics, 1=0x3C off, 2=0x3D off, s=restore.");
  Serial.println("OLED READY is address/init status; check graphics on the actual screens.\n");
}
void serialControls(uint32_t now) {
  while (Serial.available()) {
    const char c = Serial.read();
    if (c == 'h' || c == 'H' || c == '?') printHelp();
    if (c == 'i' || c == 'I') displayDiagnostics();
    if (c == '1') {
      displayAddressTest = true;
      Serial.println("TEST1: ONLY0x3C OFF. Which physical screens turned off?");
      displayPower(EYES_ADDRESS, false);
    }
    if (c == '2') {
      displayAddressTest = true;
      displayPower(EYES_ADDRESS, true);
      Serial.println("TEST2: ONLY0x3D OFF. Which physical screens turned off?");
      displayPower(MOUTH_ADDRESS, false);
    }
    if (c == 'd' || c == 'D') {
      resumeDisplays(); demoMode = true; demoStarted = now; clearMotionReactions();
      Serial.println("[MODE] DEMO: five expressions, 5 seconds each; an active cry finishes first.");
    }
    if (c == 's' || c == 'S') {
      resumeDisplays();
      demoMode = false; clearMotionReactions();
      Serial.println("[MODE] SENSOR: real reactions; an active 5s cry finishes first.");
    }
    if (c == 'b' || c == 'B') {
      resetTemperatureReference();
      Serial.println("[IR] Collecting 8 valid reference samples (~2s); aim at room. Thresholds unchanged.");
    }
  }
  (void)now;
}
// The two web-app monitor checks: normal temperature, and gentle rocking
// (motion, but not the strong shaking that starts a cry).
bool temperatureOk(uint32_t now) {
  return irValid && now - lastIRGood < IR_FRESH_MS && thermalState == 0;
}
bool cryActive(uint32_t now) {
  return crying.enabled && (!cryOutputsStarted || active(crying, now));
}
bool rockedGently(uint32_t now) {
  return imuValid && now - lastIMUGood < IMU_FRESH_MS && now - imuStarted >= 250u &&
         !cryActive(now) && gentleMotion && now - lastGentle < GENTLE_HOLD_MS;
}
void printJsonFloat(const char *key, float value) {
  Serial.print(",\""); Serial.print(key); Serial.print("\":");
  if (isfinite(value)) Serial.print(value, 2); else Serial.print("null");
}
void printJsonBool(const char *key, bool value) {
  Serial.print(",\""); Serial.print(key); Serial.print("\":");
  Serial.print(value ? "true" : "false");
}
// Machine-readable line for the web app (app.py reads lines starting [DATA]).
void reportData(uint32_t now) {
  if (!Serial) return;
  const bool irFresh = irValid && now - lastIRGood < IR_FRESH_MS;
  const bool imuFresh = imuValid && now - lastIMUGood < IMU_FRESH_MS;
  Serial.print("[DATA] {\"t\":"); Serial.print(now);
  Serial.print(",\"mode\":\""); Serial.print(demoMode ? "DEMO" : "SENSOR");
  Serial.print("\",\"mood\":\""); Serial.print(moodName(currentMood)); Serial.print('"');
  printJsonFloat("obj", irFresh ? objectC : NAN);
  printJsonBool("temp_ok", temperatureOk(now));
  printJsonFloat("dyn", imuFresh ? dynamicAccel : NAN);
  printJsonFloat("spin", imuFresh ? spin : NAN);
  printJsonBool("rocked", rockedGently(now));
  printJsonBool("crying", cryActive(now));
  Serial.println('}');
}
void report(uint32_t now) {
  if (!Serial) return;
  Serial.print("[LIVE t="); Serial.print(now / 1000.0f, 1); Serial.print("s] ");
  Serial.print(demoMode ? "DEMO " : "SENSOR "); Serial.print(moodName(currentMood));
  Serial.print(" | mist_cmd="); Serial.print(mistOn ? "ON" : "OFF");
  Serial.print(" | motor_cmd="); Serial.print(motorCommandText());
  Serial.print(" | DRV="); Serial.print(motorReady ? "READY" : "MISSING/ERROR");
  const uint32_t cryElapsed = now - crying.started;
  Serial.print(" | cry_ms_left=");
  Serial.print(crying.enabled && cryElapsed < CRY_HOLD_MS ? CRY_HOLD_MS - cryElapsed : 0);
  Serial.print(" | feedback="); Serial.print(feedbackText(currentReason));
  Serial.print(" | motion="); Serial.print(motionFeedback(now));
  Serial.print(" | IR=");
  if (irValid && now - lastIRGood < IR_FRESH_MS) {
    Serial.print(thermalState > 0 ? "HOT " : (thermalState < 0 ? "COLD " : "NORMAL "));
    Serial.print("OK obj="); Serial.print(objectC, 2);
    Serial.print("C amb="); Serial.print(ambientC, 2); Serial.print("C ref=");
    if (referenceReady) Serial.print(referenceC, 2); else Serial.print("calibrating");
  } else Serial.print("MISSING/INVALID/STALE");
  Serial.print(" | MPU=");
  if (imuValid && now - lastIMUGood < IMU_FRESH_MS) {
    Serial.print("OK a="); Serial.print(ax, 2); Serial.print(',');
    Serial.print(ay, 2); Serial.print(','); Serial.print(az, 2);
    Serial.print("m/s2 dyn="); Serial.print(dynamicAccel, 2);
    Serial.print("m/s2 spin="); Serial.print(spin, 2); Serial.print("rad/s");
    Serial.print(" gyro="); Serial.print(gx, 2); Serial.print(',');
    Serial.print(gy, 2); Serial.print(','); Serial.print(gz, 2);
    Serial.print("rad/s die="); Serial.print(mpuTemperatureC, 1); Serial.print('C');
  } else Serial.print("MISSING/INVALID/STALE");
  Serial.print(" | OLED3C="); Serial.print(panelReady[0] ? "READY" : "MISSING");
  Serial.print(" OLED3D="); Serial.print(panelReady[1] ? "READY" : "MISSING");
  Serial.println();
}
void setup() {
  initMistOutput();
  Serial.begin(115200);
  // XIAO variant maps GP6/GP7's I2C1 peripheral to Wire. Do NOT use Wire1 here.
  Wire.setSDA(D4); Wire.setSCL(D5); Wire.begin();
  Wire.setClock(I2C_HZ); Wire.setTimeout(25);
  // Stop a previously latched RTP output before waiting for USB or drawing.
  tryInitMotor();
  const uint32_t start = millis();
  while (!Serial && millis() - start < 3000u) delay(10);
  scanBus();
  // The installed U8g2 default constructors can share static pixel storage.
  // Assign distinct storage before begin()/clearDisplay().
  eyes.getU8g2()->tile_buf_ptr = eyeFrameBuffer;
  mouth.getU8g2()->tile_buf_ptr = mouthFrameBuffer;
  initPanel(0); initPanel(1);
  tryInitSensors(millis());
  demoStarted = millis();
  printHelp();
  Serial.println("[MODE] SENSOR: starts SLEEPY; gently rock at normal temperature for GOOD STATE.");
  Serial.println("Larger16x8 pixel grid; hard edges; no blur/dithering.");
  Serial.println("DISPLAY ROUTING:0x3C=EYES;0x3D=MOUTH+ENGLISH STATUS.");
  Serial.println("Display diagnostics:i=scan,1=0x3C off,2=0x3D off,s=restore.");
}
void loop() {
  static uint32_t lastIMU = 0, lastIR = 0, lastPrint = 0, lastRetry = 0, lastData = 0;
  uint32_t now = millis();
  serialControls(now);
  if (now - lastIMU >= IMU_INTERVAL_MS) { lastIMU = now; readMotion(now); }
  now = millis();
  if (now - lastIR >= IR_INTERVAL_MS) { lastIR = now; readTemperature(now); }
  now = millis();
  if (now - lastRetry >= RETRY_INTERVAL_MS) {
    lastRetry = now;
    if (!mlxReady || !mpuReady) tryInitSensors(now);
    if (motorReady && !responds(HAPTIC_ADDRESS)) { motorReady = false; motorOn = false; }
    if (!motorReady) tryInitMotor();
    if (!displayAddressTest) {
      for (uint8_t i = 0; i < 2; ++i) {
        const bool present = responds(PANEL_ADDRESSES[i]);
        if (!present) panelReady[i] = false;
        else if (!panelReady[i]) initPanel(i);
      }
    }
  }
  now = millis();
  const Mood previousMood = currentMood;
  const Reason previousReason = currentReason;
  currentMood = chooseMood(now);
  updateMistOutput();
  updateMotorOutput();
  updateCryTiming(millis());
  static bool firstStateReport = true;
  if (Serial && (firstStateReport || currentMood != previousMood || currentReason != previousReason)) {
    Serial.print("[STATE] ");
    Serial.print(firstStateReport ? "START" : moodName(previousMood));
    Serial.print(" -> "); Serial.print(moodName(currentMood));
    Serial.print(" | mist_cmd="); Serial.print(mistOn ? "ON" : "OFF");
    Serial.print(" | motor_cmd="); Serial.print(motorCommandText());
    Serial.print(" | feedback="); Serial.println(feedbackText(currentReason));
    firstStateReport = false;
  }
  serviceDisplay(now);
  now = millis();
  if (now - lastPrint >= PRINT_INTERVAL_MS) { lastPrint = now; report(now); }
  if (now - lastData >= DATA_INTERVAL_MS) { lastData = now; reportData(now); }
  delay(1);
}
