"""Explicit live test in a temporary gateway container; requires subscriber credentials.

Uses a temporary SQLite file and local test PLC. Never stops the project broker or PLC.
Publishes readings under a unique replay-test-* site so test data is identifiable.
"""
import asyncio
import json
import os
from pathlib import Path
import queue
import signal
import sqlite3
import ssl
import sys
import tempfile
import threading
import time
import urllib.request
import uuid

import paho.mqtt.client as mqtt
from pymodbus.datastore import ModbusDeviceContext, ModbusSequentialDataBlock, ModbusServerContext
from pymodbus.server import ModbusTcpServer


async def eventually(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError('Expected state was not reached')


def health():
    try:
        with urllib.request.urlopen('http://127.0.0.1:18000/health', timeout=1) as response:
            return json.load(response)
    except OSError:
        return {}


async def make_plc(temperature):
    registers = ModbusDeviceContext(ir=ModbusSequentialDataBlock(1, [0] * 16))
    server = ModbusTcpServer(ModbusServerContext(devices=registers), address=('127.0.0.1', 15021))
    await server.async_setValues(1, 4, 0, [round(temperature*10), 425, 83])
    await server.serve_forever(background=True)
    return server


async def main():
    site = 'replay-test-' + uuid.uuid4().hex[:10]
    machine = 'press-test'
    topic = f'factorysense/telemetry/{site}/{machine}'
    process = server = subscriber = None
    drains = []
    logs = []
    received = queue.Queue()
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / 'buffer.db')
        env = {**os.environ, 'SITE_ID': site, 'MACHINE_ID': machine,
               'BUFFER_PATH': path, 'BUFFER_RETENTION_H': '72', 'MODBUS_HOST': '127.0.0.1',
               'MODBUS_PORT': '15021', 'POLL_INTERVAL_S': '0.1', 'PORT': '18000',
               'LOCAL_WINDOW_S': '60', 'LOCAL_TEMP_CRITICAL_C': '85', 'LOCAL_TEMP_CLEAR_C': '80',
               'MQTT_ACK_TIMEOUT_S': '5'}
        env.pop('MQTT_TEST_SUBSCRIBER_PASSWORD', None)
        async def start(extra):
            proc = await asyncio.create_subprocess_exec(sys.executable, '/app/src/main.py',
                env={**env, **extra}, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            async def drain():
                while line := await proc.stdout.readline():
                    logs.append(line.decode())
            drains.append(asyncio.create_task(drain()))
            return proc
        try:
            server = await make_plc(90)
            process = await start({'MQTT_HOST': '127.0.0.1', 'MQTT_PORT': '15022'})
            await eventually(lambda: health().get('buffered_messages', 0) >= 5)
            state = health()
            assert not state['mqtt_connected'] and state['modbus_connected']
            assert state['local_alerts']['overheating'] is True
            assert state['status'] == 'degraded'
            # Force a crash: the process cannot run its cleanup or flush Python memory.
            process.kill()
            await process.wait()
            with sqlite3.connect(path) as db:
                before = db.execute('SELECT topic,payload FROM outbox ORDER BY id').fetchall()
                alarm_state = json.loads(db.execute('SELECT value FROM local_state WHERE key=?', (topic,)).fetchone()[0])
            expected = [payload for row_topic, payload in before if row_topic == topic]
            events = [json.loads(payload) for row_topic, payload in before if row_topic != topic]
            assert len(expected) >= 4 and [e['state'] for e in events] == ['active']
            assert alarm_state['overheating'] is True
            print('PASS: offline collection, local overheating, health details and committed data survive forced process termination.')

            # Use the existing ingestor account for a separate test subscriber.
            subscriber = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=site + '-subscriber')
            subscriber.tls_set_context(ssl.create_default_context(cafile=os.environ['MQTT_CA_FILE']))
            subscriber.username_pw_set('ingestor', os.environ['MQTT_TEST_SUBSCRIBER_PASSWORD'])
            subscribed = threading.Event()
            subscription_errors = []
            def on_connect(client, userdata, flags, reason, properties):
                if reason.is_failure:
                    subscription_errors.append(str(reason))
                    subscribed.set()
                else:
                    client.subscribe(topic, qos=1)
            def on_subscribe(client, userdata, mid, reasons, properties):
                subscription_errors.extend(str(reason) for reason in reasons if reason.is_failure)
                subscribed.set()
            subscriber.on_connect = on_connect
            subscriber.on_subscribe = on_subscribe
            subscriber.on_message = lambda client, userdata, message: received.put(message.payload.decode())
            subscriber.connect_async(os.environ['MQTT_HOST'], int(os.environ['MQTT_PORT']))
            subscriber.loop_start()
            assert await asyncio.to_thread(subscribed.wait, 7) and not subscription_errors
            await server.shutdown()
            server = None
            # Restart on the same SQLite file with MQTT available but the PLC unavailable.
            process = await start({})
            await eventually(lambda: received.qsize() >= len(expected))
            await eventually(lambda: health().get('buffered_messages') == 0)
            state = health()
            assert state['mqtt_connected'] and not state['modbus_connected']
            assert state['local_alerts']['overheating'] is True
            actual = [received.get_nowait() for _ in range(received.qsize())]
            assert actual == expected, (actual, expected)
            with sqlite3.connect(path) as db:
                assert db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 0
            print('PASS: restart replays exact original JSON/timestamps in order, even while the PLC is down; ACKed rows are removed.')

            # Returning with the same high temperature must not emit a duplicate alarm.
            server = await make_plc(90)
            await eventually(lambda: health().get('modbus_connected'))
            await asyncio.sleep(0.3)
            active_events = [line for line in logs if 'local_alert=' in line and '"state": "active"' in line]
            assert len(active_events) == 1
            await server.async_setValues(1, 4, 0, [790, 425, 83])
            await eventually(lambda: health().get('local_alerts', {}).get('overheating') is False)
            await eventually(lambda: any('local_alert=' in line and '"state": "resolved"' in line for line in logs))
            assert any('modbus_recovered' in line for line in logs)
            print('PASS: PLC recovery resets backoff; active alarm survives restart without duplication and clears below 80 C.')
            process.send_signal(signal.SIGTERM)
            assert await asyncio.wait_for(process.wait(), timeout=6) == 0
            print('PASS: SIGTERM exits cleanly; pending unacknowledged messages remain durable.')
            print('TEST_SITE=' + site)
            print('REPLAYED_READINGS=' + str(len(expected)))
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            if server is not None:
                await server.shutdown()
            if subscriber is not None:
                subscriber.disconnect()
                subscriber.loop_stop()
            await asyncio.gather(*drains)


if __name__ == '__main__':
    asyncio.run(main())
