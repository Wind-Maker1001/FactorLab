"""Optional trade_updates session; REST reconciles both connection boundaries."""
import json

STREAM_URL = "wss://paper-api.alpaca.markets/stream"


def trade_updates(adapter, *, connect=None, on_update=None, idle_timeout=60):
    """Run one session; caller may reconnect after this raises.

    Requires the optional websockets package. A new session always repairs gaps
    with REST, rather than treating a subscription acknowledgement as recovery.
    """
    if connect is None:
        from websockets.sync.client import connect
    adapter.recover()
    config = adapter.http.config
    try:
        with connect(STREAM_URL, open_timeout=config.timeout, close_timeout=5,
                     max_size=1024 * 1024) as socket:
            socket.send(json.dumps({"action": "auth", "key": config.key_id, "secret": config.secret_key}))
            response = json.loads(socket.recv(timeout=config.timeout))
            if response.get("stream") != "authorization" or response["data"]["status"] != "authorized":
                raise PermissionError("Alpaca Paper stream authorization failed")
            socket.send(json.dumps({"action": "listen", "data": {"streams": ["trade_updates"]}}))
            response = json.loads(socket.recv(timeout=config.timeout))
            if response.get("stream") != "listening" or "trade_updates" not in response["data"]["streams"]:
                raise ConnectionError("Alpaca Paper trade_updates subscription failed")
            adapter.recover()
            if on_update is not None and on_update():
                return
            while True:
                message = json.loads(socket.recv(timeout=idle_timeout))
                if message.get("stream") == "trade_updates":
                    adapter.on_trade_update(message)
                    if on_update is not None and on_update():
                        return
    finally:
        adapter.reconciled = False
        adapter.recover()
