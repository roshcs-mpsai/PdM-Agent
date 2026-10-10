"""MQTT plumbing shared by the services (R0).

A service is a set of pure handlers -- payload in, messages out -- and
``run_service`` wires them to the broker: subscribe on connect (so a
reconnect resubscribes), call the handler for each message, publish what it
returns at QoS 1, and on SIGINT/SIGTERM publish whatever ``on_stop`` returns
before disconnecting. Keeping the handlers free of MQTT is what lets the
tests drive every service without a broker.

Every service also gets the standard metrics (pdm_common.metrics: messages in
and out per topic, handler time and errors, ``pdm_up``) on its port in
``metrics.PORTS`` (``PDM_METRICS_PORT`` overrides, ``off`` disables), with
``/health`` answering 503 while the broker connection is down.

Broker authentication, ACLs and persistent sessions arrive in R3 hardening.
"""
from __future__ import annotations

import logging
import signal
import threading
from dataclasses import dataclass
from typing import Callable, Iterable, Optional


@dataclass(frozen=True)
class Out:
    """One message to publish."""
    topic: str
    payload: str
    retain: bool = False


Handler = Callable[[bytes], Iterable[Out]]


def _client(client_id: str):
    import paho.mqtt.client as mqtt

    return mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)


def run_service(
    name: str,
    handlers: dict[str, Handler],
    *,
    host: str = "localhost",
    port: int = 1883,
    on_stop: Optional[Callable[[], Iterable[Out]]] = None,
    qos: int = 1,
    tick: Optional[tuple[float, Callable[[], Iterable[Out]]]] = None,
) -> None:
    """Run until SIGINT or SIGTERM. ``handlers`` maps exact topics to handlers.

    ``tick``: (seconds, function) -- called every so many wall seconds, never at
    the same time as a handler, and what it returns is published (the gate's
    no-data check).

    Shutdown runs in a fixed order: stop handling (anything still arriving is
    left unprocessed and counted in the log), publish what ``on_stop``
    returns, then disconnect. No handler runs after ``on_stop``, so the
    counts it reports are final. If the broker has gone, the final messages
    are logged as lost and the service still exits cleanly.
    """
    from pdm_common import metrics

    log = logging.getLogger(name)
    client = _client(f"pdm-{name}")
    M = metrics.service_metrics(name)
    M.set_up(False)
    server = metrics.serve(metrics.REGISTRY, metrics.port_for(name), service=name, health=client.is_connected)
    lock = threading.Lock()          # handlers and on_stop never run concurrently
    stop = threading.Event()
    closing = threading.Event()
    unprocessed = [0]

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("broker refused the connection: %s", reason_code)
            return
        for topic in handlers:
            client.subscribe(topic, qos=qos)
        M.set_up(True)
        log.info("connected to %s:%s; subscribed to %s", host, port, ", ".join(handlers))

    def on_disconnect(client, userdata, flags, reason_code, properties):
        M.set_up(False)
        log.warning("disconnected from the broker: %s", reason_code)

    def on_message(client, userdata, msg):
        handler = handlers.get(msg.topic)
        if handler is None:
            return
        try:
            with lock:
                if closing.is_set():
                    unprocessed[0] += 1
                    return
                with M.handling(msg.topic):
                    outs = list(handler(msg.payload))
        except Exception:                     # a bad message must not stop the service
            log.exception("handler failed on a message from %s", msg.topic)
            return
        for out in outs:
            client.publish(out.topic, out.payload, qos=qos, retain=out.retain)
            M.messages_out_total.labels(topic=out.topic).inc()

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        client.connect(host, port)
    except OSError as exc:
        raise SystemExit(f"{name}: cannot reach the broker at {host}:{port} ({exc}); "
                         "is it running? docker compose up -d")
    client.loop_start()
    try:
        if tick is None:
            stop.wait()
        else:
            every, fn = tick
            while not stop.wait(every):
                with lock:
                    if closing.is_set():
                        break
                    try:
                        outs = list(fn())
                    except Exception:
                        log.exception("tick failed")
                        M.handler_errors_total.labels(topic="tick").inc()
                        outs = []
                for out in outs:
                    client.publish(out.topic, out.payload, qos=qos, retain=out.retain)
                    M.messages_out_total.labels(topic=out.topic).inc()
    finally:
        with lock:
            closing.set()                     # from here on, messages are not handled
            outs = list(on_stop()) if on_stop is not None else []
        for out in outs:
            try:
                info = client.publish(out.topic, out.payload, qos=qos, retain=out.retain)
                info.wait_for_publish(timeout=5)
                if not info.is_published():
                    log.error("final message on %s was not confirmed by the broker", out.topic)
            except Exception as exc:          # broker gone: report it, still shut down
                log.error("final message on %s was not published: %s", out.topic, exc)
        client.loop_stop()
        client.disconnect()
        M.set_up(False)
        if server is not None:
            server.close()
        if unprocessed[0]:
            log.info("%d message(s) arrived during shutdown and were not handled", unprocessed[0])


def read_retained(topic: str, host: str = "localhost", port: int = 1883,
                  timeout: float = 2.0) -> Optional[bytes]:
    """The retained message on ``topic``, or None if none arrives in time."""
    client = _client("")
    got: dict[str, bytes] = {}
    done = threading.Event()

    def on_connect(client, userdata, flags, reason_code, properties):
        client.subscribe(topic, qos=1)

    def on_message(client, userdata, msg):
        got["payload"] = msg.payload
        done.set()

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(host, port)
    client.loop_start()
    try:
        done.wait(timeout)
    finally:
        client.loop_stop()
        client.disconnect()
    return got.get("payload")
