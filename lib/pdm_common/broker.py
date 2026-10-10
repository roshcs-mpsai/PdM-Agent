"""MQTT broker connection settings from the environment (R3 hardening, NFR-06).

Every program that talks to the broker reads the same variables:

    PDM_MQTT_HOST                broker host; replaces the program's --broker when set
    PDM_MQTT_PORT                broker port; replaces --port when set
    PDM_MQTT_USER_<SERVICE>      the service's broker account, else PDM_MQTT_USER
    PDM_MQTT_PASSWORD_<SERVICE>  its password, else PDM_MQTT_PASSWORD
    PDM_MQTT_TLS_CA              CA certificate (PEM) of the broker; setting it turns TLS on,
                                 and the port becomes 8883 unless PDM_MQTT_PORT says otherwise

``<SERVICE>`` is the service name in upper case with every character other than a letter or
a digit replaced by ``_``: ingest -> INGEST, opcua_north -> OPCUA_NORTH, south-cip ->
SOUTH_CIP. The broker accounts carry the same names (mosquitto/config/acl), and
tools/broker_users.py writes both variables of every account to .env. The per-service
variables let one shell (or one .env) run any service; inside a container, set the generic
PDM_MQTT_USER / PDM_MQTT_PASSWORD to that container's account.

Why the environment wins over --broker and --port: the services default those flags to
localhost and 1883, and a default cannot be told apart from a value given on purpose. With
none of the variables set, nothing changes: no credentials, no TLS, and the host and port
the caller passed (the R0 development broker).

``run_service`` and ``read_retained`` (pdm_common.mqtt) apply all of this. A program with
its own paho loop calls ``connect(client, "<service>", host, port)`` in place of
``client.connect(host, port)``.

Nothing here imports paho; ``apply`` works on any object with paho's ``username_pw_set``
and ``tls_set``.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

PLAIN_PORT = 1883
TLS_PORT = 8883


class BrokerConfigError(ValueError):
    """The broker variables are inconsistent (a user without a password, a missing CA file...)."""


@dataclass(frozen=True)
class BrokerOptions:
    """Connection settings for one service. ``None`` means "not set in the environment".
    The password is left out of ``repr``, so logging the options never shows it."""
    host: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = field(default=None, repr=False)
    tls_ca: Optional[str] = None

    @property
    def tls(self) -> bool:
        return self.tls_ca is not None


def env_key(service: str) -> str:
    """The suffix of the per-service variables: 'south-cip' -> 'SOUTH_CIP'."""
    return re.sub(r"[^A-Za-z0-9]", "_", service).upper()


def _get(env: Mapping[str, str], name: str) -> Optional[str]:
    value = env.get(name, "").strip()
    return value or None


def _none_of(names: list[str]) -> str:
    return f"{names[0]} is not" if len(names) == 1 else f"neither {' nor '.join(names)} is"


def _connection(env: Mapping[str, str]) -> tuple[Optional[str], Optional[int], Optional[str]]:
    """(host, port, tls_ca) from the environment; the parts that do not depend on the service."""
    host = _get(env, "PDM_MQTT_HOST")
    port = None
    text = _get(env, "PDM_MQTT_PORT")
    if text is not None:
        if not text.isdigit() or not 0 < int(text) < 65536:
            raise BrokerConfigError(f"PDM_MQTT_PORT must be a port number, not {text!r}")
        port = int(text)
    ca = _get(env, "PDM_MQTT_TLS_CA")
    if ca is not None and not Path(ca).is_file():
        raise BrokerConfigError(f"PDM_MQTT_TLS_CA={ca}: no such file "
                                "(tools/make_certs.sh writes mosquitto/certs/ca.crt)")
    return host, port, ca


def client_options(service: str = "", env: Optional[Mapping[str, str]] = None) -> BrokerOptions:
    """The broker settings for ``service`` from ``env`` (default: os.environ).

    The account comes from PDM_MQTT_USER_<SERVICE> / PDM_MQTT_PASSWORD_<SERVICE>, each
    falling back to PDM_MQTT_USER / PDM_MQTT_PASSWORD. With ``service`` empty only the
    generic pair is read. A user without a password, or a password without a user, is an
    error rather than an anonymous connection the broker would refuse later.
    """
    env = os.environ if env is None else env
    host, port, ca = _connection(env)
    names = ([f"PDM_MQTT_USER_{env_key(service)}"] if service else []) + ["PDM_MQTT_USER"]
    secrets = ([f"PDM_MQTT_PASSWORD_{env_key(service)}"] if service else []) + ["PDM_MQTT_PASSWORD"]
    user_var = next((n for n in names if _get(env, n) is not None), None)
    password_var = next((n for n in secrets if _get(env, n) is not None), None)
    if user_var and not password_var:
        raise BrokerConfigError(f"{user_var} is set but {_none_of(secrets)}")
    if password_var and not user_var:
        raise BrokerConfigError(f"{password_var} is set but {_none_of(names)}")
    return BrokerOptions(host=host, port=port,
                         username=_get(env, user_var) if user_var else None,
                         password=_get(env, password_var) if password_var else None,
                         tls_ca=ca)


def address(host: str, port: int, options: Optional[BrokerOptions] = None,
            env: Optional[Mapping[str, str]] = None) -> tuple[str, int]:
    """Where to connect: PDM_MQTT_HOST / PDM_MQTT_PORT when set, else ``host`` and ``port``
    (the caller's --broker and --port), except that TLS without PDM_MQTT_PORT means 8883."""
    if options is None:
        env_host, env_port, ca = _connection(os.environ if env is None else env)
    else:
        env_host, env_port, ca = options.host, options.port, options.tls_ca
    if env_port is None and ca is not None:
        env_port = TLS_PORT
    return env_host or host, env_port or port


def apply(client, options: BrokerOptions) -> None:
    """Credentials and TLS on a paho client, before it connects. TLS verifies the broker's
    certificate against the CA and the host name against the certificate (paho's defaults:
    CERT_REQUIRED, hostname checking on)."""
    if options.username is not None:
        client.username_pw_set(options.username, options.password)
    if options.tls_ca is not None:
        client.tls_set(ca_certs=options.tls_ca)


def connect(client, service: str, host: str = "localhost", port: int = PLAIN_PORT, **kwargs) -> tuple[str, int]:
    """``apply`` the service's options, then ``client.connect`` to ``address(...)``; returns the
    (host, port) used. For the programs with their own paho loop."""
    options = client_options(service)
    apply(client, options)
    host, port = address(host, port, options)
    client.connect(host, port, **kwargs)
    return host, port


__all__ = ["BrokerConfigError", "BrokerOptions", "PLAIN_PORT", "TLS_PORT", "address", "apply", "client_options",
           "connect", "env_key"]
