"""Convert a `docker run ...` command into compose YAML."""

import shlex

import yaml

# flags that take a value
_VALUE_FLAGS = {
    "-p", "--publish", "-v", "--volume", "-e", "--env", "--env-file", "--name",
    "--network", "--net", "--restart", "--hostname", "-h", "--user", "-u",
    "--workdir", "-w", "--entrypoint", "--label", "-l", "--add-host", "--dns",
    "--cap-add", "--cap-drop", "--device", "--memory", "-m", "--cpus",
    "--shm-size", "--health-cmd", "--platform", "--pull", "--stop-timeout",
    "--log-driver", "--log-opt", "--tmpfs", "--expose", "--link", "--ip",
    "--mac-address", "--pid", "--ipc", "--gpus", "--runtime",
}
_BOOL_FLAGS = {"-d", "--detach", "-i", "--interactive", "-t", "--tty", "--rm",
               "--privileged", "--init", "--read-only", "--no-healthcheck", "-P",
               "--publish-all"}

_MULTI = {"ports": ("-p", "--publish"), "volumes": ("-v", "--volume"),
          "environment": ("-e", "--env"), "labels": ("--label", "-l"),
          "cap_add": ("--cap-add",), "cap_drop": ("--cap-drop",),
          "devices": ("--device",), "extra_hosts": ("--add-host",),
          "dns": ("--dns",), "tmpfs": ("--tmpfs",), "expose": ("--expose",)}


def docker_run_to_compose(command: str) -> str:
    tokens = shlex.split(command.replace("\\\n", " ").replace("\\\r\n", " "))
    if not tokens:
        raise ValueError("Nothing to convert")
    # tolerate a leading "docker run" or "sudo docker run"
    while tokens and tokens[0] in ("sudo", "docker"):
        tokens.pop(0)
    if tokens and tokens[0] == "run":
        tokens.pop(0)
    if not tokens:
        raise ValueError("That doesn't look like a docker run command")

    collected: dict[str, list[str]] = {}
    single: dict[str, str] = {}
    image = None
    cmd_after_image: list[str] = []

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if image is not None:
            cmd_after_image.append(tok)
            i += 1
            continue
        if tok in _BOOL_FLAGS:
            if tok == "--privileged":
                single["privileged"] = "true"
            if tok == "--init":
                single["init"] = "true"
            if tok == "--read-only":
                single["read_only"] = "true"
            i += 1
            continue
        if tok.startswith("-"):
            if "=" in tok and tok.startswith("--"):
                flag, value = tok.split("=", 1)
            else:
                flag = tok
                if flag in _VALUE_FLAGS:
                    i += 1
                    if i >= len(tokens):
                        raise ValueError(f"Flag {flag} is missing its value")
                    value = tokens[i]
                else:
                    # unknown boolean-ish flag: skip it rather than guess
                    i += 1
                    continue
            _absorb(flag, value, collected, single)
            i += 1
            continue
        image = tok
        i += 1

    if image is None:
        raise ValueError("No image found in the command")

    name = single.pop("container_name", None) or _default_name(image)
    service: dict = {"image": image, "container_name": name}
    restart = single.pop("restart", None)
    service["restart"] = restart or "unless-stopped"
    for key in ("hostname", "user", "working_dir", "entrypoint", "network_mode",
                "mem_limit", "cpus", "shm_size", "privileged", "init",
                "read_only", "platform", "pid", "ipc", "mac_address"):
        if key in single:
            v = single[key]
            service[key] = True if v == "true" else v
    for compose_key in ("ports", "volumes", "environment", "labels", "cap_add",
                        "cap_drop", "devices", "extra_hosts", "dns", "tmpfs", "expose"):
        if collected.get(compose_key):
            service[compose_key] = collected[compose_key]
    if cmd_after_image:
        service["command"] = cmd_after_image if len(cmd_after_image) > 1 else cmd_after_image[0]

    doc = {"services": {name: service}}
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, width=120)


def _absorb(flag, value, collected, single):
    for compose_key, flags in _MULTI.items():
        if flag in flags:
            collected.setdefault(compose_key, []).append(value)
            return
    mapping = {
        "--name": "container_name", "--restart": "restart",
        "--hostname": "hostname", "-h": "hostname",
        "--user": "user", "-u": "user",
        "--workdir": "working_dir", "-w": "working_dir",
        "--entrypoint": "entrypoint",
        "--network": "network_mode", "--net": "network_mode",
        "--memory": "mem_limit", "-m": "mem_limit",
        "--cpus": "cpus", "--shm-size": "shm_size",
        "--platform": "platform", "--pid": "pid", "--ipc": "ipc",
        "--mac-address": "mac_address",
    }
    if flag in mapping:
        single[mapping[flag]] = value
    # anything else with a value (log opts etc.) is dropped knowingly


def _default_name(image: str) -> str:
    base = image.rsplit("/", 1)[-1].split(":")[0]
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in base) or "app"
