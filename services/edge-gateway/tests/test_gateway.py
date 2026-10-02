"""Run: python -m unittest discover -s services/edge-gateway/tests -v."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import paho.mqtt.client as mqtt
from local_checks import LocalProcessor
from outbox import Outbox
from publisher import ReplayWorker


def now_ms():
    return time.time_ns() // 1_000_000


def until(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('Condition not reached before test deadline')


class BufferFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'buffer.db')
        self.box = Outbox(self.path)

    def tearDown(self):
        self.box.close()
        self.tmp.cleanup()


class BufferTests(BufferFixture):
    def test_restart_keeps_exact_payload_and_alarm_state(self):
        payload = '{"ts":12345,"temperature":-5.2}'
        self.box.record(12345, [('topic', payload)], 'machine', {'overheating': True})
        self.box.close()
        self.box = Outbox(self.path)
        self.assertEqual(self.box.oldest()[1:], (12345, 'topic', payload))
        self.assertEqual(self.box.state('machine'), {'overheating': True})

    def test_atomic_sample_and_transition(self):
        with self.assertRaises(TypeError):
            self.box.record(1, [('telemetry', '{}'), ('status', '{}')], 'machine', {'bad': object()})
        self.assertEqual(self.box.count(), 0)
        self.assertEqual(self.box.state('machine'), {})

    def test_retention_boundary_and_fifo(self):
        for ts in [9, 10, 11]:
            self.box.record(ts, [('topic', str(ts))], 'machine', {})
        self.assertEqual(self.box.prune(10), 1)
        first = self.box.oldest()
        self.assertEqual(first[1], 10)
        self.box.acknowledge(first[0])
        self.assertEqual(self.box.oldest()[1], 11)


class FakeClient:
    def __init__(self, acks, delivered, mode='ack'):
        self.acks, self.delivered, self.mode = acks, delivered, mode
        self.connected = mode != 'offline'
        self.closed = False
        self.mid = 0

    def is_connected(self):
        return self.connected

    def publish(self, topic, payload, qos, retain):
        assert qos == 1 and not retain
        self.mid += 1
        self.delivered.append((topic, payload))
        if self.mode in ('ack', 'disconnect_race'):
            # Deliberately ACK before publish returns to exercise the callback race.
            self.acks.put(self.mid)
        code = mqtt.MQTT_ERR_NO_CONN if self.mode == 'disconnect_race' else mqtt.MQTT_ERR_SUCCESS
        return SimpleNamespace(mid=self.mid, rc=code)

    def disconnect(self):
        self.closed = True
        self.connected = False

    def loop_stop(self):
        pass


class ReplayTests(BufferFixture):
    def start_worker(self, factory, timeout=1):
        self.stop = threading.Event()
        self.worker = ReplayWorker(self.box, factory, 72, timeout, self.stop, lambda **kw: None)
        self.worker.start()
        self.worker.ready.wait(2)
        self.addCleanup(self.stop_worker)
        return self.worker

    def stop_worker(self):
        self.stop.set()
        self.worker.join(3)
        self.assertFalse(self.worker.is_alive())

    def tearDown(self):
        if hasattr(self, 'worker'):
            self.stop_worker()
        super().tearDown()

    def add_reading(self, ts=None):
        ts = ts if ts is not None else now_ms()
        self.box.record(ts, [('topic', json.dumps({'ts': ts}))], 'machine', {})

    def test_acknowledgement_race_fifo_and_timestamp_preservation(self):
        ts = now_ms()
        for offset in range(4):
            self.add_reading(ts + offset)
        sent = []
        self.start_worker(lambda acks: FakeClient(acks, sent))
        until(lambda: self.box.count() == 0)
        self.assertEqual([json.loads(body)['ts'] for _, body in sent], [ts+i for i in range(4)])

    def test_no_ack_keeps_row_until_fresh_client_acknowledges(self):
        self.add_reading()
        clients, sent = [], []
        def factory(acks):
            mode = 'silent' if not clients else 'ack'
            client = FakeClient(acks, sent, mode)
            clients.append(client)
            return client
        self.start_worker(factory, timeout=0.3)
        until(lambda: len(sent) == 1)
        self.assertEqual(self.box.count(), 1)
        until(lambda: self.box.count() == 0)
        self.assertTrue(clients[0].closed)
        self.assertEqual(sent[0], sent[1])

    def test_disconnected_publisher_does_not_drop_data(self):
        sent = []
        self.add_reading()
        self.start_worker(lambda acks: FakeClient(acks, sent, 'offline'))
        time.sleep(0.15)
        self.assertEqual(self.box.count(), 1)
        self.assertEqual(sent, [])

    def test_disconnect_during_publish_ack_still_removes_exact_row(self):
        self.add_reading()
        self.start_worker(lambda acks: FakeClient(acks, [], 'disconnect_race'))
        until(lambda: self.box.count() == 0)

    def test_stale_ack_cannot_remove_current_row(self):
        self.add_reading()
        sent = []
        def factory(acks):
            acks.put(999)
            return FakeClient(acks, sent, 'silent')
        self.start_worker(factory, timeout=5)
        until(lambda: len(sent) == 1)
        time.sleep(0.15)
        self.assertEqual(self.box.count(), 1)

    def test_expired_reading_never_published(self):
        self.add_reading(now_ms() - 73 * 3_600_000)
        sent = []
        self.start_worker(lambda acks: FakeClient(acks, sent))
        until(lambda: self.box.count() == 0)
        self.assertEqual(sent, [])

    def test_old_client_ack_cannot_acknowledge_retried_message(self):
        self.add_reading()
        clients = []
        def factory(acks):
            client = FakeClient(acks, [], 'silent')
            clients.append(client)
            return client
        self.start_worker(factory, timeout=0.5)
        until(lambda: len(clients) >= 2 and clients[1].mid == 1)
        clients[0].acks.put(1)
        time.sleep(0.12)
        self.assertEqual(self.box.count(), 1)
        clients[1].acks.put(1)
        until(lambda: self.box.count() == 0)

    def test_worker_failure_is_visible(self):
        def broken(acks):
            raise OSError('test failure')
        worker = self.start_worker(broken)
        until(lambda: worker.failure is not None)
        self.assertTrue(self.stop.is_set())


class LocalChecksTests(unittest.TestCase):
    def sample(self, temperature, vibration=0.8):
        return {'temperature': temperature, 'pressure': 4.2, 'vibration': vibration}

    def test_threshold_hysteresis_and_single_transition(self):
        processor = LocalProcessor(60, 85, 80)
        transitions = [processor.evaluate(self.sample(value), i)[1]
                       for i, value in enumerate([84.9, 85, 86, 84, 80, 79.9, 85])]
        self.assertEqual(transitions, [None, 'active', None, None, None, 'resolved', 'active'])

    def test_restart_keeps_active_state_without_duplicate_alarm(self):
        processor = LocalProcessor(60, 85, 80, {'overheating': True})
        self.assertIsNone(processor.evaluate(self.sample(90), 0)[1])
        self.assertEqual(processor.evaluate(self.sample(79), 1)[1], 'resolved')

    def test_window_excludes_stale_samples_and_alarm_uses_latest_value(self):
        processor = LocalProcessor(60, 85, 80)
        processor.evaluate(self.sample(70, 1.2), 0)
        features, transition = processor.evaluate(self.sample(90, 0.5), 1)
        self.assertEqual(features['temperature_avg_c'], 80)
        self.assertEqual(features['vibration_peak_mm_s'], 1.2)
        self.assertEqual(transition, 'active')
        features, _ = processor.evaluate(self.sample(79, 0.7), 61)
        self.assertEqual(features['sample_count'], 1)
        self.assertEqual(features['temperature_avg_c'], 79)
        self.assertEqual(features['vibration_peak_mm_s'], 0.7)


class RuntimeFailureTests(unittest.TestCase):
    def test_health_distinguishes_outage_from_worker_failure(self):
        import main as gateway
        with patch.dict(gateway.STATUS, {'mqtt_connected': False, 'modbus_connected': True,
                                        'worker_error': None, 'buffered_messages': 4}):
            self.assertEqual(gateway.health_snapshot()['status'], 'degraded')
            self.assertEqual(gateway.health_snapshot()['buffered_messages'], 4)
            gateway.update_status(worker_error='test failure')
            self.assertEqual(gateway.health_snapshot()['status'], 'error')

    def test_storage_failure_stops_instead_of_discarding_sample(self):
        import main as gateway
        config = {'SITE_ID': 'test', 'MACHINE_ID': 'machine', 'MODBUS_HOST': 'simulator',
                  'MODBUS_PORT': '502', 'MODBUS_UNIT_ID': '1', 'POLL_INTERVAL_S': '1',
                  'LOCAL_TEMP_CRITICAL_C': '85', 'LOCAL_TEMP_CLEAR_C': '80'}
        box = Mock()
        box.state.return_value = {}
        box.count.return_value = 0
        box.record.side_effect = sqlite3.OperationalError('disk full')
        worker = Mock(failure=None, ident=None)
        with patch.dict(gateway.CONFIG, config), patch.object(gateway, 'create_mqtt_client'), \
             patch.object(gateway, 'Outbox', return_value=box), \
             patch.object(gateway, 'ReplayWorker', return_value=worker), \
             patch.object(gateway, 'ModbusTcpClient'), \
             patch.object(gateway, 'read_sensor_registers', return_value=[700, 420, 80]):
            with self.assertRaises(sqlite3.OperationalError):
                gateway.run_gateway()
        box.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
