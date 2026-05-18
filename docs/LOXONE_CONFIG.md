# Loxone Config — Marstek Cloud Plugin

This plugin polls the Marstek Cloud (`eu.hamedata.com`) and publishes one MQTT
topic per datapoint per device. Use the LoxBerry **MQTT Gateway** plugin to
subscribe and route the values into Loxone Config virtual inputs.

## MQTT topic shape

```
<topic_prefix>/<device_sn>/<datapoint>
```

Default `topic_prefix` is `marstek`. `<device_sn>` is the device serial number
reported by the cloud (falls back to `devid` / `mac` / `unknown`).

## Per-device datapoints

| Topic | Description | Unit | Suggested Loxone object |
|---|---|---|---|
| `marstek/<sn>/soc` | State of charge | % | Virtual Input, EU `%` |
| `marstek/<sn>/charge` | Charge power | W | Virtual Input, EU `W` |
| `marstek/<sn>/discharge` | Discharge power | W | Virtual Input, EU `W` |
| `marstek/<sn>/load` | Load power | W | Virtual Input, EU `W` |
| `marstek/<sn>/profit` | Lifetime profit | € | Virtual Input, EU `€` |
| `marstek/<sn>/version` | Firmware version | — | Virtual Text Input |
| `marstek/<sn>/sn` | Serial number | — | Virtual Text Input |
| `marstek/<sn>/report_time` | Last device report (epoch seconds) | s | Virtual Input |
| `marstek/<sn>/connection_status` | `online` / `offline` | — | Virtual Text Input |
| `marstek/<sn>/last_update_epoch` | Last successful poll (epoch seconds) | s | Virtual Input |
| `marstek/<sn>/api_latency_ms` | Last API call latency | ms | Virtual Input |
| `marstek/<sn>/raw_json` | Raw JSON (only if `publish_raw_json: true`) | — | — |

## Plugin-wide status topics

| Topic | Description |
|---|---|
| `marstek/_status` | `online` / `error` / `offline` |
| `marstek/_device_count` | Number of devices returned this poll |
| `marstek/_last_poll_epoch` | Epoch seconds of last successful poll |

## MQTT Gateway subscription

In the **MQTT Gateway** plugin → *Incoming overview* → *Subscriptions*, add:

```
marstek/#
```

The Gateway will auto-create a Virtual Input in Loxone Config for each topic on
first receive. Rename and assign units there as needed.

## Loxone Config tips

- Use a **Status block** to derive on/off from `charge` and `discharge`.
- Use a **Memory flag** initialised from `soc` to drive battery-aware automation
  (e.g., enable car charging only above 60%).
- Display `connection_status` as a text input next to the SOC value to surface
  cloud reachability.
