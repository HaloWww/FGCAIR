DOMAIN = "fgcair"
SIGNAL_STATE_UPDATED = f"{DOMAIN}_state_updated"

APP_ID = "56f717d9c96145a3a517d96c0e35853e"
API_BASE = "http://115.190.119.84"
API_HOST = "api.fgcawx.com"
SITE_HOST = "site.fgcawx.com"

CONF_SELECTED_DIDS = "selected_dids"
CONF_DEVICES = "devices"
CONF_TEMP_SOURCE_ENTITY_ID = "temp_source_entity_id"
CONF_UPDATE_INTERVAL = "update_interval"
DEFAULT_UPDATE_INTERVAL = 60

PLATFORMS = ["climate", "select"]

MODE_TO_HVAC = {
    0: "heat_cool",
    1: "cool",
    2: "dry",
    3: "fan_only",
    4: "heat",
}

SPEED_TO_FAN = {
    0: "auto",
    1: "1档",
    2: "2档",
    3: "3档",
    4: "4档",
    5: "5档",
    6: "6档",
}
FAN_TO_SPEED = {value: key for key, value in SPEED_TO_FAN.items()}

INDOOR_MESH_PREFIX = "0400"
