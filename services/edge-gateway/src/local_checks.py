"""Demonstration calculations and local alarm transitions; independent of the cloud."""
from collections import deque
from statistics import fmean


class LocalProcessor:
    def __init__(self, window_s, critical_c, clear_c, state=None):
        self.window_s = window_s
        self.critical_c = critical_c
        self.clear_c = clear_c
        self.samples = deque(maxlen=10000)
        self.state = dict(state or {})

    def evaluate(self, measurements, monotonic_s):
        """Use fresh readings only. Trigger immediately; smoothing never delays an alarm."""
        while self.samples and self.samples[0][0] <= monotonic_s - self.window_s:
            self.samples.popleft()
        self.samples.append((monotonic_s, dict(measurements)))
        features = {
            "sample_count": len(self.samples),
            "temperature_avg_c": fmean(sample["temperature"] for _, sample in self.samples),
            "pressure_avg_bar": fmean(sample["pressure"] for _, sample in self.samples),
            "vibration_peak_mm_s": max(sample["vibration"] for _, sample in self.samples),
        }
        active = self.state.get("overheating", False)
        temperature = measurements["temperature"]
        transition = None
        if not active and temperature >= self.critical_c:
            self.state["overheating"] = True
            transition = "active"
        elif active and temperature < self.clear_c:
            self.state["overheating"] = False
            transition = "resolved"
        return features, transition
