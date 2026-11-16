#pragma once
#include "esp_bt.h"
#include "esp_gap_ble_api.h"

// Max out the ESP32 BLE TX power (+9 dBm = ESP_PWR_LVL_P9) for advertising,
// scanning, the default, and every connection handle.
inline void set_max_tx_power() {
  esp_ble_tx_power_set(ESP_BLE_PWR_TYPE_DEFAULT, ESP_PWR_LVL_P9);
  esp_ble_tx_power_set(ESP_BLE_PWR_TYPE_ADV, ESP_PWR_LVL_P9);
  esp_ble_tx_power_set(ESP_BLE_PWR_TYPE_SCAN, ESP_PWR_LVL_P9);
  for (int h = ESP_BLE_PWR_TYPE_CONN_HDL0; h <= ESP_BLE_PWR_TYPE_CONN_HDL8; h++)
    esp_ble_tx_power_set((esp_ble_power_type_t) h, ESP_PWR_LVL_P9);
}
