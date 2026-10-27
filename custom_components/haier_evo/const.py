from datetime import timedelta

DOMAIN = "haier_evo"
CONF_REGION = "region"
REGIONS = ["ru", "kz", "by"]

# Login throttling: at most one login per LOGIN_LIMIT_PERIOD seconds,
# after HTTP 429 wait LOGIN_LIMIT_429 seconds, doubling up to LOGIN_LIMIT_MAX.
LOGIN_LIMIT_PERIOD = 15
LOGIN_LIMIT_429 = 60
LOGIN_LIMIT_MAX = 900

API_TIMEOUT = 15
API_PATH = "https://evo.haieronline.ru"
API_LOGIN = "v2/{region}/users/auth/sign-in"
API_TOKEN_REFRESH = "v2/{region}/users/auth/refresh"
API_DEVICES = "v2/{region}/pages/sduiRawPaginated/smartHome?part=1&partitionWeight=6"
API_STATUS = "https://iot-platform.evo.haieronline.ru/mobile-backend-service/api/v1/config/{mac}?type=DETAILED"
API_WS_PATH = "wss://iot-platform.evo.haieronline.ru/gateway-ws-service/ws/"

# WebSocket: ping every WS_HEARTBEAT s, a missing pong closes the socket.
WS_HEARTBEAT = 30
# How long a command waits for the socket to come back.
WS_SEND_TIMEOUT = 15
# Reconnect backoff, reset after a connection that lived WS_STABLE_AFTER s.
WS_BACKOFF_MIN = 2
WS_BACKOFF_MAX = 60
WS_STABLE_AFTER = 60
# Entities go unavailable after this long without the cloud.
WS_UNAVAILABLE_AFTER = 180
# After an outage longer than this, re-read device statuses over REST.
WS_RESYNC_AFTER = 30
# Wait this long for command_response before re-sending a command once.
WS_ACK_TIMEOUT = 10
# Safety net against a session that stays open but goes silent.
REST_REFRESH_INTERVAL = timedelta(minutes=15)
