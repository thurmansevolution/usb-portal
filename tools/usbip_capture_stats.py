#!/usr/bin/env python3
"""Summarize single-packet USB/IP interrupt request/reply timing from tcpdump pcap.

Capture with tcpdump -i INTERFACE -s 256 -w capture.pcap 'tcp port 3240'.
Keep both controller input devices open during capture to enable HID polling.
These timings include USB polling; they do not measure button-to-screen delay.
"""
import argparse
import collections
import json
import statistics
import struct
from pathlib import Path


def summarize(values):
    values = sorted(values)
    if not values:
        return {}
    return {
        'samples': len(values),
        'median_ms': round(statistics.median(values), 3),
        'p95_ms': round(values[int(.95 * (len(values)-1))], 3),
        'p99_ms': round(values[int(.99 * (len(values)-1))], 3),
        'max_ms': round(max(values), 3),
    }


def analyze(path, server_port=3240):
    data = Path(path).read_bytes()
    if data[:4] != b'\xd4\xc3\xb2\xa1' or len(data) < 24 or struct.unpack_from('<I', data, 20)[0] != 1:
        raise ValueError('Expected a little-endian Ethernet pcap with microsecond timestamps')
    pending = {}
    completed = collections.defaultdict(list)
    delays = collections.defaultdict(list)
    position = 24
    while position + 16 <= len(data):
        sec, usec, length, _ = struct.unpack_from('<IIII', data, position)
        position += 16
        packet = data[position:position+length]
        position += length
        timestamp = sec + usec / 1e6
        if len(packet) < 54 or packet[12:14] != b'\x08\x00':
            continue
        ip = packet[14:]
        ihl = (ip[0] & 15) * 4
        if ip[9] != 6 or len(ip) < ihl + 20:
            continue
        tcp = ip[ihl:]
        source, destination = struct.unpack_from('!HH', tcp)
        payload = tcp[(tcp[12] >> 4) * 4:]
        if len(payload) < 48:
            continue
        command, sequence, _, direction, endpoint = struct.unpack_from('!IIIII', payload)
        client = source if destination == server_port else destination
        key = (client, sequence)
        if destination == server_port and command == 1 and direction == 1 and endpoint == 1:
            pending.setdefault(key, timestamp)
        elif source == server_port and command == 3:
            # RET_SUBMIT leaves direction and endpoint zero; match by stream/sequence.
            if key in pending:
                completed[client].append(timestamp)
                delays[client].append((timestamp - pending.pop(key)) * 1000)
    return {
        str(client): {
            'usb_poll_round_trip': summarize(delays[client]),
            'report_spacing': summarize([(b-a)*1000 for a,b in zip(times,times[1:])]),
        }
        for client, times in completed.items()
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pcap')
    parser.add_argument('--port', type=int, default=3240)
    args = parser.parse_args()
    print(json.dumps(analyze(args.pcap, args.port), indent=2))
