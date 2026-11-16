#include "lock_fastlink.h"
#include "esphome/core/log.h"
#include "esphome/core/hal.h"

#ifdef USE_ESP32
#include "esp_gap_ble_api.h"
#include "esp_bt.h"
#include "esp_bt_main.h"
#include <esp_coexist.h>
#include "esp_wifi.h"
#include "esp_timer.h"
extern "C" {
#include "common/bt_target.h"
#include "l2c_int.h"
}

// Radio-sharing table used while the BLE "mesh config" status bit is set and the Wi-Fi station is
// connected.  libcoexist.a defines it weak; this strong definition replaces it so the share can be
// chosen.  Layout (read from the library, esp-coex-lib as pinned by ESP-IDF 5.5.6): number of phases,
// period multiplier (x beacon interval), then per phase {percent, Wi-Fi flags, 0, 0}.  The stock
// table is {2, 1, 45, 0x03, 0, 0, 55, 0, 0, 0}: Wi-Fi the first 45 %, Bluetooth the last 55 %.
extern "C" {
uint8_t coex_schm_ble_mesh_config_wifi_conn[10] = {2, 1, 45, 0x03, 0, 0, 55, 0, 0, 0};
}

namespace esphome {
namespace lock_fastlink {

static const char *const TAG = "lock_fastlink";

// --- Link-setup interval override -------------------------------------------
// ESPHome opens every proxy connection at a 7.5 ms interval
// (esp_ble_gap_set_prefer_conn_params(FAST) right before esp_ble_gattc_open).
// The lock then misses the first connection event and the controller gives up
// with 0x3E "Connection Failed to be Established"; phones open at 30-50 ms and
// connect fine.  The linker wraps that call (-Wl,--wrap) so that, for the
// lock's address only, the link is set up at `connect_min..connect_max`.
static uint8_t g_lock_bda[6];
static bool g_override = false;
static uint16_t g_conn_min = 24;  // 30 ms
static uint16_t g_conn_max = 40;  // 50 ms
static uint16_t g_conn_timeout = 2000;  // 20 s: survive lost packets right after setup
static volatile uint32_t g_override_count = 0;
static volatile uint16_t g_target = 24;  // hold target chosen by the user (select / YAML)
static volatile uint32_t g_update_override_count = 0;
static const uint32_t REQUEST_RETRY_MS = 4000;  // one update procedure at 400 ms takes ~2.5 s
static const uint32_t COEX_REASSERT_MS = 1000;  // ESPHome resets coexistence to "balanced" after each connect
// Supervision timeout for our fast-interval updates (10 ms units).  At 30 ms a
// 20 s timeout rides out long bursts of lost packets instead of dropping the link.
static const uint16_t FAST_SUPERVISION = 2000;
static const uint16_t MIN_SUPERVISION = 600;
static const uint16_t MAX_INTERVAL_UNITS = 3200;  // 4 s, the Bluetooth maximum
// The component sends no request of its own on a link younger than this: ESPHome's own
// post-connect update (rewritten below) lands in that time and the two must not stack up.
static const uint32_t MIN_LINK_AGE_MS = 1500;

// --- State shared with the wrapped Bluedroid calls ----------------------------
// The wrappers run on the Bluetooth host task, loop() on the main task.  Each
// value is a single word written by one side only.
static const uint16_t NO_HANDLE = 0xFFFF;
static const uint16_t LOGIN_UNITS = 24;          // 30 ms: interval used for every login window
// Scan window = interval of the first connect request (0.625 ms units): 100 ms.  The controller
// trims 2.5 ms off every window, so 100 ms listens 97.5 % of the time (30 ms: 91.7 %).
static const uint16_t INITIATE_SCAN_UNITS = 160;
static volatile bool g_enabled = false;
static volatile bool g_link_up = false;          // loop() has seen the lock's link in LST_CONNECTED
static volatile uint32_t g_link_up_ms = 0;       // millis() when it did
static volatile uint16_t g_lock_handle = NO_HANDLE;
static volatile uint32_t g_login_fast_ms = 15000;
static volatile bool g_peer_hold = false;
static volatile bool g_full_duty = true;
static volatile uint32_t g_peer_count = 0;       // updates for the lock's link that were not our own request
static volatile uint16_t g_peer_min = 0, g_peer_max = 0, g_peer_timeout = 0;      // what was asked
static volatile uint16_t g_peer_sent_min = 0, g_peer_sent_max = 0;                // what went to the controller
static volatile uint32_t g_create_count = 0;
// Connection generation.  The host task bumps g_conn_gen on every LE Create Connection for
// the lock; loop() records it when it marks a link up.  A mismatch means a new connect was
// started since then, so the old link's state is void even if loop() never saw the gap
// (Bluedroid reuses the same control block and the main loop can stall across it).
static volatile uint32_t g_conn_gen = 0;
static volatile uint32_t g_link_gen = 0;
static inline bool link_up_now() { return g_link_up && g_link_gen == g_conn_gen; }

// --- Connect timing (diagnostic) and radio-sharing experiments -----------------
// Written on the Bluetooth host task, read by loop().
static inline uint32_t now_ms() { return (uint32_t) (esp_timer_get_time() / 1000); }
static volatile uint32_t g_t_create_ms = 0;   // first LE Create Connection of this connect
static volatile uint32_t g_t_conn_ms = 0;     // last LE Connection Complete for the lock
static volatile uint16_t g_creates = 0;       // LE Create Connection commands in this connect
static volatile uint16_t g_conn_comps = 0;    // LE Connection Complete events in this connect
static volatile uint16_t g_conn_interval = 0;
static volatile uint16_t g_init_units = INITIATE_SCAN_UNITS;  // window = interval for the first request
// After LE Connection Complete Bluedroid (as central) reads the remote features and then the
// remote version, and only reports the link up after BOTH.  The version is only used for
// pairing, which the lock does not do; skipping that exchange for the lock saves two
// connection events before "Connection open".
static volatile bool g_skip_version = false;
static volatile uint32_t g_t_feat_ms = 0;     // remote features complete (the version request point), this connection
static volatile uint32_t g_ver_skipped = 0;
static volatile uint8_t g_ver_result = 0;     // this connection: 0 not reached, 1 skipped, 2 exchanged
// Set only while btm_ble_read_remote_features_complete() runs for a SUCCESSFUL feature read of a
// peer without Data Length Extension: the one situation in which the version request may be skipped.
static volatile bool g_feat_ok = false;
static volatile uint16_t g_feat_handle = NO_HANDLE;
// Connect boost.  The ESP32's one radio is time-shared with Wi-Fi: per beacon period (102.4 ms)
// Wi-Fi gets the first 60 % and Bluetooth scanning/connecting the last 40 % whenever the station
// is awake (esp_coex_preference_set() is an empty stub on this chip).  The lock advertises every
// ~0.8 s, so its advertisements drift through that window ~14 ms at a time: about 3 land in the
// Bluetooth slice, then 4-5 in a row land in the Wi-Fi slice and are never heard (measured
// 2026-11-15: bursts of 2-3 hits, 4-5 s gaps, while other receivers heard the lock throughout).
// While a connect to the lock is waiting for an advertisement:
//   1 = ask for the larger Bluetooth slice (BLE status MESH_CONFIG: 55 %),
//   2 = also put the Wi-Fi station in max modem sleep, so it skips 2 beacons in 3 and those
//       periods belong to Bluetooth entirely.
// Both are undone the moment the link is up, when the connect is abandoned, or after BOOST_MAX_MS.
static volatile uint8_t g_boost_mode = 2;
static volatile bool g_boost_req = false;     // host task: first connect request for the lock just went out
static const uint32_t BOOST_MAX_MS = 12000;   // covers the stack's own retries after an unanswered connect request

// Interval the component wants right now: 30 ms during the login window of every
// connection (and while no link is up yet, so the first update of a new link is
// already fast), the user's hold target afterwards.
static uint16_t effective_target() {
  uint16_t t = g_target;
  uint32_t login = g_login_fast_ms;
  if (t > LOGIN_UNITS && login != 0) {
    if (!link_up_now() || millis() - g_link_up_ms < login)
      return LOGIN_UNITS;
  }
  return t;
}

void LockFastLink::set_address(uint64_t address) {
  for (int i = 0; i < 6; i++)
    this->bda_[i] = (address >> ((5 - i) * 8)) & 0xFF;
  memcpy(g_lock_bda, this->bda_, 6);
  g_override = true;
}

void LockFastLink::set_connect_interval(uint16_t min_units, uint16_t max_units) {
  g_conn_min = min_units;
  g_conn_max = max_units < min_units ? min_units : max_units;
}

void LockFastLink::set_enabled(bool enabled) {
  this->enabled_ = enabled;
  g_enabled = enabled;
  this->last_request_ms_ = 0;
  ESP_LOGI(TAG, "%s", enabled ? "enabled" : "disabled (lock's own parameters win)");
}

void LockFastLink::set_target_interval(uint16_t units) {
  if (units < 6)
    units = 6;
  if (units > MAX_INTERVAL_UNITS)
    units = MAX_INTERVAL_UNITS;
  this->target_ = units;
  g_target = units;
  this->last_request_ms_ = 0;
  ESP_LOGI(TAG, "target interval %u x1.25 ms", (unsigned) units);
}

void LockFastLink::set_honour_ms(uint32_t ms) { this->honour_ms_ = ms; }

void LockFastLink::set_login_fast_ms(uint32_t ms) { g_login_fast_ms = ms; }

void LockFastLink::set_peer_hold(bool hold) {
  g_peer_hold = hold;
  ESP_LOGI(TAG, "lock's own interval request: %s", hold ? "answered OK, our interval kept" : "granted at its fast end, then reverted");
}

void LockFastLink::set_initiate_full_duty(bool on) { g_full_duty = on; }

void LockFastLink::set_skip_version(bool skip) {
  g_skip_version = skip;
  ESP_LOGI(TAG, "version exchange with the lock: %s", skip ? "skipped" : "run");
}

void LockFastLink::set_connect_boost(int mode) {
  g_boost_mode = mode < 0 ? 0 : (mode > 2 ? 2 : (uint8_t) mode);
  ESP_LOGI(TAG, "connect boost %u", (unsigned) g_boost_mode);
}

// Wi-Fi's share of each beacon period while the boost is on (percent, 5-60; stock 45).
void LockFastLink::set_boost_wifi_share(int pct) {
  if (pct < 5)
    pct = 5;
  if (pct > 60)
    pct = 60;
  coex_schm_ble_mesh_config_wifi_conn[2] = (uint8_t) pct;
  coex_schm_ble_mesh_config_wifi_conn[6] = (uint8_t) (100 - pct);
}

// Experiments only (not persistent): boost mode 0-2; Wi-Fi share percent while boosted; hold = 1
// keeps the larger Bluetooth share on all the time (to measure it while only scanning);
// init_units = scan window = interval of the first connect request in 0.625 ms units (0 = leave).
void LockFastLink::set_radio_test(int boost, int wifi_pct, int hold, int init_units) {
  esp_err_t e1 = esp_coex_status_bit_clear(ESP_COEX_ST_TYPE_BLE, ESP_COEX_BLE_ST_MESH_CONFIG);
  this->set_boost_wifi_share(wifi_pct);
  this->set_connect_boost(boost);
  this->exp_hold_ = hold == 1;
  this->exp_hold_ms_ = millis();
  esp_err_t e2 = ESP_OK;
  if (this->exp_hold_)
    e2 = esp_coex_status_bit_set(ESP_COEX_ST_TYPE_BLE, ESP_COEX_BLE_ST_MESH_CONFIG);
  if (init_units >= 4 && init_units <= 16384)
    g_init_units = (uint16_t) init_units;
  ESP_LOGI(TAG, "radio test: boost %u, Wi-Fi share %u %%, hold %d (%s/%s), connect window %u", (unsigned) g_boost_mode,
           (unsigned) coex_schm_ble_mesh_config_wifi_conn[2], (int) this->exp_hold_, esp_err_to_name(e1),
           esp_err_to_name(e2), (unsigned) g_init_units);
}

void LockFastLink::boost_start_() {
  this->boost_on_ = true;
  this->boost_on_ms_ = millis();
  this->boost_ps_changed_ = false;
  esp_coex_status_bit_set(ESP_COEX_ST_TYPE_BLE, ESP_COEX_BLE_ST_MESH_CONFIG);
  if (g_boost_mode >= 2) {
    wifi_ps_type_t cur;
    if (esp_wifi_get_ps(&cur) == ESP_OK && cur != WIFI_PS_MAX_MODEM) {
      this->boost_prev_ps_ = (int) cur;
      if (esp_wifi_set_ps(WIFI_PS_MAX_MODEM) == ESP_OK)
        this->boost_ps_changed_ = true;
    }
  }
}

void LockFastLink::boost_stop_() {
  if (!this->exp_hold_)
    esp_coex_status_bit_clear(ESP_COEX_ST_TYPE_BLE, ESP_COEX_BLE_ST_MESH_CONFIG);
  if (this->boost_ps_changed_) {
    esp_wifi_set_ps((wifi_ps_type_t) this->boost_prev_ps_);
    this->boost_ps_changed_ = false;
  }
  this->boost_on_ = false;
  ESP_LOGD(TAG, "connect boost off after %u ms", (unsigned) (millis() - this->boost_on_ms_));
}

// One set of connect timings per connection, handed out once (5 s after link-up, so nothing
// is sent during the login).
bool LockFastLink::take_phases(uint32_t &advert_wait_ms, uint32_t &bringup_ms, uint32_t &requests) {
  if (!this->ph_pending_ || millis() - this->ph_ready_ms_ < 5000)
    return false;
  this->ph_pending_ = false;
  advert_wait_ms = this->ph_wait_ms_;
  bringup_ms = this->ph_bringup_ms_;
  requests = this->ph_reqs_;
  return true;
}

void LockFastLink::setup() {}

void LockFastLink::apply_tx_power_() {
  // The lock hears the proxy too weakly to answer its connection request
  // (0x3E "Connection Failed to be Established") at the +3 dBm default.
  // Max out the ESP32's BLE TX power for advertising/scan/connect and every link.
  static const esp_ble_power_type_t types[] = {
      ESP_BLE_PWR_TYPE_DEFAULT, ESP_BLE_PWR_TYPE_ADV,      ESP_BLE_PWR_TYPE_SCAN,
      ESP_BLE_PWR_TYPE_CONN_HDL0, ESP_BLE_PWR_TYPE_CONN_HDL1, ESP_BLE_PWR_TYPE_CONN_HDL2,
      ESP_BLE_PWR_TYPE_CONN_HDL3, ESP_BLE_PWR_TYPE_CONN_HDL4, ESP_BLE_PWR_TYPE_CONN_HDL5,
      ESP_BLE_PWR_TYPE_CONN_HDL6, ESP_BLE_PWR_TYPE_CONN_HDL7, ESP_BLE_PWR_TYPE_CONN_HDL8};
  int ok = 0;
  for (auto t : types)
    if (esp_ble_tx_power_set(t, ESP_PWR_LVL_P9) == ESP_OK)
      ok++;
  ESP_LOGI(TAG, "BLE TX power set to +9 dBm (%d/%d)", ok, (int) (sizeof(types) / sizeof(types[0])));
  this->tx_applied_ms_ = millis();
}

void LockFastLink::dump_config() {
  ESP_LOGCONFIG(TAG, "Link setup for the lock: %u-%u x1.25 ms (overrides used: %u, updates held: %u)",
                (unsigned) g_conn_min, (unsigned) g_conn_max, (unsigned) g_override_count,
                (unsigned) g_update_override_count);
  ESP_LOGCONFIG(TAG, "Lock fast link for %02X:%02X:%02X:%02X:%02X:%02X: %s, target %u x1.25 ms", this->bda_[0],
                this->bda_[1], this->bda_[2], this->bda_[3], this->bda_[4], this->bda_[5],
                this->enabled_ ? "enabled" : "disabled", (unsigned) this->target_);
  ESP_LOGCONFIG(TAG, "Login window %u ms at %u x1.25 ms; lock's own request: %s, honour %u ms (seen %u)",
                (unsigned) g_login_fast_ms, (unsigned) LOGIN_UNITS, g_peer_hold ? "hold" : "follow",
                (unsigned) this->honour_ms_, (unsigned) g_peer_count);
  ESP_LOGCONFIG(TAG, "Connect listening for the lock: %s (used %u), connect boost %u (Wi-Fi share %u %% while connecting)",
                g_full_duty ? "continuous" : "stock 30 of 60 ms", (unsigned) g_create_count, (unsigned) g_boost_mode,
                (unsigned) coex_schm_ble_mesh_config_wifi_conn[2]);
  ESP_LOGCONFIG(TAG, "Version exchange with the lock: %s (skipped %u times)", g_skip_version ? "skipped" : "run",
                (unsigned) g_ver_skipped);
}

// Per-link state must never survive into the next connection.  Bluedroid reuses
// the same control block when a connect is requested while the old link is still
// disconnecting, so "pointer changed" is not enough: this also runs whenever the
// block is seen in any state other than LST_CONNECTED.
void LockFastLink::link_gone_() {
  g_link_up = false;
  g_lock_handle = NO_HANDLE;
  this->last_request_ms_ = 0;
  this->slow_since_ms_ = 0;
  this->last_want_ = 0;
}

void LockFastLink::loop() {
  // Bluedroid's control blocks only exist while the stack is enabled
  // (ESPHome brings it up after setup and may disable it).
  if (esp_bluedroid_get_status() != ESP_BLUEDROID_STATUS_ENABLED) {
    this->stack_up_ = false;
    this->link_ = nullptr;
    this->link_gone_();
    return;
  }
  if (!this->stack_up_) {
    this->stack_up_ = true;
    this->apply_tx_power_();
  }
  if (g_create_count != this->create_seen_) {
    this->create_seen_ = g_create_count;
    ESP_LOGD(TAG, "connect for the lock: listening continuously (%u x0.625 ms windows)", (unsigned) g_init_units);
  }
  if (this->exp_hold_ && millis() - this->exp_hold_ms_ > 120000) {
    // safety: an experiment must never leave Wi-Fi squeezed for good
    this->exp_hold_ = false;
    esp_coex_status_bit_clear(ESP_COEX_ST_TYPE_BLE, ESP_COEX_BLE_ST_MESH_CONFIG);
    ESP_LOGW(TAG, "radio test hold ended by its 120 s limit");
  }
  if (g_boost_req) {
    g_boost_req = false;
    if (g_boost_mode != 0 && !this->boost_on_)
      this->boost_start_();
  }
  if (this->boost_on_ && (link_up_now() || millis() - this->boost_on_ms_ > BOOST_MAX_MS))
    this->boost_stop_();
  tL2C_LCB *lcb = l2cu_find_lcb_by_bd_addr(this->bda_, BT_TRANSPORT_LE);
  if (lcb == nullptr) {
    if (this->link_ != nullptr || g_link_up)
      this->link_gone_();
    this->link_ = nullptr;
    if (this->boost_on_ && millis() - this->boost_on_ms_ > 300)
      this->boost_stop_();  // the connect was abandoned
    if (this->coex_bt_) {
      // Hand the radio back; ESPHome sets Bluetooth priority again for its own connects.
      esp_coex_preference_set(ESP_COEX_PREFER_BALANCE);
      this->coex_bt_ = false;
      ESP_LOGD(TAG, "lock link gone, coexistence back to balanced");
    }
    return;
  }
  // Kept for chips where it does something: on the classic ESP32 esp_coex_preference_set()
  // is an empty stub (measured 2026-11-15: no change in what the proxy hears).  The radio
  // share that matters here is the connect boost above.
  {
    uint32_t t = millis();
    if (!this->coex_bt_ || t - this->coex_ms_ >= COEX_REASSERT_MS) {
      esp_coex_preference_set(ESP_COEX_PREFER_BT);
      if (!this->coex_bt_)
        ESP_LOGD(TAG, "lock link: Bluetooth priority on the radio");
      this->coex_bt_ = true;
      this->coex_ms_ = t;
    }
  }
  if (lcb != this->link_) {
    this->apply_tx_power_();
    this->link_gone_();
    this->link_ = lcb;
  }
  if (g_link_up && g_link_gen != g_conn_gen)
    this->link_gone_();  // a new connect was issued since this link was marked up
  if (lcb->link_state != LST_CONNECTED) {
    // still being set up, or being torn down: never touch it, and forget the old link
    if (g_link_up)
      this->link_gone_();
    return;
  }
  uint16_t current = lcb->current_used_conn_interval;
  if (current == 0)
    return;  // link not up yet
  uint32_t now = millis();
  if (!g_link_up) {
    g_link_gen = g_conn_gen;
    g_link_up_ms = now;
    g_lock_handle = lcb->handle;
    g_link_up = true;
    ESP_LOGD(TAG, "lock link up at %u x1.25 ms", (unsigned) current);
    {
      uint32_t t = now_ms(), tc = g_t_create_ms, tk = g_t_conn_ms;
      this->ph_wait_ms_ = tk - tc;
      this->ph_bringup_ms_ = t - tk;
      this->ph_reqs_ = g_creates;
      this->ph_ready_ms_ = millis();
      this->ph_pending_ = true;
      if (this->boost_on_)
        this->boost_stop_();
      uint8_t vr = g_ver_result;
      uint32_t tf = g_t_feat_ms;
      ESP_LOGI(TAG, "connect phases: advert wait %u ms (%u connect requests, %u link starts), link bring-up %u ms "
                    "(features %d ms, version %s)",
               (unsigned) (tk - tc), (unsigned) g_creates, (unsigned) g_conn_comps, (unsigned) (t - tk),
               vr != 0 ? (int) (tf - tk) : -1, vr == 1 ? "skipped" : (vr == 2 ? "exchanged" : "not reached"));
    }
  }
  if (g_peer_count != this->peer_seen_) {
    this->peer_seen_ = g_peer_count;
    ESP_LOGI(TAG, "lock asked for %u-%u x1.25 ms t=%u -> controller told %u-%u", (unsigned) g_peer_min,
             (unsigned) g_peer_max, (unsigned) g_peer_timeout, (unsigned) g_peer_sent_min,
             (unsigned) g_peer_sent_max);
  }
  if (!this->enabled_)
    return;
  // Follow-then-revert: the lock's own parameter request is always answered OK
  // and applied (at the fast end of its range, see the wrapper below); once it
  // has stood for honour_ms the component asks for its own interval again.
  // Nearly all the time the link runs at the wanted interval.
  uint16_t want = effective_target();
  if (want != this->last_want_) {
    if (this->last_want_ != 0)
      ESP_LOGD(TAG, "login window over, wanted interval now %u x1.25 ms", (unsigned) want);
    this->last_want_ = want;
    this->last_request_ms_ = 0;
  }
  // Also act when only the supervision timeout is short (the lock asks for 6 s; granted at an
  // interval that happens to equal the wanted one it would otherwise stay in force).
  bool timeout_ok = lcb->current_used_conn_timeout >= FAST_SUPERVISION;
  if (current == want && timeout_ok) {
    this->slow_since_ms_ = 0;
    this->last_request_ms_ = 0;  // the retry gate is for a request that had no effect, not for the next change
    return;
  }
  if (lcb->conn_update_mask & L2C_BLE_UPDATE_PENDING)
    return;
  if (this->slow_since_ms_ == 0) {
    this->slow_since_ms_ = now;
    ESP_LOGD(TAG, "lock link at %u x1.25 ms t=%u, want %u t=%u", (unsigned) current,
             (unsigned) lcb->current_used_conn_timeout, (unsigned) want, (unsigned) FAST_SUPERVISION);
  }
  if (now - this->slow_since_ms_ < this->honour_ms_)
    return;
  if (now - g_link_up_ms < MIN_LINK_AGE_MS)
    return;
  if (this->last_request_ms_ != 0 && now - this->last_request_ms_ < REQUEST_RETRY_MS)
    return;
  esp_ble_conn_update_params_t params{};
  memcpy(params.bda, this->bda_, sizeof(params.bda));
  params.min_int = want;
  params.max_int = want;
  params.latency = 0;
  params.timeout = FAST_SUPERVISION;
  esp_err_t err = esp_ble_gap_update_conn_params(&params);
  this->last_request_ms_ = now;
  ESP_LOGI(TAG, "interval %u -> requesting %u x1.25 ms (%s)", (unsigned) current, (unsigned) want,
           err == ESP_OK ? "sent" : esp_err_to_name(err));
}

}  // namespace lock_fastlink
}  // namespace esphome

extern "C" {
esp_err_t __real_esp_ble_gap_set_prefer_conn_params(esp_bd_addr_t bd_addr, uint16_t min_conn_int,
                                                    uint16_t max_conn_int, uint16_t slave_latency,
                                                    uint16_t supervision_tout);
esp_err_t __wrap_esp_ble_gap_set_prefer_conn_params(esp_bd_addr_t bd_addr, uint16_t min_conn_int,
                                                    uint16_t max_conn_int, uint16_t slave_latency,
                                                    uint16_t supervision_tout) {
  using namespace esphome::lock_fastlink;
  if (g_override && memcmp(bd_addr, g_lock_bda, 6) == 0) {
    g_override_count = g_override_count + 1;
    // ESPHome calls this on the main loop right before esp_ble_gattc_open: put the larger
    // Bluetooth share in force before the controller starts listening; loop() does the rest.
    if (g_boost_mode != 0) {
      esp_coex_status_bit_set(ESP_COEX_ST_TYPE_BLE, ESP_COEX_BLE_ST_MESH_CONFIG);
      g_boost_req = true;
    }
    ESP_LOGI(TAG, "link setup for the lock: %u-%u x1.25 ms instead of %u-%u", (unsigned) g_conn_min,
             (unsigned) g_conn_max, (unsigned) min_conn_int, (unsigned) max_conn_int);
    return __real_esp_ble_gap_set_prefer_conn_params(bd_addr, g_conn_min, g_conn_max, 0, g_conn_timeout);
  }
  return __real_esp_ble_gap_set_prefer_conn_params(bd_addr, min_conn_int, max_conn_int, slave_latency,
                                                   supervision_tout);
}

// --- Post-connect update guard -----------------------------------------------
// After service discovery ESPHome pushes its "medium" parameters (8.75-11.25 ms,
// esp_ble_gap_update_conn_params).  The lock cannot keep up at that interval:
// the MTU exchange never gets answered and the link dies with a supervision
// timeout (0x08) ~15 s later.  For the lock's address, any update faster than
// the wanted interval is raised to it (30 ms in the login window).
esp_err_t __real_esp_ble_gap_update_conn_params(esp_ble_conn_update_params_t *params);
esp_err_t __wrap_esp_ble_gap_update_conn_params(esp_ble_conn_update_params_t *params) {
  using namespace esphome::lock_fastlink;
  if (params != nullptr && g_override && memcmp(params->bda, g_lock_bda, 6) == 0) {
    uint16_t want = effective_target();
    uint16_t floor = want < g_conn_min ? want : g_conn_min;
    if (floor < 6)
      floor = 6;
    if (params->min_int < floor || params->max_int < floor || params->latency != 0 ||
        params->timeout < MIN_SUPERVISION) {
      esp_ble_conn_update_params_t fixed = *params;
      if (fixed.min_int < floor || fixed.max_int < floor) {
        fixed.min_int = want;
        fixed.max_int = want;
        fixed.timeout = FAST_SUPERVISION;
      }
      fixed.latency = 0;
      if (fixed.timeout < MIN_SUPERVISION)
        fixed.timeout = MIN_SUPERVISION;
      ESP_LOGI(TAG, "update for the lock: %u-%u x1.25 ms t=%u -> %u-%u t=%u", (unsigned) params->min_int,
               (unsigned) params->max_int, (unsigned) params->timeout, (unsigned) fixed.min_int,
               (unsigned) fixed.max_int, (unsigned) fixed.timeout);
      g_update_override_count = g_update_override_count + 1;
      return __real_esp_ble_gap_update_conn_params(&fixed);
    }
  }
  return __real_esp_ble_gap_update_conn_params(params);
}

// --- The lock's own interval request ------------------------------------------
// Every connection-parameter change, ours or the lock's (its L2CAP request is
// answered OK by Bluedroid and then turned into this HCI command), reaches the
// controller through btsnd_hcic_ble_upd_ll_conn_params.  For the lock's link, a
// request that is not the component's own (min == max == wanted interval) is the
// lock's 200-400 ms request.  The controller would pick the slow end (400 ms)
// and the login then crawls until the revert lands ~4 s later.
//  follow (default): grant it at the fast end of the range the lock asked for
//                    (200 ms); the revert then takes 6 events of 200 ms, not 400.
//  hold:             apply the component's own interval instead; the link never
//                    slows down.  The lock still gets its OK answer.
// No logging here (Bluetooth host task): loop() reports what happened.
BOOLEAN __real_btsnd_hcic_ble_upd_ll_conn_params(UINT16 handle, UINT16 conn_int_min, UINT16 conn_int_max,
                                                 UINT16 conn_latency, UINT16 conn_timeout, UINT16 min_ce_len,
                                                 UINT16 max_ce_len);
BOOLEAN __wrap_btsnd_hcic_ble_upd_ll_conn_params(UINT16 handle, UINT16 conn_int_min, UINT16 conn_int_max,
                                                 UINT16 conn_latency, UINT16 conn_timeout, UINT16 min_ce_len,
                                                 UINT16 max_ce_len) {
  using namespace esphome::lock_fastlink;
  if (g_override && g_enabled && link_up_now() && handle == g_lock_handle) {
    uint16_t want = effective_target();
    if (!(conn_int_min == want && conn_int_max == want)) {
      g_peer_min = conn_int_min;
      g_peer_max = conn_int_max;
      g_peer_timeout = conn_timeout;
      if (g_peer_hold) {
        conn_int_min = want;
        conn_int_max = want;
        conn_latency = 0;
        conn_timeout = FAST_SUPERVISION;
      } else if (conn_int_min < conn_int_max) {
        conn_int_max = conn_int_min;
      }
      g_peer_sent_min = conn_int_min;
      g_peer_sent_max = conn_int_max;
      g_peer_count = g_peer_count + 1;
    }
  }
  return __real_btsnd_hcic_ble_upd_ll_conn_params(handle, conn_int_min, conn_int_max, conn_latency, conn_timeout,
                                                  min_ce_len, max_ce_len);
}

// --- Connect listening ----------------------------------------------------------
// Bluedroid starts a connect listening 30 ms out of every 60 ms, so half of the
// lock's advertisements (one every ~0.8 s) are missed and link setup takes a
// median 2 s.  For the lock's address the first request of each connect listens
// continuously; the request sent to the lock is unchanged, it just goes out on
// the first advertisement heard.
BOOLEAN __real_btsnd_hcic_ble_create_ll_conn(UINT16 scan_int, UINT16 scan_win, UINT8 init_filter_policy,
                                             UINT8 addr_type_peer, BD_ADDR bda_peer, UINT8 addr_type_own,
                                             UINT16 conn_int_min, UINT16 conn_int_max, UINT16 conn_latency,
                                             UINT16 conn_timeout, UINT16 min_ce_len, UINT16 max_ce_len);
BOOLEAN __wrap_btsnd_hcic_ble_create_ll_conn(UINT16 scan_int, UINT16 scan_win, UINT8 init_filter_policy,
                                             UINT8 addr_type_peer, BD_ADDR bda_peer, UINT8 addr_type_own,
                                             UINT16 conn_int_min, UINT16 conn_int_max, UINT16 conn_latency,
                                             UINT16 conn_timeout, UINT16 min_ce_len, UINT16 max_ce_len) {
  using namespace esphome::lock_fastlink;
  if (g_override && bda_peer != nullptr && memcmp(bda_peer, g_lock_bda, 6) == 0) {
    g_conn_gen = g_conn_gen + 1;  // a new connection to the lock is being set up
    // Only the first request of a connect listens continuously.  Bluedroid re-issues the
    // connect by itself (up to GATTC_CONNECT_RETRY_COUNT times, no pause) when the lock does
    // not answer; those retries keep the stock 30-of-60 ms so they are spaced as before.
    tL2C_LCB *p = l2cu_find_lcb_by_bd_addr(bda_peer, BT_TRANSPORT_LE);
    bool first = p != nullptr && p->retry_create_con == 0;
    if (first) {
      g_t_create_ms = now_ms();
      g_creates = 1;
      g_conn_comps = 0;
      g_boost_req = true;
    } else {
      g_creates = g_creates + 1;
    }
    if (g_full_duty && first) {
      scan_int = g_init_units;
      scan_win = g_init_units;
      g_create_count = g_create_count + 1;
    }
  }
  return __real_btsnd_hcic_ble_create_ll_conn(scan_int, scan_win, init_filter_policy, addr_type_peer, bda_peer,
                                              addr_type_own, conn_int_min, conn_int_max, conn_latency, conn_timeout,
                                              min_ce_len, max_ce_len);
}

// --- Connect timing ---------------------------------------------------------------
// LE Connection Complete for the lock: the controller caught an advertisement and sent
// its connect request.  What follows until the link is reported up is the stack's own
// bring-up.  Timestamp only; behaviour unchanged.
void __real_l2cble_conn_comp(UINT16 handle, UINT8 role, BD_ADDR bda, tBLE_ADDR_TYPE type, UINT16 conn_interval,
                             UINT16 conn_latency, UINT16 conn_timeout, UINT8 adv_handle, UINT16 sync_handle);
void __wrap_l2cble_conn_comp(UINT16 handle, UINT8 role, BD_ADDR bda, tBLE_ADDR_TYPE type, UINT16 conn_interval,
                             UINT16 conn_latency, UINT16 conn_timeout, UINT8 adv_handle, UINT16 sync_handle) {
  using namespace esphome::lock_fastlink;
  if (g_override && bda != nullptr && memcmp(bda, g_lock_bda, 6) == 0) {
    g_t_conn_ms = now_ms();
    g_conn_comps = g_conn_comps + 1;
    g_conn_interval = conn_interval;
    g_ver_result = 0;
  }
  __real_l2cble_conn_comp(handle, role, bda, type, conn_interval, conn_latency, conn_timeout, adv_handle, sync_handle);
}

// --- Version exchange ---------------------------------------------------------------
// As central, Bluedroid reads the remote features after LE Connection Complete and then, from
// btm_ble_read_remote_features_complete(), asks for the remote version; the link is reported up
// only from btm_read_remote_version_complete().  For the lock (when enabled) the link is reported
// up at the version request instead and nothing is sent.  The skip is taken only
//  - from that one call site (latch set around the real features-complete handler),
//  - after a feature read that SUCCEEDED (status 0), for a peer without Data Length Extension
//    (with it, the stock path would also negotiate the data length after the version),
//  - for a central LE link to the lock that is still waiting to be reported up, and
//  - if the report really brought the link up; otherwise the real request goes out.
// Event layout: status (1), handle (2, little endian), LE features (8).
void __real_btm_ble_read_remote_features_complete(UINT8 *p);
void __wrap_btm_ble_read_remote_features_complete(UINT8 *p) {
  using namespace esphome::lock_fastlink;
  bool ok = false;
  if (p != nullptr && p[0] == 0) {
    g_feat_handle = (uint16_t) (p[1] | (p[2] << 8));
    ok = (p[3] & 0x20) == 0;  // LE features bit 5: Data Packet Length Extension
  }
  g_feat_ok = ok;
  __real_btm_ble_read_remote_features_complete(p);
  g_feat_ok = false;
  g_feat_handle = NO_HANDLE;
}

BOOLEAN __real_btsnd_hcic_rmt_ver_req(UINT16 handle);
BOOLEAN __wrap_btsnd_hcic_rmt_ver_req(UINT16 handle) {
  using namespace esphome::lock_fastlink;
  if (g_override && g_feat_ok && handle == g_feat_handle) {
    tL2C_LCB *p = l2cu_find_lcb_by_handle(handle);
    if (p != nullptr && p->in_use && p->transport == BT_TRANSPORT_LE && p->link_role == HCI_ROLE_MASTER &&
        p->link_state == LST_CONNECTING && memcmp(p->remote_bd_addr, g_lock_bda, 6) == 0) {
      g_t_feat_ms = now_ms();
      if (g_skip_version) {
        l2cble_notify_le_connection(p->remote_bd_addr);
        if (p->link_state == LST_CONNECTED) {
          g_ver_skipped = g_ver_skipped + 1;
          g_ver_result = 1;
          return TRUE;
        }
      }
      g_ver_result = 2;
    }
  }
  return __real_btsnd_hcic_rmt_ver_req(handle);
}
}
#endif
