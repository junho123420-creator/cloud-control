def inspect_group(group):
    findings = []
    for permission in group.get("IpPermissions", []):
        protocol = str(permission.get("IpProtocol", ""))
        start = permission.get("FromPort")
        end = permission.get("ToPort")

        if protocol == "-1":
            ports = [22, 3389]
        elif protocol in ("tcp", "6"):
            if not isinstance(start, int) or not isinstance(end, int):
                continue
            ports = [p for p in (22, 3389) if start <= p <= end]
        else:
            continue

        if not ports:
            continue

        sources = []
        for item in permission.get("IpRanges", []):
            if item.get("CidrIp") == "0.0.0.0/0":
                sources.append(("IPv4", item["CidrIp"]))
        for item in permission.get("Ipv6Ranges", []):
            if item.get("CidrIpv6") == "::/0":
                sources.append(("IPv6", item["CidrIpv6"]))

        for family, cidr in sources:
            findings.append({
                "group_id": group["GroupId"],
                "group_name": group.get("GroupName", ""),
                "ip_family": family,
                "cidr": cidr,
                "protocol": protocol,
                "from_port": start,
                "to_port": end,
                "management_ports": ports,
                "result": "공개 관리 포트 규칙: 검토 필요",
            })
    return findings