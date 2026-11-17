#pragma once
#include "esphome/core/component.h"

namespace esphome {
namespace lock_fastlink {

// Keeps one peripheral's BLE link at an interval chosen by the central.
//
// The RV lock asks for a 200-400 ms connection interval right after its
// notifications are enabled and Bluedroid always grants it, after which every
// exchange costs ~0.8 s.  When enabled, this component holds the link at
// `target` (1.25 ms units):
//  - every connection starts with a login window (`login_fast`) at 30 ms, whatever
//    the hold target is, so the handshake never runs on a slow link;
//  - the lock's own request is granted at the fast end of the range it asked for
//    and reverted after `honour` (peer_hold = false), or answered OK and applied
//    as the component's own interval (peer_hold = true);
//  - outside the login window the link is moved to `target`.
// Disabled = stock behaviour (the lock's own parameters win).
class LockFastLink : public Component {
 public:
  void set_address(uint64_t address);
  void set_enabled(bool enabled);
  void set_target_interval(uint16_t units);
  void set_connect_interval(uint16_t min_units, uint16_t max_units);
  void set_honour_ms(uint32_t ms);
  void set_login_fast_ms(uint32_t ms);
  void set_peer_hold(bool hold);
  void set_initiate_full_duty(bool on);
  void set_retry_full_duty(bool on);
  void set_radio_test(int boost, int wifi_pct, int hold, int init_units);
  void set_skip_version(bool skip);
  void set_boost_wifi_share(int pct);
  void set_connect_boost(int mode);
  bool take_phases(uint32_t &advert_wait_ms, uint32_t &bringup_ms, uint32_t &requests);
  bool enabled() const { return this->enabled_; }
  uint16_t target_interval() const { return this->target_; }
  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_BLUETOOTH; }

 protected:
  uint8_t bda_[6]{};
  bool enabled_{false};
  uint16_t target_{24};       // 30 ms
  uint32_t honour_ms_{200};   // how long an interval set by the lock stands before the revert is requested
  uint32_t last_request_ms_{0};
  void *link_{nullptr};
  uint32_t tx_applied_ms_{0};
  uint32_t slow_since_ms_{0};
  uint32_t coex_ms_{0};       // last time Bluetooth priority was asserted for the lock's link
  bool coex_bt_{false};       // we hold Bluetooth priority
  bool stack_up_{false};
  uint16_t last_want_{0};         // last effective target logged
  uint32_t peer_seen_{0};         // wrapper counters already reported in the log
  uint32_t create_seen_{0};
  bool boost_on_{false};          // connect boost is applied right now
  bool exp_hold_{false};          // experiment: larger Bluetooth share held on continuously
  uint32_t exp_hold_ms_{0};
  uint32_t boost_on_ms_{0};
  bool boost_ps_changed_{false};
  int boost_prev_ps_{1};
  bool ph_pending_{false};        // connect timings not handed out yet
  uint32_t ph_ready_ms_{0}, ph_wait_ms_{0}, ph_bringup_ms_{0}, ph_reqs_{0};
  void boost_start_();
  void boost_stop_();
  void apply_tx_power_();
  void link_gone_();
};

}  // namespace lock_fastlink
}  // namespace esphome
