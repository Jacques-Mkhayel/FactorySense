"""Replay the SQLite outbox in order without blocking PLC collection."""
import logging
import queue
import threading
import time

import paho.mqtt.client as mqtt

log = logging.getLogger("edge-gateway")


class ReplayWorker(threading.Thread):
    def __init__(self, outbox, client_factory, retention_h, ack_timeout_s, stop, status):
        super().__init__(name="mqtt-replay", daemon=True)
        self.outbox = outbox
        self.client_factory = client_factory
        self.retention_ms = int(retention_h * 3_600_000)
        self.ack_timeout_s = ack_timeout_s
        self.stop = stop
        self.status = status
        self.failure = None
        self.ready = threading.Event()

    def run(self):
        client = None
        try:
            acknowledgements = queue.Queue()
            client = self.client_factory(acknowledgements)
            self.ready.set()
            pending = None  # (database row id, MQTT message id, acknowledgement deadline)
            while not self.stop.is_set():
                expired = self.outbox.prune(time.time_ns() // 1_000_000 - self.retention_ms)
                if expired:
                    log.warning("buffer_expired count=%d", expired)
                self.status(buffered_messages=self.outbox.count())
                row = self.outbox.oldest()

                if pending is not None:
                    try:
                        acknowledged = acknowledgements.get(timeout=0.1) == pending[1]
                    except queue.Empty:
                        acknowledged = False
                    if acknowledged:
                        # Delete only the specific durable row matched to this broker ACK.
                        self.outbox.acknowledge(pending[0])
                        log.info("buffer_delivered row_id=%d mid=%d", pending[0], pending[1])
                        pending = None
                        continue
                    if row is None or row[0] != pending[0] or time.monotonic() >= pending[2]:
                        log.warning("mqtt_delivery_retry row_id=%d reason=expired_or_ack_timeout", pending[0])
                        # Retire the old client and its memory queue before resubmitting.
                        # A fresh ACK queue prevents stale message IDs deleting a new row.
                        client.disconnect()
                        client.loop_stop()
                        acknowledgements = queue.Queue()
                        client = self.client_factory(acknowledgements)
                        pending = None
                    self.stop.wait(0.1)
                    continue

                if row is None or not client.is_connected():
                    self.stop.wait(0.1)
                    continue
                row_id, _, topic, payload = row
                try:
                    info = client.publish(topic, payload=payload, qos=1, retain=False)
                except (OSError, ValueError) as exc:
                    log.warning("mqtt_publish_failed row_id=%d error=%s", row_id, exc)
                    client.disconnect()
                    client.loop_stop()
                    acknowledgements = queue.Queue()
                    client = self.client_factory(acknowledgements)
                    self.stop.wait(1)
                    continue
                # Paho can retain a QoS 1 message after a disconnect races with publish.
                if info.rc in (mqtt.MQTT_ERR_SUCCESS, mqtt.MQTT_ERR_NO_CONN):
                    pending = (row_id, info.mid, time.monotonic() + self.ack_timeout_s)
                    log.info("mqtt_publish_queued row_id=%d topic=%s mid=%d qos=1", row_id, topic, info.mid)
                else:
                    log.warning("mqtt_publish_unconfirmed row_id=%d reason=%s", row_id, mqtt.error_string(info.rc))
                    self.stop.wait(1)
        except Exception as exc:
            # Expose worker failure to the main loop; never silently lose the replay worker.
            self.failure = exc
            self.status(worker_error=str(exc))
            log.exception("replay_worker_failed")
            self.stop.set()
        finally:
            self.ready.set()
            if client is not None:
                client.disconnect()
                client.loop_stop()
