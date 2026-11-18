"""Replay AI4I sensor snapshots and run the loaded v7 model for each case."""
from __future__ import annotations

from threading import Event, Lock, Thread

from ai4i_replay_cases import ADDITIONAL_CASES
from ai4i_trial import build_state


SENSOR_KEYS = ("product_type", "air_temperature_K", "process_temperature_K",
               "rotational_speed_rpm", "torque_Nm", "tool_wear_min")

# AI4I source rows, selected for the demonstration. The model receives only
# the sensor values. Dataset labels and case names are used for display.
CASES = (
    ("정상 운전", "정상", ("M", 298.1, 308.6, 1551, 42.8, 0)),
    ("열 방출 장애", "열 방출 장애", ("M", 300.8, 309.4, 1342, 62.4, 113)),
    ("동력 고장", "동력 고장", ("L", 298.9, 309.1, 2861, 4.6, 143)),
    ("과부하 고장", "과부하 고장", ("L", 298.4, 308.2, 1282, 60.7, 216)),
    ("공구 마모 고장", "공구 마모 고장", ("L", 298.8, 308.9, 1455, 41.3, 208)),
)
EXTENDED_CASES = CASES + tuple(
    (f"{title} · 사례 {sample_index + 2}", expected,
     ADDITIONAL_CASES[expected][sample_index][1:])
    for sample_index in range(4)
    for title, expected, _ in CASES
)
REPLAY_CASES = {len(CASES): CASES, len(EXTENDED_CASES): EXTENDED_CASES}

COLLECTION_SECONDS = 1.5
PREDICTION_DISPLAY_SECONDS = 0.6
MEASURED_SECONDS = 2.0


class AI4ISimulation:
    def __init__(self, trial):
        self.trial = trial
        self.lock = Lock()
        self.model = "unloaded"
        self.phase = "idle"
        self.message = "모델을 불러오면 시뮬레이션을 시작할 수 있습니다."
        self.current = None
        self.records = []
        self.cancel = None
        self.total = len(CASES)

    def _snapshot(self):
        # Current case objects are replaced as they progress, never edited
        # in place. The HTTP handler can safely serialize the snapshot.
        return {"model": self.model, "phase": self.phase, "message": self.message,
                "active": self.cancel is not None, "total": self.total,
                "current": self.current, "records": list(self.records)}

    def snapshot(self):
        with self.lock:
            return self._snapshot()

    def load(self):
        with self.lock:
            if self.model in ("loading", "ready"):
                return self._snapshot()
            self.model = "loading"
            self.phase = "idle"
            self.message = "저장된 v7 모델을 불러오는 중입니다."
            Thread(target=self._load, daemon=True).start()
            return self._snapshot()

    def _load(self):
        try:
            self.trial.load_model()
        except Exception as exc:
            with self.lock:
                self.model = self.phase = "error"
                self.message = f"모델을 불러오지 못했습니다 ({type(exc).__name__}). {exc}"
            return
        with self.lock:
            self.model = "ready"
            self.message = "모델 준비 완료. 시작을 누르세요."

    def start(self, count=5):
        if type(count) is not int or count not in REPLAY_CASES:
            raise ValueError("5건 또는 25건 재생을 선택하세요.")
        cases = REPLAY_CASES[count]
        with self.lock:
            if self.model != "ready":
                raise ValueError("모델을 먼저 불러오세요.")
            if self.cancel is not None:
                raise ValueError("시뮬레이션이 이미 진행 중입니다.")
            self.records = []
            self.current = None
            self.total = len(cases)
            self.cancel = Event()
            self.phase = "collecting"
            self.message = "첫 번째 상황의 가상 센서값을 수집하는 중입니다."
            Thread(target=self._run, args=(self.cancel, cases), daemon=True).start()
            return self._snapshot()

    def stop(self):
        with self.lock:
            if self.cancel is not None:
                self.cancel.set()
                self.cancel = None
                self.phase = "stopped"
                self.message = "시뮬레이션을 중지했습니다."
            return self._snapshot()

    def _run(self, cancel, cases):
        try:
            for number, (title, expected, values) in enumerate(cases, start=1):
                sensors = dict(zip(SENSOR_KEYS, values))
                case = {"number": number, "title": title, "sensors": sensors,
                        "expected": expected}
                with self.lock:
                    if self.cancel is not cancel:
                        return
                    self.current = case
                    self.phase = "collecting"
                    self.message = "가상 센서값 수집 중"
                if cancel.wait(COLLECTION_SECONDS):
                    return
                with self.lock:
                    if self.cancel is not cancel:
                        return
                    self.phase = "predicting"
                    self.message = "Laya 모델 판단 중"
                answer = self.trial.predict(build_state(sensors), cancel=cancel)
                if cancel.wait(PREDICTION_DISPLAY_SECONDS):
                    return
                with self.lock:
                    if self.cancel is not cancel:
                        return
                    self.current = {**case, "diagnosis": answer}
                    self.records.append(self.current)
                    self.phase = "measured"
                    self.message = "측정 완료"
                if cancel.wait(MEASURED_SECONDS):
                    return
        except Exception as exc:
            self._finish(cancel, "error", f"모델 판단에 실패했습니다 ({type(exc).__name__}). {exc}")
            return
        self._finish(cancel, "complete", "모든 상황의 측정이 완료되었습니다.")

    def _finish(self, cancel, phase, message):
        with self.lock:
            if self.cancel is cancel:
                self.cancel = None
                self.phase = phase
                self.message = message
