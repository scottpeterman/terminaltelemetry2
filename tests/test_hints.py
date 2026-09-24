import pytest

from terminaltelemetry2.hints import hint_for


@pytest.mark.parametrize("error,output,expect", [
    ("ValueError: permission denied while trying to connect to the Docker daemon socket at "
     "unix:///var/run/docker.sock: Get ...", "", "docker group"),
    ("ValueError: Exiting: failed to connect to any daemons.", "", "frrvty"),
    ("ValueError: Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
     "Is the docker daemon running?", "", "daemon isn't running"),
    ("ValueError: /bin/sh: 6: ip: not found", "", "isn't installed"),
    ("no JSON", "sudo: a password is required", "sudo"),
    ("no template for 'x' parsed this output", "% Invalid input detected at '^' marker.",
     "rejected the command"),
    ("no template for 'hp_comware_x' parsed this output", " % Wrong parameter found at '^'",
     "rejected the command"),
    ("no template for 'x' parsed this output", "Interface  Status\nEth1  up", "lab"),
])
def test_hints(error, output, expect):
    assert expect in (hint_for(error, output) or "")


def test_no_hint_for_unknown():
    assert hint_for("ValueError: something odd", "weird") is None
