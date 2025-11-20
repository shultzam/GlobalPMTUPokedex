#!/usr/bin/env python3

# /opt/pmtu-pokedex/ddns/

import json
import os
import sys
import time
from datetime import datetime

import requests

CONFIG_PATH = os.environ.get("CF_DDNS_CONFIG", "../configs/cf_ddns_config.json")
IP_CHECK_URL = "https://ifconfig.me/ip"
CF_API_BASE = "https://api.cloudflare.com/client/v4"


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[CF-DDNS] {ts} {msg}")


def load_config():
    if not os.path.exists(CONFIG_PATH):
        log(f"ERROR: config file not found: {CONFIG_PATH}")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_public_ip():
    resp = requests.get(IP_CHECK_URL, timeout=5)
    resp.raise_for_status()
    ip = resp.text.strip()
    log(f"Current public IP detected as {ip}")
    return ip


def get_record(zone_id, record_id, headers):
    url = f"{CF_API_BASE}/zones/{zone_id}/dns_records/{record_id}"
    resp = requests.get(url, headers=headers, timeout=10)
    if not resp.ok:
        log(f"ERROR: unable to fetch DNS record {record_id}: {resp.status_code} {resp.text}")
        return None
    data = resp.json()
    if not data.get("success"):
        log(f"ERROR: Cloudflare API error getting record {record_id}: {data}")
        return None
    return data["result"]


def update_record(zone_id, record, new_ip, headers):
    record_id = record["id"]
    name = record["name"]

    url = f"{CF_API_BASE}/zones/{zone_id}/dns_records/{record_id}"

    payload = {
        "type": "A",
        "name": name,
        "content": new_ip,
        "ttl": 300,
        "proxied": record.get("proxied", False),
    }

    resp = requests.put(url, headers=headers, json=payload, timeout=10)
    if not resp.ok:
        log(f"ERROR: failed to update record {name} ({record_id}): {resp.status_code} {resp.text}")
        return False

    data = resp.json()
    if not data.get("success"):
        log(f"ERROR: Cloudflare API error updating {name}: {data}")
        return False

    log(f"Updated {name} to {new_ip}")
    return True


def main():
    cfg = load_config()
    api_token = cfg["api_token"]
    zone_id = cfg["zone_id"]
    records = cfg["records"]

    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    try:
        current_ip = get_public_ip()
    except Exception as e:
        log(f"ERROR: cannot detect public IP: {e}")
        return 1

    for rec in records:
        # Get the existing record details
        rec_id = rec["id"]
        existing = get_record(zone_id, rec_id, headers)
        if not existing:
            continue

        old_ip = existing.get("content")
        name = existing.get("name")

        if old_ip == current_ip:
            log(f"No change for {name} (still {old_ip})")
            continue

        # Update the record
        update_record(zone_id, existing, current_ip, headers)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
