"""Who may call the API (T20; HIL-07, INT-05, docs/SECURITY.md).

Every request but /health and /metrics carries ``Authorization: Bearer <token>``. Tokens come
from the environment:

* ``PDM_TOKEN_<ROLE>``: a token for the role, recorded under the role's name;
* ``PDM_TOKEN_<ROLE>_<NAME>``: a token for one person (``PDM_TOKEN_REVIEWER_ROSH``), recorded
  under that name. A reviewer signs in to the dashboard with their own token, so every decision
  names who made it (HIL-07).

Roles: reviewer, engineer, operator and evaluator (people), agent (services/agent). The
connector's token is for the work-order endpoint and grants nothing here. The API refuses to
start with no token, a token shorter than 32 characters, or two variables holding the same
token. A token is compared with ``hmac.compare_digest`` against every known token, never logged,
and never echoed in an error. No token or an unknown one: 401 with ``WWW-Authenticate: Bearer``;
a known token on an endpoint its role may not use: 403.
"""
from __future__ import annotations

import hmac
import os
import re
from typing import Mapping, NamedTuple, Optional

ROLES = ("reviewer", "engineer", "operator", "evaluator", "agent")
PEOPLE = ("reviewer", "engineer", "operator", "evaluator")
MIN_LENGTH = 32
_VAR = re.compile(r"^PDM_TOKEN_(REVIEWER|ENGINEER|OPERATOR|EVALUATOR|AGENT)(?:_([A-Z0-9][A-Z0-9_]*))?$")


class AuthError(RuntimeError):
    pass


class Principal(NamedTuple):
    role: str
    name: str

    @property
    def actor(self) -> str:
        """As the incident lifecycle records it: 'reviewer:rosh'."""
        return f"{self.role}:{self.name}" if self.name != self.role else self.role


def load_tokens(env: Optional[Mapping[str, str]] = None) -> dict[str, Principal]:
    """{token: principal} from PDM_TOKEN_<ROLE>[_<NAME>]; AuthError on a weak or repeated token."""
    env = os.environ if env is None else env
    found: dict[str, Principal] = {}
    for key in sorted(env):
        m = _VAR.match(key)
        value = env[key]
        if not m or not value:
            continue
        if len(value) < MIN_LENGTH:
            raise AuthError(f"{key} is shorter than {MIN_LENGTH} characters")
        if value in found:
            raise AuthError(f"{key} holds the same token as another variable")
        role = m.group(1).lower()
        found[value] = Principal(role, (m.group(2) or role).lower())
    if not found:
        raise AuthError("no PDM_TOKEN_<ROLE> is set; the API has nobody to let in")
    return found


def authenticate(header: Optional[str], tokens: Mapping[str, Principal]) -> Optional[Principal]:
    """The principal behind an Authorization header, or None. Compares against every token."""
    if not header or not header.startswith("Bearer "):
        return None
    given = header[len("Bearer "):].strip().encode()
    match = None
    for token, principal in tokens.items():
        if hmac.compare_digest(given, token.encode()):
            match = principal
    return match
