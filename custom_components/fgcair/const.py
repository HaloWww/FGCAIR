DOMAIN = "fgcair"
SIGNAL_STATE_UPDATED = f"{DOMAIN}_state_updated"

APP_ID = "56f717d9c96145a3a517d96c0e35853e"
API_BASE = "http://115.190.119.84"
API_HOST = "api.fgcawx.com"
SITE_HOST = "site.fgcawx.com"

CONF_SELECTED_DIDS = "selected_dids"
CONF_AUTO_BIND_CAPTURED = "auto_bind_captured"
CONF_TEMP_SOURCE_DID = "temp_source_did"

PLATFORMS = ["climate", "fan", "select", "switch"]

MODE_TO_LABEL = {
    0: "自动",
    1: "制冷",
    2: "除湿",
    3: "通风",
    4: "制热",
}
LABEL_TO_MODE = {value: key for key, value in MODE_TO_LABEL.items()}

MODE_TO_HVAC = {
    0: "heat_cool",
    1: "cool",
    2: "dry",
    3: "fan_only",
    4: "heat",
}

SPEED_TO_FAN = {
    0: "自动",
    1: "1档",
    2: "2档",
    3: "3档",
    4: "4档",
    5: "5档",
    6: "6档",
}
FAN_TO_SPEED = {value: key for key, value in SPEED_TO_FAN.items()}

KNOWN_INDOOR_DIDS = {
    1: "YSNtVwL8Rs4UmGk7cXXAoC",
    2: "YkkWf6qcA8V5wEm2U9hVsr",
    3: "pW2wbCa55vsjLY5DdKrbYt",
    4: "tV4LPSPTMd22afK8PBTvrK",
}
