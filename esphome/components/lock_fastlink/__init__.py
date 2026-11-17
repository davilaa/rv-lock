"""Keep one BLE link (the RV lock's) at an interval chosen by the proxy.

The Kohree/Tuya RV lock asks for a 200-400 ms connection interval right after
its notifications are enabled, which makes every request/response take ~0.8 s.
Bluedroid always accepts.  This component logs in at 30 ms on every connection,
grants the lock's request at its fast end and reverts it (or keeps its own
interval, `peer_request: hold`), then holds the link at `interval`.
"""
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.const import CONF_ID, CONF_MAC_ADDRESS

DEPENDENCIES = ["esp32_ble_tracker"]
CODEOWNERS = []

IDF_ROOT = "/root/.cache/esphome/idf/frameworks"
_INCLUDES = [
    "host/bluedroid/common/include",
    "host/bluedroid/stack/include",
    "host/bluedroid/stack/l2cap/include",
    "host/bluedroid/stack/btm/include",
    "host/bluedroid/config/include",
    "host/bluedroid/device/include",
    "host/bluedroid/hci/include",
    "host/bluedroid/bta/include",
    "host/bluedroid/api/include/api",
    "common/include",
    "common/osi/include",
    "common/btc/include",
    "common/api/include/api",
]

lock_fastlink_ns = cg.esphome_ns.namespace("lock_fastlink")
LockFastLink = lock_fastlink_ns.class_("LockFastLink", cg.Component)

CONF_ENABLED = "enabled"
CONF_INTERVAL_MS = "interval"
CONF_CONNECT_MIN = "connect_interval_min"
CONF_CONNECT_MAX = "connect_interval_max"
CONF_HONOUR = "honour"
CONF_LOGIN_FAST = "login_fast"
CONF_PEER_REQUEST = "peer_request"
CONF_INITIATE_FULL_DUTY = "initiate_full_duty"
CONF_CONNECT_BOOST = "connect_boost"
CONF_BOOST_WIFI_SHARE = "boost_wifi_share"
CONF_SKIP_VERSION = "skip_version_exchange"
CONF_RETRY_FULL_DUTY = "retry_full_duty"

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(LockFastLink),
        cv.Required(CONF_MAC_ADDRESS): cv.mac_address,
        cv.Optional(CONF_ENABLED, default=True): cv.boolean,
        cv.Optional(CONF_INTERVAL_MS, default="30ms"): cv.positive_time_period_microseconds,
        cv.Optional(CONF_CONNECT_MIN, default="30ms"): cv.positive_time_period_microseconds,
        cv.Optional(CONF_CONNECT_MAX, default="50ms"): cv.positive_time_period_microseconds,
        # how long an interval set by the lock stands before the proxy asks for its own again
        cv.Optional(CONF_HONOUR, default="200ms"): cv.positive_time_period_milliseconds,
        # every connection runs at 30 ms for this long (handshake + first command), then moves to `interval`
        cv.Optional(CONF_LOGIN_FAST, default="15s"): cv.positive_time_period_milliseconds,
        # the lock's own 200-400 ms request: follow = grant at 200 ms then revert; hold = answer OK, keep our interval
        cv.Optional(CONF_PEER_REQUEST, default="follow"): cv.one_of("follow", "hold", lower=True),
        # listen continuously while connecting to the lock (stock Bluedroid listens 30 of every 60 ms)
        cv.Optional(CONF_INITIATE_FULL_DUTY, default=True): cv.boolean,
        # while a connect waits for the lock's advertisement: 0 = nothing, 1 = larger Bluetooth share of the
        # radio, 2 = also Wi-Fi max modem sleep (the station skips 2 beacons in 3 for that second or two)
        cv.Optional(CONF_CONNECT_BOOST, default=2): cv.int_range(min=0, max=2),
        # Wi-Fi's share of each beacon period while the boost is on (stock table: 45)
        cv.Optional(CONF_BOOST_WIFI_SHARE, default=45): cv.int_range(min=5, max=60),
        # report the lock's link up after the feature exchange, without the version exchange
        cv.Optional(CONF_SKIP_VERSION, default=False): cv.boolean,
        # the first retry after an unanswered connect request also listens continuously (later retries stay stock)
        cv.Optional(CONF_RETRY_FULL_DUTY, default=False): cv.boolean,
    }
).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    from esphome.components.esp32 import idf_version

    version = idf_version()
    idf = f"{IDF_ROOT}/{version.major}.{version.minor}.{version.patch}/components/bt"
    for inc in _INCLUDES:
        cg.add_cxx_build_flag(f"-I{idf}/{inc}")
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    cg.add(var.set_address(config[CONF_MAC_ADDRESS].as_hex))
    cg.add(var.set_enabled(config[CONF_ENABLED]))
    units = max(6, round(config[CONF_INTERVAL_MS].total_microseconds / 1250))
    cg.add(var.set_target_interval(units))
    cmin = max(6, round(config[CONF_CONNECT_MIN].total_microseconds / 1250))
    cmax = max(cmin, round(config[CONF_CONNECT_MAX].total_microseconds / 1250))
    cg.add(var.set_connect_interval(cmin, cmax))
    cg.add_build_flag("-Wl,--wrap=esp_ble_gap_set_prefer_conn_params")
    cg.add(var.set_honour_ms(config[CONF_HONOUR].total_milliseconds))
    cg.add(var.set_login_fast_ms(config[CONF_LOGIN_FAST].total_milliseconds))
    cg.add(var.set_peer_hold(config[CONF_PEER_REQUEST] == "hold"))
    cg.add(var.set_initiate_full_duty(config[CONF_INITIATE_FULL_DUTY]))
    cg.add(var.set_connect_boost(config[CONF_CONNECT_BOOST]))
    cg.add(var.set_boost_wifi_share(config[CONF_BOOST_WIFI_SHARE]))
    cg.add(var.set_skip_version(config[CONF_SKIP_VERSION]))
    cg.add(var.set_retry_full_duty(config[CONF_RETRY_FULL_DUTY]))
    cg.add_build_flag("-Wl,--wrap=esp_ble_gap_update_conn_params")
    cg.add_build_flag("-Wl,--wrap=btsnd_hcic_ble_upd_ll_conn_params")
    cg.add_build_flag("-Wl,--wrap=btsnd_hcic_ble_create_ll_conn")
    cg.add_build_flag("-Wl,--wrap=l2cble_conn_comp")
    cg.add_build_flag("-Wl,--wrap=btsnd_hcic_rmt_ver_req")
    cg.add_build_flag("-Wl,--wrap=btm_ble_read_remote_features_complete")
