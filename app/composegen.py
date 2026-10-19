"""Rebuild a compose file from a running container's actual configuration
(`docker inspect`). Used when adopting containers whose original compose
file Dockup can't read, and for adopting plain `docker run` containers.
"""

import yaml


def containers_to_compose(inspects: list[dict]) -> str:
    services = {}
    networks_top = {}
    for ins in inspects:
        name = (ins.get("Name") or "app").lstrip("/")
        cfg = ins.get("Config") or {}
        host = ins.get("HostConfig") or {}
        labels = cfg.get("Labels") or {}
        service_name = labels.get("com.docker.compose.service") or _safe(name)

        svc: dict = {"image": cfg.get("Image") or ins.get("Image", ""),
                     "container_name": name}

        restart = (host.get("RestartPolicy") or {}).get("Name") or ""
        svc["restart"] = restart if restart and restart != "no" else "unless-stopped"

        ports = []
        for spec, binds in (host.get("PortBindings") or {}).items():
            cport, proto = (spec.split("/") + ["tcp"])[:2]
            for b in binds or []:
                hport = b.get("HostPort") or ""
                hip = b.get("HostIp") or ""
                entry = f"{hport}:{cport}" if hport else cport
                if hip and hip not in ("0.0.0.0", "::"):
                    entry = f"{hip}:{entry}"
                if proto != "tcp":
                    entry += f"/{proto}"
                ports.append(entry)
        if ports:
            svc["ports"] = sorted(set(ports))

        vols = []
        for m in ins.get("Mounts") or []:
            if m.get("Type") == "volume":
                vols.append(f"{m.get('Name')}:{m.get('Destination')}")
            elif m.get("Type") == "bind":
                suffix = "" if m.get("RW", True) else ":ro"
                vols.append(f"{m.get('Source')}:{m.get('Destination')}{suffix}")
        if vols:
            svc["volumes"] = vols

        env = [e for e in cfg.get("Env") or [] if not e.startswith("PATH=")]
        if env:
            svc["environment"] = env

        if cfg.get("WorkingDir"):
            svc["working_dir"] = cfg["WorkingDir"]
        if cfg.get("User"):
            svc["user"] = cfg["User"]
        # `docker inspect` reports the argv the container was actually
        # launched with, image default or not. Reproducing it exactly is
        # what makes an adopted stack behave like the container it came
        # from; dropping it silently changes what runs.
        if cfg.get("Entrypoint"):
            svc["entrypoint"] = list(cfg["Entrypoint"])
        if cfg.get("Cmd"):
            svc["command"] = list(cfg["Cmd"])
        if host.get("Privileged"):
            svc["privileged"] = True
        if host.get("CapAdd"):
            svc["cap_add"] = list(host["CapAdd"])
        if host.get("Devices"):
            svc["devices"] = [f"{d.get('PathOnHost')}:{d.get('PathInContainer')}"
                              for d in host["Devices"]]
        net_mode = host.get("NetworkMode") or ""
        if net_mode and net_mode not in ("default", "bridge", "host", "none") and not net_mode.startswith("container:"):
            # A custom bridge network, not `network_mode:`. network_mode
            # attaches to an existing network by its literal name and never
            # creates it, so it breaks as soon as that network is pruned.
            # Declared under networks: with an explicit name, compose manages
            # it. It has to be external: true, because adoption always means the
            # network already exists and almost never carries the label compose
            # puts on networks it created itself; without it the next redeploy
            # fails with "found but has incorrect label".
            svc["networks"] = [net_mode]
            networks_top[net_mode] = {"name": net_mode, "external": True}

        services[service_name] = svc

    volumes_top = {}
    for svc in services.values():
        for v in svc.get("volumes", []):
            first = v.split(":")[0]
            if first and not first.startswith(("/", ".", "~")):
                volumes_top[first] = {"external": True}

    doc: dict = {"services": services}
    if volumes_top:
        doc["volumes"] = volumes_top
    if networks_top:
        doc["networks"] = networks_top
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, width=120)


def rewrite_relative_binds(compose_text: str, original_dir: str) -> str:
    """Make relative bind mounts absolute against the stack's original home,
    so an adopted file keeps pointing at the same data."""
    try:
        doc = yaml.safe_load(compose_text)
    except yaml.YAMLError:
        return compose_text
    if not isinstance(doc, dict):
        return compose_text
    changed = False
    for svc in (doc.get("services") or {}).values():
        if not isinstance(svc, dict):
            continue
        vols = svc.get("volumes")
        if not isinstance(vols, list):
            continue
        for i, v in enumerate(vols):
            if isinstance(v, str) and (v.startswith("./") or v.startswith("../")):
                vols[i] = original_dir.rstrip("/") + "/" + v.split(":", 1)[0].lstrip("./") \
                    + (":" + v.split(":", 1)[1] if ":" in v else "")
                changed = True
            elif isinstance(v, dict) and str(v.get("source", "")).startswith(("./", "../")):
                v["source"] = original_dir.rstrip("/") + "/" + v["source"].lstrip("./")
                changed = True
    if not changed:
        return compose_text
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, width=120)


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in name).strip("-") or "app"
