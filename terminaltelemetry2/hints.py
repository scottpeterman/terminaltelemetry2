"""What a failed poll most likely means, in words an operator can act on.

Matched against the parse error and the raw output together; first hit
wins, so specific causes are listed before generic ones.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

_HINTS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"docker\.sock.*permission denied|permission denied.*docker", re.I | re.S),
     "The login user can't reach the Docker socket. Add it to the docker group "
     "(sudo usermod -aG docker <user>), then reconnect -- group changes apply to new logins."),
    (re.compile(r"failed to connect to any daemons|can't open configuration file /etc/frr|"
                r"vtysh.*permission denied", re.I | re.S),
     "vtysh can't reach FRR as this user. Add it to the frrvty group "
     "(sudo usermod -aG frrvty <user>), then reconnect."),
    (re.compile(r"cannot connect to the docker daemon|is the docker daemon running", re.I),
     "Docker is installed but its daemon isn't running on this host."),
    (re.compile(r"is not allowed to execute|not in the sudoers file|is not in the sudoers", re.I),
     "sudo refuses this command for this user. Add a NOPASSWD sudoers rule for it, e.g. "
     "'<user> ALL=(root) NOPASSWD: /usr/bin/docker, /usr/bin/vtysh'."),
    (re.compile(r"sudo: .*password|a password is required|a terminal is required", re.I),
     "The command needs sudo with a password; the telemetry shell can't answer prompts. "
     "Allow it NOPASSWD for this user, or poll without sudo."),
    (re.compile(r"(command )?not found$|: not found\b|command not found", re.I | re.M),
     "The command isn't installed, or isn't on the telemetry shell's PATH -- that shell "
     "is a plain /bin/sh, not your login shell. If it's installed, use its full path in "
     "the widget or pack command."),
    (re.compile(r"% ?invalid input|unrecognized command|unknown command|syntax error|"
                r"% ?incomplete command|wrong parameter", re.I),
     "The device rejected the command -- it may be spelled differently on this OS or "
     "release. Check the command in the platform pack (tt2 --packs)."),
    (re.compile(r"permission denied|access denied|not authori[sz]ed|authorization failed|"
                r"privilege", re.I),
     "Permission denied for this login. The command may need a higher privilege level "
     "(enable) or group membership."),
    (re.compile(r"no template for .* parsed this output|state error", re.I),
     "The command ran but no template understood the output. Open the lab ({ }) to fix "
     "or pick a template."),
]


def hint_for(error: Optional[str], output: str = "") -> Optional[str]:
    """First matching hint for this failure, or None."""
    text = f"{error or ''}\n{output or ''}"
    for rx, hint in _HINTS:
        if rx.search(text):
            return hint
    return None
